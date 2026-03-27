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
    """
    global _file_logging_active, _file_handler
    if _file_logging_active:
        return _LOG_FILE
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
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
        sample_rate: int = 44100,
        sample_size: int = 16,
        channels: int = 2,
    ):
        self._queue = pcm_queue
        self._visualizer = visualizer
        self.sample_rate = sample_rate
        self.sample_size = sample_size
        self.channels = channels
        self._running = False
        self._thread: threading.Thread | None = None

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
                self._visualizer.set_format(
                    self.sample_rate, self.sample_size, self.channels,
                )
                self._visualizer.feed_audio(data, immediate=True)
            except Exception:
                logger.debug("PCM consumer feed error", exc_info=True)


# ---------------------------------------------------------------------------
# Metadata / artwork callback holder
# ---------------------------------------------------------------------------


class _MetadataHook:
    """Receives metadata + artwork from the AP2Handler and updates PlayerState."""

    def __init__(self, state: PlayerState):
        self._state = state
        self._last_title = ""

    def on_metadata(self, title: str, artist: str, album: str) -> None:
        s = self._state
        old_title = s.title
        if title:
            s.title = title
        if artist:
            s.artist = artist
        if album:
            s.album = album
        s.is_playing = True
        s.connected = True

        if s.title != old_title and s.title:
            logger.info("AirPlay now playing: %s - %s [%s]", s.artist, s.title, s.album)

    def on_artwork(self, data: bytes) -> None:
        if data:
            self._state.artwork_data = data
            logger.info("AirPlay artwork received (%d bytes)", len(data))

    def on_volume(self, volume_db: float) -> None:
        # AirPlay volume is -144..0 dB (linear-ish). Map to 0-100.
        if volume_db <= -144:
            self._state.volume = 0
            self._state.muted = True
        else:
            # -30 dB → 0%, 0 dB → 100%
            pct = max(0, min(100, int((volume_db + 30) / 30 * 100)))
            self._state.volume = pct
            self._state.muted = False

    def on_progress(self, start_ts: int, current_ts: int, stop_ts: int, sample_rate: int = 44100) -> None:
        if sample_rate <= 0:
            return
        duration_ms = int((stop_ts - start_ts) / sample_rate * 1000)
        progress_ms = int((current_ts - start_ts) / sample_rate * 1000)
        self._state.duration_ms = max(0, duration_ms)
        self._state.progress_ms = max(0, min(progress_ms, duration_ms))
        self._state.progress_update_time = time.monotonic()
        self._state.playback_speed = 1.0

    def on_disconnect(self) -> None:
        self._state.is_playing = False
        self._state.connected = False
        self._state.supported_commands = []
        if self._state.active_source == "airplay":
            self._state.active_source = ""


# ---------------------------------------------------------------------------
# DACP client — sends transport commands back to the AirPlay sender
# ---------------------------------------------------------------------------

# Standard DACP commands the iPhone always supports
_AIRPLAY_SUPPORTED_COMMANDS = [
    "play", "pause", "next", "previous",
    "shuffle", "unshuffle",
    "repeat_all", "repeat_one", "repeat_off",
    "volume",
]


class _DACPClient:
    """Send DACP commands to the AirPlay sender (iPhone).

    The sender advertises a ``_touch-able._tcp`` mDNS service whose port
    accepts plain-HTTP GET requests authenticated by the ``Active-Remote``
    token received during RTSP SETUP.
    """

    def __init__(self) -> None:
        self._active_remote: str = ""
        self._dacp_id: str = ""
        self._sender_ip: str = ""
        self._dacp_port: int = 0
        self._browser: object | None = None

    # -- discovery ----------------------------------------------------------

    def set_sender_info(self, ip: str, dacp_id: str, active_remote: str) -> None:
        self._sender_ip = ip
        self._dacp_id = dacp_id
        self._active_remote = active_remote
        logger.info("DACP: sender=%s dacp_id=%s active_remote=%s",
                     ip, dacp_id, active_remote)
        # Start mDNS browse for the sender's DACP service
        self._browse_for_dacp()

    def _browse_for_dacp(self) -> None:
        """Browse mDNS for _touch-able._tcp matching our DACP-ID."""
        try:
            from zeroconf import ServiceBrowser, Zeroconf  # type: ignore[import-untyped]

            class _Listener:
                def __init__(self, client: _DACPClient):
                    self._client = client

                def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    info = zc.get_service_info(type_, name)
                    if info is None:
                        return
                    # Match by DACP-ID in the service name or TXT properties
                    dacp_match = self._client._dacp_id.upper().replace("-", "")
                    name_clean = name.upper().replace("-", "")
                    if dacp_match and dacp_match in name_clean:
                        self._client._dacp_port = info.port
                        logger.info("DACP: found sender service on port %d", info.port)
                    elif not self._client._dacp_port and info.parsed_addresses():
                        # Fallback: match by IP
                        for addr in info.parsed_addresses():
                            if addr == self._client._sender_ip:
                                self._client._dacp_port = info.port
                                logger.info("DACP: matched sender by IP on port %d", info.port)
                                break

                def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    pass

                def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    pass

            zc = Zeroconf()
            self._browser = ServiceBrowser(zc, "_touch-able._tcp.local.", _Listener(self))
            logger.info("DACP: browsing for _touch-able._tcp services")
        except Exception:
            logger.debug("DACP: mDNS browse failed", exc_info=True)

    @property
    def available(self) -> bool:
        return bool(self._active_remote and self._sender_ip and self._dacp_port)

    # -- command sending ----------------------------------------------------

    def _send(self, path: str) -> bool:
        """Send a DACP GET request. Returns True on success."""
        if not self._sender_ip or not self._active_remote:
            return False
        port = self._dacp_port
        if not port:
            # Fallback: try common DACP ports
            port = 3689
        try:
            import http.client
            conn = http.client.HTTPConnection(
                self._sender_ip, port, timeout=2,
            )
            conn.request("GET", path, headers={
                "Active-Remote": self._active_remote,
                "Host": f"{self._sender_ip}:{port}",
            })
            resp = conn.getresponse()
            conn.close()
            logger.debug("DACP: %s -> %d", path, resp.status)
            return 200 <= resp.status < 300
        except Exception:
            logger.debug("DACP: %s failed", path, exc_info=True)
            return False

    def play_pause(self) -> bool:
        return self._send("/ctrl-int/1/playpause")

    def next_track(self) -> bool:
        return self._send("/ctrl-int/1/nextitem")

    def prev_track(self) -> bool:
        return self._send("/ctrl-int/1/previtem")

    def set_volume(self, volume_pct: int) -> bool:
        """Set volume (0-100 maps to DACP 0.0-100.0)."""
        vol = max(0.0, min(100.0, float(volume_pct)))
        return self._send(f"/ctrl-int/1/setproperty?dmcp.volume={vol:.1f}")

    def set_shuffle(self, on: bool) -> bool:
        return self._send(f"/ctrl-int/1/setproperty?dacp.shufflestate={1 if on else 0}")

    def set_repeat(self, mode: str) -> bool:
        """mode: 'off'=0, 'one'=1, 'all'=2"""
        val = {"off": 0, "one": 1, "all": 2}.get(mode, 0)
        return self._send(f"/ctrl-int/1/setproperty?dacp.repeatstate={val}")


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

    return result


# ---------------------------------------------------------------------------
# Patched AP2Handler that calls our hooks
# ---------------------------------------------------------------------------


def _create_patched_handler(meta_hook: _MetadataHook, dacp_client: _DACPClient, config=None):
    """Import and return a subclass of AP2Handler with our hooks injected."""
    _patch_vendor_imports()

    # Now we can import the vendored modules
    from ap2_receiver import AP2Handler  # type: ignore[import-untyped]
    import plistlib

    class PatchedAP2Handler(AP2Handler):
        """AP2Handler with metadata hooks for AlfiePRIME integration."""

        _meta_hook = meta_hook
        _dacp_client = dacp_client
        _config = config

        def dispatch(self):
            """Override dispatch to log all requests and catch exceptions."""
            path = self.path.split("?")[0] if "?" in self.path else self.path
            logger.info("AirPlay: %s %s from %s", self.command, path, self.client_address[0])
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
            artwork = info.get(f"{_MR}ArtworkData", b"") or info.get("artworkData", b"")
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
            if artwork and isinstance(artwork, (bytes, bytearray)) and len(artwork) > 0:
                self._meta_hook.on_artwork(bytes(artwork))
            if rate is not None:
                self._meta_hook._state.is_playing = float(rate) > 0

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
                        self._meta_hook._state.is_playing = (caps == 1)
                    # Duration from DMAP
                    astm = fields.get("astm")
                    if astm:
                        self._meta_hook._state.duration_ms = int(astm)
                except Exception:
                    logger.debug("Failed to parse DMAP metadata", exc_info=True)
                self._send_ok()
                return

            elif content_type == "application/x-apple-binary-plist" and body:
                # AirPlay 2 binary plist SET_PARAMETER
                try:
                    pl = plistlib.loads(body)
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
                    pl = plistlib.loads(body)
                    rate = pl.get("rate", None)
                    rtp_time = pl.get("rtpTime", 0)
                    logger.debug("AirPlay SETRATEANCHORTIME: rate=%s rtpTime=%s", rate, rtp_time)
                    if rate is not None:
                        if float(rate) > 0:
                            self._meta_hook._state.is_playing = True
                            # Forward to audio connections
                            for s in self.server.streams:
                                try:
                                    s.getAudioConnection().send(f"play-{rtp_time}")
                                except Exception:
                                    pass
                        else:
                            self._meta_hook._state.is_playing = False
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
                    pl = plistlib.loads(body)
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
            """Capture DACP headers, gate on swap prompt, then delegate to parent."""
            dacp_id = self.headers.get("DACP-ID", "")
            active_remote = self.headers.get("Active-Remote", "")
            if dacp_id and active_remote:
                client_ip = self.client_address[0]
                self._dacp_client.set_sender_info(client_ip, dacp_id, active_remote)

            # Swap gating: if another source is active, check config
            state = self._meta_hook._state
            if state.active_source and state.active_source != "airplay":
                cfg = self._config
                if cfg and not cfg.swap_prompt:
                    if cfg.swap_auto_action == "deny":
                        logger.info("Auto-denying AirPlay connection (active=%s)", state.active_source)
                        self._send_ok()
                        return
                elif cfg and cfg.swap_prompt:
                    client_ip = self.client_address[0]
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

            super().do_SETUP()

        def do_RECORD(self):
            """Stream start — mark as playing."""
            self._meta_hook._state.is_playing = True
            self._meta_hook._state.connected = True
            self._meta_hook._state.active_source = "airplay"
            self._meta_hook._state.codec = "airplay"
            # Populate supported commands so TUI buttons are active
            self._meta_hook._state.supported_commands = list(_AIRPLAY_SUPPORTED_COMMANDS)
            logger.info("AirPlay: stream RECORD — playback starting")
            try:
                super().do_RECORD()
            except Exception:
                logger.debug("do_RECORD error", exc_info=True)
                self._send_ok()

        def do_TEARDOWN(self):
            """Stream teardown — mark as disconnected."""
            logger.info("AirPlay: stream TEARDOWN — client disconnecting")
            try:
                super().do_TEARDOWN()
            except Exception:
                logger.debug("do_TEARDOWN error", exc_info=True)
                self._send_ok()
            finally:
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
        device_name: str = "AlfiePRIME Musiciser",
        port: int = 7000,
        config=None,
    ):
        self._tui = tui
        self._visualizer = visualizer
        self._device_name = device_name
        self._port = port
        self._config = config
        self._running = False
        self._server: object | None = None
        self._server_thread: threading.Thread | None = None
        self._zeroconf: object | None = None
        self._mdns_services: list = []
        self._pin: str | None = None
        self._dacp = _DACPClient()
        self._original_command_cb: object | None = None

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
        """Handle transport command when AirPlay is the active source."""
        state = self._state
        dacp = self._dacp

        # If AirPlay is not the active source, delegate to the original callback
        if not state.connected or state.active_source != "airplay" or not dacp.available:
            if self._original_command_cb:
                self._original_command_cb(command)
            return

        # Volume — send via DACP to change on the sender
        if command == "volume_up":
            new_vol = min(100, state.volume + 5)
            state.volume = new_vol
            dacp.set_volume(new_vol)
            return
        elif command == "volume_down":
            new_vol = max(0, state.volume - 5)
            state.volume = new_vol
            dacp.set_volume(new_vol)
            return

        # Transport — run in a thread to avoid blocking the TUI input thread
        def _send():
            try:
                if command == "play_pause":
                    dacp.play_pause()
                elif command == "next":
                    dacp.next_track()
                elif command == "previous":
                    dacp.prev_track()
                elif command == "shuffle":
                    new_state = not state.shuffle
                    if dacp.set_shuffle(new_state):
                        state.shuffle = new_state
                elif command == "repeat":
                    cycle = {"off": "all", "all": "one", "one": "off"}
                    new_mode = cycle.get(state.repeat_mode, "off")
                    if dacp.set_repeat(new_mode):
                        state.repeat_mode = new_mode
            except Exception:
                logger.debug("DACP command '%s' failed", command, exc_info=True)

        threading.Thread(target=_send, daemon=True).start()

    async def start(self) -> None:
        """Start the AirPlay receiver in a background thread."""
        self._running = True
        loop = asyncio.get_running_loop()

        self._state.airplay_ready = True

        # Intercept TUI command callback to route AirPlay commands via DACP
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
        meta_hook = _MetadataHook(self._state)

        # Create patched handler
        HandlerClass = _create_patched_handler(meta_hook, self._dacp, config=self._config)

        # Tell child processes where to log so their output reaches our file.
        os.environ["AIRPLAY_DEBUG_LOG"] = _LOG_FILE

        # Create a multiprocessing queue for PCM audio from child processes.
        # The vendored audio.py writes decoded PCM to this queue; a consumer
        # thread in the parent feeds it to the visualizer.
        pcm_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=64)
        pcm_consumer = _PCMConsumer(pcm_queue, self._visualizer)
        pcm_consumer.start()

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
        self._state.server_name = f"AirPlay: {self._device_name}"

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
            self._pcm_consumer = pcm_consumer
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
            self._state.connected = False
            self._state.airplay_ready = False

    def stop(self) -> None:
        """Shut down the AirPlay server and unregister mDNS."""
        self._running = False
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
            try:
                self._server.shutdown()
            except Exception:
                pass
        logger.info("AirPlay receiver stopped")
