"""AirPlay 2 receiver bridge.

Embeds the vendored ap2-receiver as a background server thread and routes
decoded audio + metadata into the AlfiePRIME pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import random
import socket
import struct
import sys
import threading
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alfieprime_musiciser.state import PlayerState
    from alfieprime_musiciser.visualizer import AudioVisualizer
    from alfieprime_musiciser.tui import BoomBoxTUI

logger = logging.getLogger(__name__)

# File log path for persistent AirPlay debug output
if sys.platform == "win32":
    _LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "alfieprime")
else:
    _LOG_DIR = os.path.join(os.path.expanduser("~"), ".cache", "alfieprime")
_LOG_FILE = os.path.join(_LOG_DIR, "airplay_debug.log")


_file_logging_active = False
_file_handler: logging.Handler | None = None


def setup_file_logging() -> str:
    """Enable file logging for AirPlay debug output. Returns the log path.

    Safe to call multiple times — only attaches once.
    On first call, rotates the existing log to .old (deleting any prior .old).
    """
    global _file_logging_active, _file_handler
    if _file_logging_active:
        return _LOG_FILE
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        # Rotate: delete .old, rename current → .old
        _old = _LOG_FILE + ".old"
        if os.path.exists(_old):
            os.remove(_old)
        if os.path.exists(_LOG_FILE):
            os.rename(_LOG_FILE, _old)
        handler = logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        # Use ASCII-safe separator to avoid encoding issues on Windows
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        _file_handler = handler
        # Attach to the root logger so we capture EVERYTHING — including
        # vendored AP2Handler per-connection loggers with dynamic names
        # like "AP2Handler: ip:port<=>ip:port" and HAP/Audio/Control loggers.
        root = logging.getLogger()
        root.addHandler(handler)
        # Also ensure our own loggers are at DEBUG
        for name in (
            "alfieprime_musiciser.airplay",
            "alfieprime_musiciser.airplay.receiver",
        ):
            lg = logging.getLogger(name)
            lg.setLevel(logging.DEBUG)
        _file_logging_active = True
        logger.info("AirPlay file log: %s", _LOG_FILE)
    except Exception as exc:
        logger.warning("Failed to set up file logging at %s: %s", _LOG_FILE, exc)
    return _LOG_FILE


def _reattach_file_logging() -> None:
    """Re-attach our file handler to the root logger.

    The vendored ap2/utils.py calls ``logging.config.dictConfig()`` at
    import time which reconfigures the root logger and REMOVES our handler.
    Call this AFTER importing vendored modules to restore file logging.
    """
    if _file_handler is None:
        return
    root = logging.getLogger()
    if _file_handler not in root.handlers:
        root.addHandler(_file_handler)
        logger.debug("Re-attached file logging handler after vendored import")


# ---------------------------------------------------------------------------
# Patch the vendored ap2 package so imports resolve correctly.
# The vendored code does ``from ap2.xxx import …`` – we redirect that to
# ``alfieprime_musiciser.airplay.vendor.ap2.xxx``.
# ---------------------------------------------------------------------------

_VENDOR_ROOT = os.path.join(os.path.dirname(__file__), "vendor")


def _patch_vendor_imports() -> None:
    """Add the vendor directory to sys.path and fix known packaging issues."""
    if _VENDOR_ROOT not in sys.path:
        sys.path.insert(0, _VENDOR_ROOT)

    # Fix: an empty ap2/playfair/ directory can shadow the real ap2/playfair.py
    # module. Detect and remove it at runtime if it exists.
    playfair_dir = os.path.join(_VENDOR_ROOT, "ap2", "playfair")
    playfair_init = os.path.join(playfair_dir, "__init__.py")
    playfair_mod = os.path.join(_VENDOR_ROOT, "ap2", "playfair.py")
    if os.path.isdir(playfair_dir) and os.path.isfile(playfair_mod):
        init_size = os.path.getsize(playfair_init) if os.path.isfile(playfair_init) else -1
        if init_size <= 0:
            # Empty __init__.py shadowing the real module — remove the directory
            import shutil
            try:
                shutil.rmtree(playfair_dir)
                logger.info("Removed empty ap2/playfair/ directory that was shadowing playfair.py")
            except OSError as exc:
                logger.warning("Could not remove ap2/playfair/ directory: %s", exc)
            # Purge any cached bad import
            for key in list(sys.modules):
                if "playfair" in key:
                    del sys.modules[key]


# ---------------------------------------------------------------------------
# Hooks – thin wrappers injected into the vendored AP2 audio pipeline
# ---------------------------------------------------------------------------


class _PCMConsumer:
    """Reads PCM chunks from a multiprocessing.Queue and feeds the visualizer.

    The queue is written to by the audio child process (in vendored audio.py).
    This consumer runs a daemon thread in the parent process.
    """

    def __init__(
        self,
        pcm_queue: multiprocessing.Queue,
        visualizer: AudioVisualizer,
        state: PlayerState | None = None,
        sample_rate: int = 44100,
        sample_size: int = 16,
        channels: int = 2,
    ):
        self._queue = pcm_queue
        self._visualizer = visualizer
        self._state = state
        self.sample_rate = sample_rate
        self.sample_size = sample_size
        self.channels = channels
        self._running = False
        self._thread: threading.Thread | None = None
        self.dj_mixer = None  # Set externally when DJ mode activates
        self.dj_feed_channel = "b"  # Which mixer channel to feed

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._consume, daemon=True)
        self._thread.start()
        logger.info("PCM consumer thread started")

    def stop(self) -> None:
        self._running = False

    def _consume(self) -> None:
        import queue as _queue_mod
        while self._running:
            try:
                data = self._queue.get(timeout=0.02)
            except _queue_mod.Empty:
                continue
            except (OSError, EOFError):
                break
            try:
                # Format update message from audio child process
                if isinstance(data, tuple) and len(data) == 4 and data[0] == "_fmt":
                    _, self.sample_rate, self.sample_size, self.channels = data
                    logger.info(
                        "AirPlay audio format: %d Hz, %d-bit, %d ch",
                        self.sample_rate, self.sample_size, self.channels,
                    )
                    continue

                # Feed DJ mixer when active — channel determined by dj_feed_channel
                mixer = self.dj_mixer
                if mixer is not None:
                    # AirPlay child always resamples to s16, so use 16-bit
                    if self.dj_feed_channel == "a":
                        mixer.set_format_a(self.sample_rate, 16, self.channels)
                        mixer.feed_a(data)
                    else:
                        mixer.set_format_b(self.sample_rate, 16, self.channels)
                        mixer.feed_b(data)

                # Only feed visualizer when AirPlay is the active source
                if self._state and self._state.active_source != "airplay":
                    continue
                # PCM from AirPlay child is always s16 (resampled), use 16-bit
                self._visualizer.set_format(
                    self.sample_rate, 16, self.channels,
                )
                # Ensure visualizer is unpaused — SendSpin may have left it
                # paused before we switched to AirPlay.
                if self._visualizer._paused:
                    self._visualizer.set_paused(False)
                # Ensure is_playing stays True while we're receiving audio —
                # some AirPlay senders don't always send SETRATEANCHORTIME
                # with rate > 0, leaving is_playing False while music plays.
                if self._state and not self._state.is_playing:
                    self._state.is_playing = True
                self._visualizer.feed_audio(data, immediate=True)
            except Exception:
                logger.warning("PCM consumer feed error", exc_info=True)


# ---------------------------------------------------------------------------
# Metadata / artwork callback holder
# ---------------------------------------------------------------------------


class _MetadataHook:
    """Receives metadata + artwork from the AP2Handler and updates PlayerState."""

    def __init__(self, state: PlayerState, visualizer: AudioVisualizer | None = None):
        self._state = state
        self._visualizer = visualizer
        self._last_title = ""

    def _is_active(self) -> bool:
        return self._state.active_source in ("airplay", "")

    def _set_playing(self, playing: bool) -> None:
        """Set is_playing, gated by active source."""
        if self._is_active():
            if not playing and self._state.is_playing:
                # Freeze progress at the current interpolated value so the
                # display doesn't jump when switching from interpolated to raw.
                self._state.progress_ms = self._state.get_interpolated_progress()
                self._state.playback_speed = 0.0
                self._state.progress_update_time = time.monotonic()
            self._state.is_playing = playing
            if self._visualizer:
                self._visualizer.set_paused(not playing)
        else:
            self._state.write_to_snapshot("airplay", is_playing=playing)

    def on_metadata(self, title: str, artist: str, album: str) -> None:
        s = self._state
        fields: dict = {}
        if title:
            fields["title"] = title
        if artist:
            fields["artist"] = artist
        if album:
            fields["album"] = album
        # Don't force is_playing here — let SETRATEANCHORTIME/DMAP caps control it

        if self._is_active():
            old_title = s.title
            for k, v in fields.items():
                setattr(s, k, v)
            s.connected = True
            if s.title != old_title and s.title:
                logger.info("AirPlay now playing: %s - %s [%s]", s.artist, s.title, s.album)
        else:
            s.write_to_snapshot("airplay", **fields)

    def on_artwork(self, data: bytes) -> None:
        if not data:
            return
        logger.info("AirPlay artwork received (%d bytes)", len(data))

        def _extract_and_apply() -> None:
            from alfieprime_musiciser.colors import _extract_theme_from_image, ColorTheme
            from alfieprime_musiciser.mpris import write_art_cache
            theme = _extract_theme_from_image(data) or ColorTheme()
            write_art_cache(data)
            if self._is_active():
                self._state.artwork_data = data
                self._state.theme = theme
            else:
                self._state.write_to_snapshot("airplay",
                    artwork_data=data, theme=theme,
                )

        import threading
        threading.Thread(target=_extract_and_apply, daemon=True).start()

    def on_volume(self, volume_db: float) -> None:
        # AirPlay volume is -144..0 dB.  -144 is a sentinel meaning the
        # iPhone's volume slider is at absolute zero — ignore it rather than
        # muting our output, because iPhones often send -144 during the
        # initial handshake before sending the real volume.
        logger.info("AirPlay volume received: %.2f dB", volume_db)
        if volume_db <= -144:
            # Don't mute — just set volume to 0 and let the next real
            # volume update from the device correct it.
            logger.info("AirPlay volume: ignoring sentinel -144 dB")
            return
        # Map -30 dB → 0 %, 0 dB → 100 %  (clamped)
        pct = max(0, min(100, int((volume_db + 30) / 30 * 100)))
        self._state.set_source_volume("airplay", pct, muted=False)
        logger.info("AirPlay volume: %d%% (from %.2f dB)", pct, volume_db)

    def on_progress(self, start_ts: int, current_ts: int, stop_ts: int, sample_rate: int = 44100) -> None:
        if sample_rate <= 0:
            return
        duration_ms = int((stop_ts - start_ts) / sample_rate * 1000)
        progress_ms = int((current_ts - start_ts) / sample_rate * 1000)
        if self._is_active():
            self._state.duration_ms = max(0, duration_ms)
            self._state.progress_ms = max(0, min(progress_ms, duration_ms))
            self._state.progress_update_time = time.monotonic()
            self._state.playback_speed = 1.0
        else:
            self._state.write_to_snapshot("airplay",
                duration_ms=max(0, duration_ms),
                progress_ms=max(0, min(progress_ms, duration_ms)),
                progress_update_time=time.monotonic(),
                playback_speed=1.0,
            )

    def on_disconnect(self) -> None:
        self._state.airplay_connected = False
        self._state.connected = self._state.sendspin_connected
        if self._state.active_source == "airplay":
            # Save AirPlay state, switch to SendSpin, restore its snapshot
            self._state.save_snapshot("airplay")
            self._state.active_source = "sendspin" if self._state.sendspin_connected else ""
            if self._state.active_source:
                self._state.restore_snapshot(self._state.active_source)
            else:
                self._state.is_playing = False
                self._state.supported_commands = []


# ---------------------------------------------------------------------------
# Local transport control via audio child pipe
# ---------------------------------------------------------------------------

# AirPlay 2 has NO reverse command channel.  The event connection is
# receive-only (iPhone → receiver).  Next/prev/shuffle/repeat require
# the phone.  Play/pause is handled locally: we tell the audio child
# process to stop/start writing PCM to the speaker.
_AIRPLAY_SUPPORTED_COMMANDS = [
    "play", "pause",
    "volume",
]


class _RemoteControl:
    """Local playback control for AirPlay audio streams.

    Play/pause sends "pause" / "play-0" to the vendored audio child
    process via the multiprocessing pipe (same mechanism the RTSP
    handler uses for SETRATEANCHORTIME).  The iPhone keeps streaming;
    we just mute/unmute our speaker output.
    """

    def __init__(self) -> None:
        self._server: object | None = None  # AP2Server reference

    def set_server(self, server: object) -> None:
        self._server = server

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        pass

    def send_to_audio(self, msg: str) -> bool:
        """Send a control message to all active audio child processes."""
        server = self._server
        if server is None:
            logger.debug("RemoteControl: no server ref")
            return False
        streams = getattr(server, "streams", [])
        if not streams:
            logger.debug("RemoteControl: no active streams")
            return False
        sent = False
        for s in streams:
            try:
                conn = s.getAudioConnection()
                if conn is not None:
                    conn.send(msg)
                    sent = True
                    logger.info("RemoteControl: sent '%s' to audio child", msg)
                else:
                    logger.debug("RemoteControl: stream has no audio connection")
            except Exception:
                logger.debug("RemoteControl: audio pipe send failed", exc_info=True)
        return sent

    def close(self) -> None:
        self._server = None


# ---------------------------------------------------------------------------
# DMAP binary field extractor
# ---------------------------------------------------------------------------


def _extract_dmap_fields(data: bytes) -> dict[str, object]:
    """Extract DMAP fields from binary data into a dict.

    Returns e.g. ``{'minm': 'Song Title', 'asar': 'Artist', ...}``.
    Integer fields are returned as int, strings as str.
    """
    result: dict[str, object] = {}
    pos = 0
    while pos + 8 <= len(data):
        code = data[pos:pos + 4].decode("ascii", errors="replace")
        length = int.from_bytes(data[pos + 4:pos + 8], "big")
        value_bytes = data[pos + 8:pos + 8 + length]
        pos += 8 + length

        # Recurse into container types (mlit, msrv, mdcl)
        if code in ("mlit", "msrv", "mdcl"):
            result.update(_extract_dmap_fields(value_bytes))
            continue

        # String fields we care about
        if code in ("minm", "asar", "asal", "asaa", "asgn", "ascp"):
            try:
                result[code] = value_bytes.decode("utf-8")
            except UnicodeDecodeError:
                pass
        # Integer fields
        elif code in ("caps", "astm", "astn", "asdc", "asdn"):
            result[code] = int.from_bytes(value_bytes, "big") if value_bytes else 0
        # Artwork (PICT = raw image bytes in some legacy AirPlay senders)
        elif code == "PICT" and len(value_bytes) > 100:
            result["PICT"] = value_bytes

    return result


# ---------------------------------------------------------------------------
# Patched AP2Handler that calls our hooks
# ---------------------------------------------------------------------------


def _find_artwork(obj: object) -> bytes | None:
    """Recursively search a plist tree for artwork data.

    AirPlay 2 nests artwork under varying keys depending on iOS version:
      - ``kMRMediaRemoteNowPlayingInfoArtworkData``
      - ``artworkData``
    The key may appear at any depth, so we walk the entire tree.
    """
    _ARTWORK_KEYS = {
        "kMRMediaRemoteNowPlayingInfoArtworkData",
        "artworkData",
        "ArtworkData",
        "kMRMediaRemoteNowPlayingInfoArtworkDataBytes",
    }
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _ARTWORK_KEYS and isinstance(v, (bytes, bytearray)) and len(v) > 100:
                return bytes(v)
            found = _find_artwork(v)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_artwork(item)
            if found:
                return found
    return None


def _create_patched_handler(meta_hook: _MetadataHook, remote: _RemoteControl, config=None):
    """Import and return a subclass of AP2Handler with our hooks injected."""
    import http.server
    _patch_vendor_imports()

    # Now we can import the vendored modules
    from ap2_receiver import AP2Handler  # type: ignore[import-untyped]
    import plistlib
    # Use biplist (same as vendored code) for consistent binary plist parsing
    try:
        from biplist import readPlistFromString as _parse_plist
    except ImportError:
        _parse_plist = plistlib.loads  # fallback to stdlib

    class PatchedAP2Handler(AP2Handler):
        """AP2Handler with metadata hooks for AlfiePRIME integration."""

        _meta_hook = meta_hook
        _config = config

        def __init__(self, socket, client_address, server):
            # Replicate vendor's __init__ but suppress logger BEFORE
            # BaseHTTPRequestHandler.__init__ processes the first request.
            from threading import current_thread
            from ap2.utils import get_screen_logger as _get_logger
            server_address = socket.getsockname()
            pair_string = (
                f'{self.__class__.__name__}: '
                f'{server_address[0]}:{server_address[1]}'
                f'<=>{client_address[0]}:{client_address[1]}'
                f'; {current_thread().name}'
            )
            self.logger = _get_logger(pair_string, level='WARNING')
            self.logger.propagate = False
            # Now call BaseHTTPRequestHandler.__init__ which processes requests
            http.server.BaseHTTPRequestHandler.__init__(self, socket, client_address, server)

        def log_request(self, code="-", size="-"):
            """Suppress BaseHTTPRequestHandler stderr output."""
            pass

        def log_message(self, format, *args):
            """Redirect HTTP log messages to our file logger instead of stderr."""
            logger.debug(format, *args)

        def dispatch(self):
            """Override dispatch to log all requests and catch exceptions."""
            path = self.path.split("?")[0] if "?" in self.path else self.path
            logger.debug("AirPlay: %s %s from %s", self.command, path, self.client_address[0])
            try:
                super().dispatch()
            except Exception:
                logger.exception("AirPlay: handler crashed on %s %s", self.command, path)
                try:
                    self.send_error(500)
                except Exception:
                    pass

        # -- helpers --------------------------------------------------

        def _send_ok(self):
            self.send_response(200)
            self.send_header("Server", self.version_string())
            cseq = self.headers.get("CSeq")
            if cseq:
                self.send_header("CSeq", cseq)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_body(self) -> bytes:
            content_len = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(content_len) if content_len > 0 else b""

        def _extract_plist_metadata(self, plist: dict) -> None:
            """Try to extract Now Playing metadata from an AirPlay 2 plist."""
            # AirPlay 2 sends metadata in /command POSTs with MR keys
            # nested under params.params or directly at top level.
            info = plist
            if "params" in plist and isinstance(plist["params"], dict):
                info = plist["params"]
                if "params" in info and isinstance(info["params"], dict):
                    info = info["params"]

            # Standard MediaRemote NowPlayingInfo keys
            _MR = "kMRMediaRemoteNowPlayingInfo"
            title = info.get(f"{_MR}Title", "") or info.get("title", "")
            artist = info.get(f"{_MR}Artist", "") or info.get("artist", "")
            album = info.get(f"{_MR}Album", "") or info.get("album", "")
            duration = info.get(f"{_MR}Duration", 0) or info.get("duration", 0)
            elapsed = info.get(f"{_MR}ElapsedTime", 0) or info.get("elapsed", 0)
            rate = info.get(f"{_MR}PlaybackRate", None)

            if title or artist or album:
                self._meta_hook.on_metadata(
                    str(title) if title else "",
                    str(artist) if artist else "",
                    str(album) if album else "",
                )
            if duration:
                duration_ms = int(float(duration) * 1000)
                elapsed_ms = int(float(elapsed) * 1000) if elapsed else 0
                self._meta_hook._state.duration_ms = duration_ms
                self._meta_hook._state.progress_ms = elapsed_ms
                self._meta_hook._state.progress_update_time = time.monotonic()
                self._meta_hook._state.playback_speed = 1.0
            # Search the full plist tree for artwork (nesting varies by iOS version)
            artwork = _find_artwork(plist)
            if artwork:
                self._meta_hook.on_artwork(artwork)
            if rate is not None:
                self._meta_hook._set_playing(float(rate) > 0)

        # -- RTSP method overrides ------------------------------------

        def do_SET_PARAMETER(self):
            """Intercept metadata, artwork, volume, progress."""
            content_type = self.headers.get("Content-Type", "")
            body = self._read_body()

            if content_type == "text/parameters" and body:
                for line in body.split(b"\r\n"):
                    if not line:
                        continue
                    parts = line.split(b":", 1)
                    if len(parts) != 2:
                        continue
                    key = parts[0].strip()
                    val = parts[1].strip()
                    if key == b"volume":
                        try:
                            self._meta_hook.on_volume(float(val))
                        except ValueError:
                            pass
                    elif key == b"progress":
                        try:
                            nums = val.split(b"/")
                            if len(nums) == 3:
                                self._meta_hook.on_progress(
                                    int(nums[0]), int(nums[1]), int(nums[2]),
                                )
                                # Also forward to audio connections (parent's job)
                                for s in self.server.streams:
                                    try:
                                        s.getAudioConnection().send(
                                            f"progress-{val.decode('utf8').lstrip()}"
                                        )
                                    except Exception:
                                        pass
                        except ValueError:
                            pass
                self._send_ok()
                return

            elif content_type.startswith("image/") and body:
                self._meta_hook.on_artwork(body)
                self._send_ok()
                return

            elif content_type == "application/x-dmap-tagged" and body:
                try:
                    fields = _extract_dmap_fields(body)
                    logger.debug("AirPlay DMAP: %s", fields)
                    title = fields.get("minm", "")
                    artist = fields.get("asar", "")
                    album = fields.get("asal", "")
                    if title or artist or album:
                        self._meta_hook.on_metadata(
                            str(title), str(artist), str(album),
                        )
                    # Play state from DMAP caps field
                    caps = fields.get("caps")
                    if caps is not None:
                        self._meta_hook._set_playing(caps == 1)
                    # Duration from DMAP
                    astm = fields.get("astm")
                    if astm:
                        if self._meta_hook._is_active():
                            self._meta_hook._state.duration_ms = int(astm)
                        else:
                            self._meta_hook._state.write_to_snapshot("airplay", duration_ms=int(astm))
                    # Artwork from DMAP PICT field
                    pict = fields.get("PICT")
                    if pict and isinstance(pict, (bytes, bytearray)):
                        self._meta_hook.on_artwork(bytes(pict))
                except Exception:
                    logger.debug("Failed to parse DMAP metadata", exc_info=True)
                self._send_ok()
                return

            elif content_type == "application/x-apple-binary-plist" and body:
                # AirPlay 2 binary plist SET_PARAMETER
                try:
                    pl = _parse_plist(body)
                    logger.debug("AirPlay SET_PARAMETER bplist: %s",
                                 {k: v for k, v in pl.items()
                                  if not isinstance(v, (bytes, bytearray))})
                    self._extract_plist_metadata(pl)
                except Exception:
                    logger.debug("Failed to parse bplist SET_PARAMETER", exc_info=True)
                self._send_ok()
                return

            # Unknown content type — send 200 (body already consumed)
            self._send_ok()

        def do_SETRATEANCHORTIME(self):
            """Intercept play/pause rate changes."""
            body = self._read_body()
            if body:
                try:
                    pl = _parse_plist(body)
                    rate = pl.get("rate", None)
                    rtp_time = pl.get("rtpTime", 0)
                    logger.debug("AirPlay SETRATEANCHORTIME: rate=%s rtpTime=%s", rate, rtp_time)
                    if rate is not None:
                        if float(rate) > 0:
                            self._meta_hook._set_playing(True)
                            # Forward to audio connections
                            for s in self.server.streams:
                                try:
                                    s.getAudioConnection().send(f"play-{rtp_time}")
                                except Exception:
                                    pass
                        else:
                            self._meta_hook._set_playing(False)
                            for s in self.server.streams:
                                try:
                                    s.getAudioConnection().send("pause")
                                except Exception:
                                    pass
                except Exception:
                    logger.debug("Failed to parse SETRATEANCHORTIME", exc_info=True)
            self._send_ok()

        def handle_command(self):
            """Intercept AirPlay 2 /command POST with metadata."""
            body = self._read_body()
            if body:
                try:
                    pl = _parse_plist(body)
                    # Log without artwork (too large)
                    log_pl = {k: (f"<{len(v)} bytes>" if isinstance(v, (bytes, bytearray)) else v)
                              for k, v in pl.items()}
                    logger.debug("AirPlay /command: %s", log_pl)
                    self._extract_plist_metadata(pl)
                except Exception:
                    logger.debug("Failed to parse /command plist", exc_info=True)
            self._send_ok()

        def handle_feedback(self):
            """Handle /feedback — mostly stream status, forward to parent."""
            try:
                super().handle_feedback()
            except Exception:
                logger.debug("handle_feedback error", exc_info=True)
                self._send_ok()

        def do_SETUP(self):
            """Gate on swap prompt, then delegate to parent."""
            # Swap gating: if another source is active, check config
            state = self._meta_hook._state
            if state.active_source and state.active_source != "airplay":
                cfg = self._config
                client_ip = self.client_address[0]
                device_key = f"airplay:{client_ip}"
                # Auto-accept previously accepted devices
                if cfg and device_key in cfg.accepted_devices:
                    logger.info("Auto-accepting previously approved AirPlay device %s", client_ip)
                elif cfg and not cfg.swap_prompt:
                    if cfg.swap_auto_action == "deny":
                        logger.info("Auto-denying AirPlay connection (active=%s)", state.active_source)
                        self._send_ok()
                        return
                elif cfg and cfg.swap_prompt:
                    state.swap_pending = True
                    state.swap_pending_source = "airplay"
                    state.swap_pending_name = client_ip
                    state.swap_response = ""
                    # Block RTSP thread waiting for user response (up to 30s)
                    import time as _time
                    for _ in range(300):
                        if state.swap_response:
                            break
                        _time.sleep(0.1)
                    response = state.swap_response
                    state.swap_pending = False
                    state.swap_response = ""
                    if response != "accept":
                        logger.info("User denied AirPlay connection swap")
                        self._send_ok()
                        return
                    # Remember this device for future connections
                    if cfg and device_key not in cfg.accepted_devices:
                        cfg.accepted_devices.append(device_key)
                        cfg.save()

            super().do_SETUP()

        def do_RECORD(self):
            """Stream start — mark as connected but NOT playing yet.

            The actual play state arrives via SETRATEANCHORTIME (rate > 0)
            or DMAP metadata (caps == 1).  Setting is_playing=True here
            causes a false "playing" state when the iPhone connects paused.
            """
            state = self._meta_hook._state
            # Infrastructure state — always set
            state.airplay_connected = True
            state.connected = True
            if not state.active_source:
                state.active_source = "airplay"
            # Display state — mark codec/commands but leave is_playing
            # as-is until the device tells us the actual play state.
            if state.active_source == "airplay":
                state.codec = "airplay"
                state.supported_commands = list(_AIRPLAY_SUPPORTED_COMMANDS)
            else:
                state.write_to_snapshot("airplay",
                    codec="airplay",
                    supported_commands=list(_AIRPLAY_SUPPORTED_COMMANDS),
                )
            logger.info("AirPlay: stream RECORD — connected (waiting for play state)")
            try:
                super().do_RECORD()
            except Exception:
                logger.debug("do_RECORD error", exc_info=True)
                self._send_ok()

        def do_TEARDOWN(self):
            """Stream teardown — may be a pause (stream-level) or full disconnect.

            iPhone sends a TEARDOWN with specific ``streams`` in the plist
            body when pausing (stream-level).  A full disconnect sends an
            empty plist ``{}``.  The vendored handler culls the stream list
            in both cases, so we peek at the body *before* delegating to
            determine which kind this is.
            """
            logger.info("AirPlay: stream TEARDOWN")
            # Peek at the body to distinguish pause vs disconnect.
            # A stream-level teardown (pause) has {"streams": [...]}.
            # A full disconnect has an empty plist {}.
            stream_level = False
            original_rfile = self.rfile
            try:
                ct = self.headers.get("Content-Type", "")
                cl = int(self.headers.get("Content-Length", 0))
                if cl > 0 and "plist" in ct:
                    body = self.rfile.read(cl)
                    pl = _parse_plist(body)
                    stream_level = bool(pl.get("streams"))
                    # Put the body back so super() can read it
                    import io
                    self.rfile = io.BytesIO(body)
            except Exception:
                logger.debug("TEARDOWN: failed to peek at body", exc_info=True)

            # Protect event_proc from NoneType crash — the vendored
            # code does ``self.server.event_proc.terminate()`` on full
            # disconnect; if it's already None (from a prior teardown
            # or race) that crashes.
            if getattr(self.server, 'event_proc', None) is None:
                class _DummyProc:
                    def terminate(self): pass
                self.server.event_proc = _DummyProc()

            # For stream-level teardown, save event_proc so vendored
            # code can't kill it (pause should keep the session alive).
            saved_event_proc = self.server.event_proc

            try:
                super().do_TEARDOWN()
            except Exception:
                logger.debug("do_TEARDOWN error", exc_info=True)
                self._send_ok()
            finally:
                # Restore the real socket stream so subsequent RTSP
                # requests on this keep-alive connection still work.
                self.rfile = original_rfile

                if stream_level:
                    # Restore event_proc — pause must NOT kill it.
                    self.server.event_proc = saved_event_proc
                    logger.info("AirPlay: stream-level teardown (pause) — session alive")
                    self._meta_hook._set_playing(False)
                else:
                    logger.info("AirPlay: full teardown — client disconnected")
                    self._meta_hook.on_disconnect()

    return PatchedAP2Handler


# ---------------------------------------------------------------------------
# Main AirPlay receiver class
# ---------------------------------------------------------------------------


class AirPlayReceiver:
    """AirPlay 2 receiver that integrates with AlfiePRIME's TUI and visualizer.

    Runs the AP2 RTSP server in a background thread and feeds decoded audio
    into the shared AudioVisualizer.  Metadata and artwork updates are routed
    to the shared PlayerState.
    """

    def __init__(
        self,
        tui: BoomBoxTUI | None,
        visualizer: AudioVisualizer,
        *,
        device_name: str = "",
        port: int = 7000,
        config=None,
    ):
        self._tui = tui
        self._visualizer = visualizer
        if not device_name:
            device_name = f"Musiciser@{socket.gethostname()}"
        self._device_name = device_name
        self._port = port
        self._config = config
        self._running = False
        self._server: object | None = None
        self._server_thread: threading.Thread | None = None
        self._zeroconf: object | None = None
        self._mdns_services: list = []
        self._pin: str | None = None
        self._remote = _RemoteControl()
        self._original_command_cb: object | None = None
        self.__dj_mixer = None
        self._dj_feed_channel = "b"  # Which mixer channel this receiver feeds

    @property
    def _dj_mixer(self):
        return self.__dj_mixer

    @_dj_mixer.setter
    def _dj_mixer(self, mixer):
        self.__dj_mixer = mixer
        # Forward to PCM consumer if it exists
        if hasattr(self, "_pcm_consumer") and self._pcm_consumer is not None:
            self._pcm_consumer.dj_mixer = mixer
            self._pcm_consumer.dj_feed_channel = self._dj_feed_channel
            logger.info("AirPlay: DJ mixer propagated to PCM consumer (ch=%s, mixer=%s)",
                        self._dj_feed_channel, "ON" if mixer else "OFF")
        else:
            logger.warning("AirPlay: DJ mixer set but _pcm_consumer not available yet")

    @property
    def pin(self) -> str | None:
        """The current 4-digit pairing PIN, or None if not yet generated."""
        return self._pin

    @property
    def _state(self) -> PlayerState:
        if self._tui is not None:
            return self._tui.state
        # Fallback for daemon mode
        from alfieprime_musiciser.state import PlayerState
        if not hasattr(self, "_daemon_state"):
            self._daemon_state = PlayerState()
        return self._daemon_state

    def _on_airplay_command(self, command: str) -> None:
        """Route transport commands to the correct source."""
        state = self._state

        # Delegate to SendSpin handler when AirPlay is NOT active
        if state.active_source != "airplay":
            if self._original_command_cb:
                self._original_command_cb(command)
            return

        # Volume is always local and per-source.
        if command == "volume_up":
            cur_vol, cur_muted = state.get_source_volume("airplay")
            new_vol = min(100, cur_vol + 5)
            state.set_source_volume("airplay", new_vol, False if cur_muted else None)
            if cur_muted:
                logger.info("AirPlay unmuted via volume up")
            logger.info("AirPlay volume up: %d%%", new_vol)
            return
        elif command == "volume_down":
            cur_vol, cur_muted = state.get_source_volume("airplay")
            new_vol = max(0, cur_vol - 5)
            state.set_source_volume("airplay", new_vol, False if cur_muted else None)
            if cur_muted:
                logger.info("AirPlay unmuted via volume down")
            logger.info("AirPlay volume down: %d%%", new_vol)
            return
        elif command == "mute":
            cur_vol, cur_muted = state.get_source_volume("airplay")
            state.set_source_muted("airplay", not cur_muted)
            logger.info("AirPlay mute %s", "on" if not cur_muted else "off")
            return

        # Play/pause — local only: mute/unmute our speaker output.
        # The iPhone keeps streaming; we just stop/start writing PCM.
        if command == "play_pause":
            if state.is_playing:
                self._remote.send_to_audio("pause")
                state.is_playing = False
                state.playback_speed = 0.0
                self._visualizer.set_paused(True)
                logger.info("AirPlay: paused (local mute)")
            else:
                self._remote.send_to_audio("play-0")
                state.is_playing = True
                state.playback_speed = 1.0
                state.progress_update_time = time.monotonic()
                self._visualizer.set_paused(False)
                logger.info("AirPlay: resumed (local unmute)")
            return

        logger.debug("AirPlay: command '%s' not available (use phone controls)", command)

    async def start(self) -> None:
        """Start the AirPlay receiver in a background thread."""
        self._running = True
        loop = asyncio.get_running_loop()

        self._state.airplay_ready = True
        self._remote.set_loop(loop)

        # Intercept TUI command callback to route AirPlay commands
        if self._tui is not None:
            self._original_command_cb = getattr(self._tui, "_command_callback", None)
            self._tui.set_command_callback(self._on_airplay_command)

        logger.info("Starting AirPlay 2 receiver on port %d as '%s'", self._port, self._device_name)

        # Start the server in a thread (it's blocking)
        await loop.run_in_executor(None, self._run_server)

    def _run_server(self) -> None:
        """Blocking server start – runs in a thread."""
        # Enable file logging first so everything is captured
        setup_file_logging()

        _patch_vendor_imports()

        # Stub hexdump before importing ap2_receiver (only used in DEBUG)
        import importlib
        if importlib.util.find_spec("hexdump") is None:
            import types
            _hd_mod = types.ModuleType("hexdump")
            _hd_mod.hexdump = lambda *a, **kw: None  # type: ignore[attr-defined]
            sys.modules["hexdump"] = _hd_mod

        logger.info("AirPlay: importing dependencies...")
        try:
            import netifaces as ni
            logger.debug("AirPlay: netifaces loaded")
            from ap2_receiver import (  # type: ignore[import-untyped]
                AP2Server, setup_global_structs, register_mdns,
                get_screen_logger,
            )
            logger.debug("AirPlay: ap2_receiver loaded")
            import ap2_receiver as ap2mod  # type: ignore[import-untyped]
        except ImportError as exc:
            logger.error("AirPlay dependencies not available: %s", exc)
            return

        # The vendored ap2/utils.py calls logging.config.dictConfig() at import
        # time, which wipes our root handler.  Re-attach it now.
        _reattach_file_logging()

        # ── Find a network interface with an IPv4 address and MAC ──
        # Prefer real LAN interfaces (192.168.x.x) over VPN/virtual adapters.
        logger.info("AirPlay: scanning network interfaces...")
        candidates: list[tuple[str, str]] = []  # (iface_name, ipv4)
        seen_ips: set[str] = set()
        for name in ni.interfaces():
            addrs = ni.ifaddresses(name)
            logger.debug("  iface %s: families=%s", name, list(addrs.keys()))
            if ni.AF_INET in addrs:
                for addr in addrs[ni.AF_INET]:
                    ip = addr.get("addr", "")
                    logger.debug("    IPv4: %s", ip)
                    if ip and not ip.startswith("127.") and ip not in seen_ips:
                        candidates.append((name, ip))
                        seen_ips.add(ip)

        # Fallback: netifaces on Windows often misses adapters.  Use the
        # UDP-socket trick to find the default-route IP and also try
        # socket.getaddrinfo to discover IPs netifaces doesn't report.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            default_ip = s.getsockname()[0]
            s.close()
            if default_ip and default_ip not in seen_ips:
                logger.info("  default-route IP (socket probe): %s", default_ip)
                candidates.append(("_default", default_ip))
                seen_ips.add(default_ip)
        except Exception as exc:
            logger.debug("  UDP socket probe failed: %s", exc)

        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127.") and ip not in seen_ips:
                    logger.info("  getaddrinfo IP: %s", ip)
                    candidates.append(("_hostname", ip))
                    seen_ips.add(ip)
        except Exception as exc:
            logger.debug("  getaddrinfo fallback failed: %s", exc)

        if not candidates:
            logger.error("No suitable network interface found for AirPlay")
            return

        def _ip_priority(item: tuple[str, str]) -> int:
            """Lower = better. Prefer real LAN IPs over virtual/VPN."""
            ip = item[1]
            if ip.startswith("192.168."):
                return 0   # typical home/office LAN
            if ip.startswith("10.") and not ip.startswith("10.10."):
                return 1   # corporate LAN (but not Tailscale 10.10.x)
            if ip.startswith("10."):
                return 3   # likely Tailscale/VPN
            # 172.16-31.x.x = private (often Docker/Hyper-V/WSL)
            octets = ip.split(".")
            if len(octets) == 4 and octets[0] == "172":
                second = int(octets[1])
                if 16 <= second <= 31:
                    return 4   # virtual adapter
            return 2   # anything else

        candidates.sort(key=_ip_priority)
        for name, ip in candidates:
            logger.debug("  candidate: %s = %s (priority %d)", name, ip, _ip_priority((name, ip)))

        iface_name, ipv4_addr = candidates[0]
        logger.info("AirPlay: using interface %s (IPv4: %s)", iface_name, ipv4_addr)

        # Get MAC address — try the selected interface first, then scan all
        mac_addr = ""
        ipv6_addr = ""
        link_key = getattr(ni, "AF_LINK", getattr(ni, "AF_PACKET", 17))

        if not iface_name.startswith("_"):
            # Real netifaces interface
            ifen = ni.ifaddresses(iface_name)
            if ifen.get(link_key):
                mac_addr = ifen[link_key][0].get("addr", "")
            if ifen.get(ni.AF_INET6):
                ipv6_addr = ifen[ni.AF_INET6][0].get("addr", "").split("%")[0]
        else:
            # IP found via fallback — scan all interfaces for a MAC
            for probe_name in ni.interfaces():
                probe = ni.ifaddresses(probe_name)
                if ni.AF_INET in probe:
                    for a in probe[ni.AF_INET]:
                        if a.get("addr") == ipv4_addr and probe.get(link_key):
                            mac_addr = probe[link_key][0].get("addr", "")
                            if probe.get(ni.AF_INET6):
                                ipv6_addr = probe[ni.AF_INET6][0].get("addr", "").split("%")[0]
                            break
                if mac_addr:
                    break

        if not mac_addr:
            # Generate a fake MAC if we can't find one
            mac_addr = "AA:BB:CC:%02X:%02X:%02X" % (
                random.randint(0, 255), random.randint(0, 255), random.randint(0, 255),
            )
            logger.warning("Could not detect MAC address, using generated: %s", mac_addr)

        # Pack addresses to binary for mDNS registration
        ip4_bin = socket.inet_pton(socket.AF_INET, ipv4_addr)
        ip6_bin = socket.inet_pton(socket.AF_INET6, ipv6_addr) if ipv6_addr else None

        # ── Set all globals that ap2-receiver expects ──
        ap2mod.DEVICE_ID = mac_addr
        ap2mod.DEVICE_ID_BIN = int(mac_addr.replace(":", ""), base=16).to_bytes(6, "big")
        ap2mod.IPV4 = ipv4_addr
        ap2mod.IP4ADDR_BIN = ip4_bin
        ap2mod.IPV6 = ipv6_addr
        if ip6_bin:
            ap2mod.IP6ADDR_BIN = ip6_bin
        ap2mod.DEV_NAME = self._device_name
        ap2mod.DISABLE_VM = True   # We handle volume ourselves
        ap2mod.DISABLE_PTP_MASTER = False
        ap2mod.DEBUG = False

        # Set up the screen logger the vendored code expects
        ap2mod.SCR_LOG = get_screen_logger("AirPlay", level="INFO")

        # Create a mock argparse Namespace for setup_global_structs
        import argparse
        mock_args = argparse.Namespace(
            mdns=self._device_name,
            netiface=iface_name,
            no_volume_management=True,
            no_ptp_master=False,
            features=None,
            debug=False,
            fakemac=False,
        )
        # The vendored update_status_flags() references module-level `args`
        ap2mod.args = mock_args

        # Configure global data structures (device_info, mdns_props, etc.)
        logger.info("AirPlay: initialising global structs...")
        try:
            import ap2.pairing.hap as _hap_mod  # type: ignore[import-untyped]
            # Redirect pairing store to a proper data directory
            pairings_dir = os.path.join(_LOG_DIR, "pairings") + os.sep
            os.makedirs(pairings_dir, exist_ok=True)
            _hap_mod.PAIRING_STORE = pairings_dir
            logger.debug("AirPlay: pairing store: %s", pairings_dir)

            from ap2.pairing.hap import DeviceProperties  # type: ignore[import-untyped]
            ap2mod.DEV_PROPS = DeviceProperties(ap2mod.PI, False)
            logger.debug("AirPlay: DeviceProperties created with PI=%s", ap2mod.PI)
            setup_global_structs(mock_args, isDebug=False)

            # Transient pairing (Ft48) — iPhone connects without PIN prompt.
            # The vendored SRP uses the default password from DEV_PROPS
            # (defaults to "3939") for the transient handshake.
            self._pin = None
            self._state.airplay_pin = ""
            logger.info("AirPlay: transient pairing enabled (no PIN required)")

            logger.info("AirPlay: global structs ready")
        except Exception:
            logger.exception("Failed to setup AirPlay global structs")
            return

        # Log the mDNS properties that will be advertised
        mdns_props = getattr(ap2mod, 'mdns_props', {})
        logger.info("AirPlay: device_name=%s mac=%s ipv4=%s ipv6=%s",
                     self._device_name, mac_addr, ipv4_addr, ipv6_addr)
        logger.debug("AirPlay: mDNS TXT props: %s", {k: v for k, v in mdns_props.items() if k != 'pk'})

        # Create metadata hook
        meta_hook = _MetadataHook(self._state, visualizer=self._visualizer)

        # Create patched handler
        HandlerClass = _create_patched_handler(meta_hook, self._remote, config=self._config)

        # Tell child processes where to log so their output reaches our file.
        os.environ["AIRPLAY_DEBUG_LOG"] = _LOG_FILE

        # Create a multiprocessing queue for PCM audio from child processes.
        # The vendored audio.py writes decoded PCM to this queue; a consumer
        # thread in the parent feeds it to the visualizer.
        pcm_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=64)
        pcm_consumer = _PCMConsumer(pcm_queue, self._visualizer, state=self._state)
        self._pcm_consumer = pcm_consumer
        # Propagate DJ mixer if it was already set before pcm_consumer existed
        if self.__dj_mixer is not None:
            pcm_consumer.dj_mixer = self.__dj_mixer
            pcm_consumer.dj_feed_channel = self._dj_feed_channel
        pcm_consumer.start()

        # Monkey-patch EventGeneric.spawn to use a thread instead of a
        # subprocess (avoids multiprocessing issues on some platforms).
        from ap2.connections.event import EventGeneric  # type: ignore[import-untyped]

        @staticmethod
        def _patched_event_spawn(addr=None, port=None, name='events', shared_key=None, isDebug=False):
            """Thread-based event listener (read-only — receives from iPhone)."""
            event = EventGeneric(addr, port, name, shared_key, isDebug)

            def _serve():
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((event.addr, event.port))
                sock.listen(1)
                try:
                    conn, peer = sock.accept()
                    logger.info("Event connection (%s) from %s:%d", name, peer[0], peer[1])
                    try:
                        while True:
                            data = conn.recv(4096)
                            if not data:
                                break
                    except (OSError, ConnectionError):
                        pass
                    finally:
                        conn.close()
                        logger.info("Event connection (%s) closed", name)
                except (OSError, KeyboardInterrupt):
                    pass
                finally:
                    sock.close()

            t = threading.Thread(target=_serve, daemon=True)
            t.terminate = lambda: None  # type: ignore[attr-defined]
            t.start()
            return event.port, t

        EventGeneric.spawn = _patched_event_spawn

        # Monkey-patch Stream so every audio child process gets the queue.
        from ap2.connections.stream import Stream  # type: ignore[import-untyped]
        _original_stream_init = Stream.__init__

        def _patched_stream_init(self_stream, *args, **kwargs):
            kwargs.setdefault("pcm_queue", pcm_queue)
            logger.debug("Stream.__init__ patched: pcm_queue=%r injected", pcm_queue)
            _original_stream_init(self_stream, *args, **kwargs)

        Stream.__init__ = _patched_stream_init

        # ── Register mDNS: both _airplay._tcp AND _raop._tcp ──
        # iPhones need BOTH services to show the device in the AirPlay list.
        # Advertise ALL routable IPs so the iPhone can reach us regardless of
        # which interface it routes through.
        logger.info("AirPlay: registering mDNS services on port %d...", self._port)
        all_ips: set[str] = set()
        for _name, _ip in candidates:
            all_ips.add(_ip)
        # Also include any IPs we know about
        all_ips.discard("127.0.0.1")
        addresses: list[bytes] = []
        for ip in sorted(all_ips, key=lambda x: _ip_priority(("", x))):
            try:
                addresses.append(socket.inet_pton(socket.AF_INET, ip))
            except OSError:
                pass
        if ip6_bin:
            addresses.append(ip6_bin)
        logger.debug("AirPlay: mDNS addresses: %s", [socket.inet_ntoa(a) if len(a) == 4 else a.hex() for a in addresses])

        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf  # type: ignore[import-untyped]

            mdns_props = getattr(ap2mod, 'mdns_props', {})

            # 1) _airplay._tcp service (AirPlay 2)
            airplay_info = ServiceInfo(
                "_airplay._tcp.local.",
                f"{self._device_name}._airplay._tcp.local.",
                addresses=addresses,
                port=self._port,
                properties=mdns_props,
                server=f"{mac_addr.replace(':', '')}@{self._device_name}._airplay.local.",
            )

            # 2) _raop._tcp service (RAOP — required for iPhone discovery)
            # RAOP name format: <MAC_NO_COLONS>@<DeviceName>._raop._tcp.local.
            mac_clean = mac_addr.replace(":", "")
            raop_name = f"{mac_clean}@{self._device_name}"
            raop_info = ServiceInfo(
                "_raop._tcp.local.",
                f"{raop_name}._raop._tcp.local.",
                addresses=addresses,
                port=self._port,
                properties=mdns_props,
                server=f"{mac_clean}@{self._device_name}._raop.local.",
            )

            zc = Zeroconf(ip_version=IPVersion.V4Only)
            self._zeroconf = zc

            zc.register_service(airplay_info, allow_name_change=True)
            logger.info("AirPlay: mDNS registered _airplay._tcp ─ name=%s server=%s port=%d",
                        airplay_info.name, airplay_info.server, airplay_info.port)

            zc.register_service(raop_info, allow_name_change=True)
            logger.info("AirPlay: mDNS registered _raop._tcp ─ name=%s server=%s port=%d",
                        raop_info.name, raop_info.server, raop_info.port)

            # Store for cleanup
            self._mdns_services = [airplay_info, raop_info]
            # Also set the module global so vendored code can update it
            ap2mod.MDNS_OBJ = (zc, airplay_info)

        except Exception:
            logger.warning("mDNS registration FAILED — AirPlay discovery will not work", exc_info=True)

        # Update state — mark as waiting for AirPlay client (not fully "connected" yet)
        self._state.airplay_server_name = self._device_name

        # Bind to 0.0.0.0 so the server accepts connections from ANY interface.
        # The mDNS registration advertises the specific IPs, but the RTSP server
        # must be reachable from all of them (especially when netifaces picks a
        # Hyper-V/WSL virtual adapter but the iPhone reaches us via the real LAN).
        bind_addr = "0.0.0.0"
        logger.info("AirPlay: waiting for client connections on %s:%d (mDNS advertises %s)",
                     bind_addr, self._port, ipv4_addr)

        # Start RTSP server
        try:
            self._server = AP2Server((bind_addr, self._port), HandlerClass)
            # Mark server socket non-inheritable so forked audio child
            # processes don't hold the port open after we exit.
            try:
                self._server.socket.set_inheritable(False)
            except (AttributeError, OSError):
                pass
            # Give _RemoteControl access to server.streams for local play/pause
            self._remote.set_server(self._server)
            # Suppress the server's vendor logger from writing to the console
            self._server.logger.setLevel(logging.WARNING)
            self._server.logger.propagate = False
            # Mark server as ready (listening) — actual connected=True happens
            # in do_RECORD when a client starts streaming.
            self._state.airplay_ready = True
            logger.info("AirPlay: RTSP server started on %s:%d — ready for connections",
                        bind_addr, self._port)
            self._server.serve_forever()
        except Exception:
            logger.exception("AirPlay server error")
        finally:
            pcm_consumer.stop()
            self._running = False
            self._state.airplay_connected = False
            self._state.connected = self._state.sendspin_connected
            self._state.airplay_ready = False

    def stop(self) -> None:
        """Shut down the AirPlay server and unregister mDNS."""
        self._running = False
        self._remote.close()
        if hasattr(self, "_pcm_consumer"):
            self._pcm_consumer.stop()

        # Unregister mDNS so the device disappears from AirPlay lists
        if self._zeroconf is not None:
            try:
                self._zeroconf.unregister_all_services()
                self._zeroconf.close()
            except Exception:
                pass
            self._zeroconf = None

        if self._server is not None:
            srv = self._server
            self._server = None

            # 1) Tear down every stream — kills audio & control child processes
            for s in list(getattr(srv, "streams", [])):
                try:
                    s.teardown()
                except Exception:
                    pass
                # Force-kill if the child didn't exit within 2 seconds
                dp = getattr(s, "data_proc", None)
                if dp is not None and hasattr(dp, "is_alive") and dp.is_alive():
                    try:
                        dp.kill()
                    except Exception:
                        pass
                cp = getattr(s, "control_proc", None)
                if cp is not None and hasattr(cp, "is_alive") and cp.is_alive():
                    try:
                        cp.kill()
                    except Exception:
                        pass
            getattr(srv, "streams", []).clear()
            getattr(srv, "sessions", []).clear()

            # 2) Close all active client sockets — unblocks handler threads
            #    that are stuck on rfile.read()
            for addr, sock in list(getattr(srv, "connections", {}).items()):
                try:
                    sock.close()
                except Exception:
                    pass
            getattr(srv, "connections", {}).clear()

            # 3) Terminate event/timing threads
            for attr in ("event_proc", "timing_proc"):
                proc = getattr(srv, attr, None)
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    setattr(srv, attr, None)

            # 4) Shut down the TCPServer (signals serve_forever to stop)
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass

        # Clear pairing state so devices can reconnect on next start.
        # Transient pairing (Ft48) means clients re-pair each session anyway,
        # but stale files cause an LTSK/DeviceProperties mismatch that forces
        # a double-restart.  Always clear client pairings; optionally clear
        # the server keypair too (forget_airplay_devices setting).
        self._clear_pairing_store()

        logger.info("AirPlay receiver stopped")

    def _clear_pairing_store(self) -> None:
        """Remove HAP pairing files so next startup gets a clean handshake."""
        pairings_dir = os.path.join(_LOG_DIR, "pairings")
        if not os.path.isdir(pairings_dir):
            return
        forget_all = getattr(self._config, "forget_airplay_devices", False)
        try:
            for fname in os.listdir(pairings_dir):
                fpath = os.path.join(pairings_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                # Always clear client pairings and device props (fixes
                # the double-restart bug).  Only clear the LTSK (server
                # identity) when "forget devices" is enabled.
                if fname == "ltsk.txt" and not forget_all:
                    continue
                os.remove(fpath)
                logger.debug("AirPlay: removed pairing file %s", fname)
        except Exception:
            logger.debug("AirPlay: failed to clear pairing store", exc_info=True)
        # Also clear the remembered-devices list so swap prompt re-appears
        if forget_all and self._config is not None:
            if self._config.accepted_devices:
                self._config.accepted_devices.clear()
                self._config.save()
                logger.debug("AirPlay: cleared accepted_devices list")
