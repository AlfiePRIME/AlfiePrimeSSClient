"""Spotify Connect receiver bridge.

Spawns librespot as a subprocess with ``--backend pipe`` so raw PCM arrives
on stdout.  Event callbacks from librespot (play/pause/track change/volume)
are received through a named pipe and provide basic playback state.

The Spotify Web API (via spotipy) is **entirely optional** — when configured
it adds rich metadata (title, artist, album, artwork) and TUI transport
controls (next/prev/shuffle/repeat).  Without it, librespot still works as
a fully functional Spotify Connect receiver with audio and basic state.

Architecture mirrors the AirPlay receiver: external process bridged into the
AlfiePRIME audio + metadata pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alfieprime_musiciser.config import Config
    from alfieprime_musiciser.state import PlayerState
    from alfieprime_musiciser.tui import BoomBoxTUI
    from alfieprime_musiciser.visualizer import AudioVisualizer

logger = logging.getLogger(__name__)

# Cache/credential paths
_CACHE_DIR = Path.home() / ".cache" / "alfieprime" / "spotify"
_LIBRESPOT_CACHE = _CACHE_DIR / "librespot"

# Module-level flag for DJ mixer state
_dj_mixer_active = False


# ---------------------------------------------------------------------------
# PCM reader thread — reads raw S16LE stereo from librespot stdout
# ---------------------------------------------------------------------------


class _PCMReader:
    """Reads raw PCM (S16LE 44100 Hz stereo) from librespot's stdout pipe.

    Mirrors the AirPlay _PCMConsumer pattern: feeds either the DJ mixer or
    the master visualizer depending on mode.
    """

    SAMPLE_RATE = 44100
    SAMPLE_SIZE = 16
    CHANNELS = 2
    FRAME_SIZE = 2 * 2  # 16-bit × 2 channels = 4 bytes per frame
    CHUNK_FRAMES = 1024  # read this many frames at a time
    CHUNK_BYTES = CHUNK_FRAMES * FRAME_SIZE

    # Bytes per millisecond of audio (44100 Hz × 4 bytes/frame = 176400 B/s)
    BYTES_PER_MS = SAMPLE_RATE * FRAME_SIZE / 1000.0

    def __init__(self, visualizer: AudioVisualizer, state: PlayerState | None = None):
        self._visualizer = visualizer
        self._state = state
        self._running = False
        self._thread: threading.Thread | None = None
        self._pipe = None  # stdout file object from subprocess
        self.dj_mixer = None
        self.dj_feed_channel = "a"
        # Progress tracking from PCM bytes consumed
        self._bytes_consumed = 0
        self._progress_base_ms = 0  # starting offset for current track
        # Real-time pacing — reset on track change
        self._pacing_reset = False

    def start(self, pipe) -> None:
        self._pipe = pipe
        self._running = True
        self._bytes_consumed = 0
        self._progress_base_ms = 0
        self._pacing_reset = False
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        logger.info("Spotify PCM reader started (44100 Hz S16LE stereo)")

    def stop(self) -> None:
        self._running = False

    def reset_progress(self, base_ms: int = 0) -> None:
        """Reset progress counter (call on track change)."""
        self._bytes_consumed = 0
        self._progress_base_ms = base_ms
        self._pacing_reset = True

    @property
    def progress_ms(self) -> int:
        """Current playback progress derived from PCM bytes consumed."""
        return self._progress_base_ms + int(self._bytes_consumed / self.BYTES_PER_MS)

    def _read_loop(self) -> None:
        """Read PCM from librespot stdout, paced to real-time.

        librespot's pipe backend has no playback clock — it decodes and
        writes audio as fast as possible.  Without pacing, the entire
        track is consumed in ~1 second and Spotify skips to the next.

        We throttle reads to real-time speed so the pipe provides natural
        backpressure to librespot, matching the behavior of a real audio
        sink.
        """
        pipe = self._pipe
        chunk_size = self.CHUNK_BYTES
        bytes_per_sec = self.SAMPLE_RATE * self.FRAME_SIZE  # 176400 B/s
        start_time = time.monotonic()
        total_bytes = 0

        while self._running and pipe is not None:
            try:
                data = pipe.read(chunk_size)
                if not data:
                    break
            except (OSError, ValueError):
                break

            total_bytes += len(data)
            self._bytes_consumed += len(data)

            # Reset pacing clock on track change
            if self._pacing_reset:
                self._pacing_reset = False
                start_time = time.monotonic()
                total_bytes = len(data)

            # ── Real-time pacing ──
            # Calculate how far ahead of real-time we are and sleep
            # the difference.  This keeps reads at ~44100 Hz stereo S16
            # speed, providing backpressure via the pipe buffer so
            # librespot doesn't race through the track.
            expected_time = total_bytes / bytes_per_sec
            elapsed = time.monotonic() - start_time
            ahead = expected_time - elapsed
            if ahead > 0.005:  # only sleep if >5ms ahead
                time.sleep(ahead)

            # Update progress on state periodically (every ~250ms of audio)
            state = self._state
            if state and self._bytes_consumed % (chunk_size * 11) < chunk_size:
                progress = self.progress_ms
                if state.active_source == "spotify" or state.active_source == "":
                    state.progress_ms = progress
                    state.progress_update_time = time.monotonic()
                    state.playback_speed = 1.0

            mixer = self.dj_mixer
            if mixer is not None:
                if self.dj_feed_channel == "a":
                    mixer.set_format_a(self.SAMPLE_RATE, self.SAMPLE_SIZE, self.CHANNELS)
                    mixer.feed_a(data)
                else:
                    mixer.set_format_b(self.SAMPLE_RATE, self.SAMPLE_SIZE, self.CHANNELS)
                    mixer.feed_b(data)
                if state and not state.is_playing:
                    state.is_playing = True
                continue

            # Only feed master visualizer when Spotify is active source
            if state and state.active_source != "spotify":
                continue

            self._visualizer.set_format(self.SAMPLE_RATE, self.SAMPLE_SIZE, self.CHANNELS)
            if self._visualizer._paused:
                self._visualizer.set_paused(False)
            if state and not state.is_playing:
                state.is_playing = True
            self._visualizer.feed_audio(data, immediate=True)

        logger.info("Spotify PCM reader stopped")


# ---------------------------------------------------------------------------
# Spotify Web API wrapper
# ---------------------------------------------------------------------------


class _SpotifyAPI:
    """Thin wrapper around spotipy for metadata + transport controls.

    Uses PKCE auth (no client secret needed — just a client ID).
    """

    SCOPE = (
        "user-read-playback-state "
        "user-modify-playback-state "
        "user-read-currently-playing"
    )
    REDIRECT_URI = "http://127.0.0.1:8421/callback"

    def __init__(self, client_id: str, cache_dir: Path | None = None):
        self._client_id = client_id
        self._cache_dir = cache_dir or _CACHE_DIR
        self._sp = None
        self._token_info = None

    def authenticate(self) -> bool:
        """Run PKCE OAuth flow. Returns True on success."""
        if not self._client_id:
            logger.warning("Spotify: no client_id configured")
            return False
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyPKCE

            cache_path = self._cache_dir / "token_cache.json"
            self._cache_dir.mkdir(parents=True, exist_ok=True)

            auth_manager = SpotifyPKCE(
                client_id=self._client_id,
                redirect_uri=self.REDIRECT_URI,
                scope=self.SCOPE,
                cache_path=str(cache_path),
                open_browser=True,
            )
            self._sp = spotipy.Spotify(auth_manager=auth_manager)
            # Validate token
            self._sp.current_user()
            logger.info("Spotify Web API authenticated")
            return True
        except Exception as exc:
            logger.warning("Spotify Web API auth failed: %s", exc)
            return False

    def get_current_track(self) -> dict | None:
        """Fetch currently playing track. Returns dict or None."""
        if self._sp is None:
            return None
        try:
            result = self._sp.current_playback()
            if result is None or result.get("item") is None:
                return None
            item = result["item"]
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            album = item.get("album", {})
            images = album.get("images", [])
            artwork_url = images[0]["url"] if images else ""
            return {
                "title": item.get("name", ""),
                "artist": artists,
                "album": album.get("name", ""),
                "artwork_url": artwork_url,
                "duration_ms": item.get("duration_ms", 0),
                "progress_ms": result.get("progress_ms", 0),
                "is_playing": result.get("is_playing", False),
                "shuffle": result.get("shuffle_state", False),
                "repeat": result.get("repeat_state", "off"),
                "volume_percent": result.get("device", {}).get("volume_percent", 100),
            }
        except Exception as exc:
            logger.debug("Spotify API get_current_track error: %s", exc)
            return None

    def send_command(self, cmd: str, **kwargs) -> bool:
        """Send a transport control command. Returns True on success."""
        if self._sp is None:
            return False
        try:
            if cmd == "play":
                self._sp.start_playback()
            elif cmd == "pause":
                self._sp.pause_playback()
            elif cmd == "next":
                self._sp.next_track()
            elif cmd == "previous":
                self._sp.previous_track()
            elif cmd == "shuffle":
                # Toggle shuffle
                current = self.get_current_track()
                new_state = not (current.get("shuffle", False) if current else False)
                self._sp.shuffle(new_state)
            elif cmd == "repeat":
                # Cycle: off → all → track → off
                current = self.get_current_track()
                cur_repeat = current.get("repeat", "off") if current else "off"
                cycle = {"off": "context", "context": "track", "track": "off"}
                self._sp.repeat(cycle.get(cur_repeat, "off"))
            elif cmd == "volume":
                vol = kwargs.get("volume_percent", 50)
                self._sp.volume(int(vol))
            else:
                logger.debug("Spotify: unknown command '%s'", cmd)
                return False
            return True
        except Exception as exc:
            logger.debug("Spotify API command '%s' error: %s", cmd, exc)
            return False

    def get_artwork_bytes(self, url: str) -> bytes:
        """Download artwork image from URL. Returns bytes or empty."""
        if not url:
            return b""
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "AlfiePRIME-Musiciser/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read()
        except Exception as exc:
            logger.debug("Spotify artwork download error: %s", exc)
            return b""



# ---------------------------------------------------------------------------
# Spotify supported commands
# ---------------------------------------------------------------------------

# Full commands when Web API is available
_SPOTIFY_API_COMMANDS = [
    "play", "pause", "next", "previous",
    "shuffle", "repeat", "volume",
]

# Basic commands without Web API — play state comes from librespot events,
# volume is local only (no Spotify device sync)
_SPOTIFY_BASIC_COMMANDS = [
    "play", "pause", "volume",
]


# ---------------------------------------------------------------------------
# Main receiver class
# ---------------------------------------------------------------------------


class SpotifyConnectReceiver:
    """Spotify Connect receiver using librespot + Spotify Web API.

    Public API mirrors AirPlayReceiver for consistency.
    """

    def __init__(
        self,
        tui: BoomBoxTUI | None,
        visualizer: AudioVisualizer,
        device_name: str = "",
        config: Config | None = None,
    ):
        self._tui = tui
        self._visualizer = visualizer
        self._state: PlayerState = tui.state if tui is not None else self._make_standalone_state()
        self._device_name = device_name or "Musiciser"
        self._config = config
        self._process = None
        self._pcm_reader: _PCMReader | None = None
        self._api: _SpotifyAPI | None = None
        self._running = False
        self._restart_count = 0
        self._last_track_id = ""
        self._last_artwork_url = ""
        # Metadata polling interval (seconds) — used as fallback
        self._metadata_poll_interval = 3.0
        self._metadata_thread: threading.Thread | None = None

        # DJ mixer integration
        self.__dj_mixer = None
        self._dj_feed_channel = "a"

    @staticmethod
    def _make_standalone_state():
        from alfieprime_musiciser.state import PlayerState
        return PlayerState()

    # ── DJ mixer property (mirrors AirPlay pattern) ──

    @property
    def _dj_mixer(self):
        return self.__dj_mixer

    @_dj_mixer.setter
    def _dj_mixer(self, mixer):
        global _dj_mixer_active
        self.__dj_mixer = mixer
        _dj_mixer_active = mixer is not None
        if self._pcm_reader is not None:
            self._pcm_reader.dj_mixer = mixer
            self._pcm_reader.dj_feed_channel = self._dj_feed_channel
            logger.info("Spotify: DJ mixer propagated to PCM reader (ch=%s, mixer=%s)",
                        self._dj_feed_channel, "ON" if mixer else "OFF")

    # ── Subprocess lifecycle ──

    def _build_librespot_cmd(self) -> list[str]:
        """Build the librespot command line."""
        cfg = self._config
        bitrate = str(cfg.spotify_bitrate) if cfg and hasattr(cfg, "spotify_bitrate") else "320"

        cmd = [
            "librespot",
            "--backend", "pipe",
            "--format", "S16",
            "--bitrate", bitrate,
            "--name", self._device_name,
            "--initial-volume", "100",
            "--verbose",
            "--disable-audio-cache",
        ]

        # Username for zeroconf-less auth (optional)
        username = cfg.spotify_username if cfg and hasattr(cfg, "spotify_username") else ""
        if username:
            cmd += ["--username", username]

        # Cache directory for librespot
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_dir = str(_LIBRESPOT_CACHE)
        cmd += ["--cache", cache_dir]

        return cmd

    def _spawn_librespot(self) -> None:
        """Spawn the librespot subprocess."""
        import subprocess

        cmd = self._build_librespot_cmd()

        logger.info("Spawning librespot: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        # Start PCM reader on stdout
        self._pcm_reader = _PCMReader(self._visualizer, self._state)
        if self.__dj_mixer is not None:
            self._pcm_reader.dj_mixer = self.__dj_mixer
            self._pcm_reader.dj_feed_channel = self._dj_feed_channel
        self._pcm_reader.start(self._process.stdout)

        # Start stderr monitor
        threading.Thread(target=self._monitor_stderr, daemon=True).start()

        logger.info("librespot started (PID %d)", self._process.pid)

    def _monitor_stderr(self) -> None:
        """Monitor librespot stderr for all events.

        librespot 0.8.0 verbose log format:
          [timestamp LEVEL module] message
        We parse these directly instead of using --onevent (which spawns a
        subprocess per event and can stall librespot's audio pipeline).

        IMPORTANT: with bufsize=0 the stderr pipe is raw FileIO, so naive
        line iteration reads one byte at a time — far too slow to keep up
        with librespot's verbose output.  We wrap it in a BufferedReader
        so readline() uses an efficient internal buffer.  If stderr isn't
        drained fast enough librespot blocks on log writes, stalling the
        audio pipeline and causing Spotify to skip tracks.
        """
        import io as _io

        proc = self._process
        if proc is None or proc.stderr is None:
            return
        # Wrap raw FileIO in BufferedReader for efficient line iteration.
        stderr = _io.BufferedReader(proc.stderr, buffer_size=65536)
        # Track whether we've already fired connected for this session
        connected_fired = False
        for line_bytes in stderr:
            try:
                line = line_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not line:
                continue
            logger.debug("librespot: %s", line)
            ll = line.lower()

            # ── Connection / disconnection ──
            # e.g. "Authenticated as ..." or "Country: ..."
            if not connected_fired and ("authenticated as" in ll or "country:" in ll):
                connected_fired = True
                self._on_librespot_connected()
            elif "disconnected" in ll or "connection closed" in ll:
                connected_fired = False
                self._on_librespot_disconnected()

            # ── Track loading ──
            # librespot 0.8: "Loading <Track Name> with Spotify URI <spotify:track:ID>"
            elif "loading <" in ll:
                m = re.search(r'Loading <(.+?)> with Spotify URI <(.+?)>', line)
                if m:
                    track_name = m.group(1).strip()
                    track_uri = m.group(2).strip()
                    if track_uri != self._last_track_id:
                        self._last_track_id = track_uri
                        # Reset PCM-based progress for new track
                        if self._pcm_reader:
                            self._pcm_reader.reset_progress(0)
                        self._update_title_from_stderr(track_name)
                        if self._api:
                            self._fetch_and_update_metadata()

            # ── Track loaded with duration ──
            # librespot 0.8: "<Track Name> (288385 ms) loaded"
            elif "ms) loaded" in ll:
                m = re.search(r'<(.+?)>\s+\((\d+)\s+ms\)\s+loaded', line)
                if m:
                    track_name = m.group(1).strip()
                    dur_ms = int(m.group(2))
                    self._update_title_from_stderr(track_name)
                    if dur_ms > 0:
                        if self._is_active():
                            self._state.duration_ms = dur_ms
                        else:
                            self._state.write_to_snapshot("spotify", duration_ms=dur_ms)

            # ── Playback state ──
            # DEBUG: "command=Load { ..., play: true, ... }"
            elif "command=load" in ll:
                if "play: true" in ll or "play:true" in ll:
                    self._set_playing(True)
                elif "play: false" in ll or "play:false" in ll:
                    self._set_playing(False)
            # TRACE (if RUST_LOG=trace): "==> Playing" / "==> Paused"
            elif "==> playing" in ll:
                self._set_playing(True)
            elif "==> paused" in ll or "==> stopped" in ll:
                self._set_playing(False)
            # Also catch "device became inactive"
            elif "device became inactive" in ll:
                self._set_playing(False)

            # ── Volume ──
            # DEBUG: "SpircTask::set_volume(32768)" (0-65535)
            # INFO:  "delayed volume update for all devices: volume is now 32768"
            elif "set_volume(" in ll or "volume is now" in ll:
                m = re.search(r'set_volume\((\d+)\)', line) or \
                    re.search(r'volume is now\s+(\d+)', line)
                if m:
                    try:
                        vol_raw = int(m.group(1))
                        vol_pct = int(vol_raw / 65535 * 100)
                        self._state.set_source_volume("spotify", min(100, vol_pct), muted=False)
                    except (ValueError, TypeError):
                        pass

    def _update_title_from_stderr(self, title: str) -> None:
        """Update track title from librespot's stderr (no API needed)."""
        if self._is_active():
            self._state.title = title
            self._state.connected = True
        else:
            self._state.write_to_snapshot("spotify", title=title)

    def _on_librespot_connected(self) -> None:
        """Called when a Spotify client connects to librespot."""
        state = self._state
        state.spotify_connected = True
        state.connected = True
        state.spotify_server_name = self._device_name

        # Auto-switch to Spotify if no source is active, or save current
        # source snapshot and switch (same pattern as AirPlay do_RECORD)
        if not state.active_source or state.active_source == "":
            state.active_source = "spotify"
        elif state.active_source != "spotify":
            # Another source is active — save Spotify state to snapshot
            pass

        # Set supported commands based on whether Web API is available
        cmds = list(_SPOTIFY_API_COMMANDS if self._api else _SPOTIFY_BASIC_COMMANDS)
        if self._is_active():
            state.supported_commands = cmds
            state.codec = "vorbis"
            state.sample_rate = 44100
            state.bit_depth = 16
        else:
            state.write_to_snapshot("spotify",
                supported_commands=cmds,
                codec="vorbis",
                sample_rate=44100,
                bit_depth=16,
            )

        state.show_toast("Spotify Connected", self._device_name)
        logger.info("Spotify client connected (Web API: %s)", "yes" if self._api else "no")

        # Fetch rich metadata if API is available
        if self._api:
            self._fetch_and_update_metadata()

    def _on_librespot_disconnected(self) -> None:
        """Called when a Spotify client disconnects."""
        state = self._state
        was_active = state.active_source == "spotify"

        state.spotify_connected = False
        state.connected = state.sendspin_connected or state.airplay_connected
        state.show_toast("Spotify Disconnected")
        logger.info("Spotify client disconnected")

        if was_active:
            state.save_snapshot("spotify")
            # Fall back to next connected source
            if state.sendspin_connected:
                state.active_source = "sendspin"
                state.restore_snapshot("sendspin")
            elif state.airplay_connected:
                state.active_source = "airplay"
                state.restore_snapshot("airplay")
            else:
                state.active_source = ""
                state.is_playing = False
                state.supported_commands = []

    def _is_active(self) -> bool:
        return self._state.active_source in ("spotify", "")

    def _set_playing(self, playing: bool) -> None:
        """Set is_playing, gated by active source."""
        if self._is_active():
            if not playing and self._state.is_playing:
                self._state.progress_ms = self._state.get_interpolated_progress()
                self._state.playback_speed = 0.0
                self._state.progress_update_time = time.monotonic()
            self._state.is_playing = playing
            if self._visualizer and not _dj_mixer_active:
                self._visualizer.set_paused(not playing)
        else:
            self._state.write_to_snapshot("spotify", is_playing=playing)

    # ── Metadata fetching ──

    def _fetch_and_update_metadata(self) -> None:
        """Fetch current track from Spotify API and update state."""
        threading.Thread(target=self._do_fetch_metadata, daemon=True).start()

    def _do_fetch_metadata(self) -> None:
        """Background metadata fetch."""
        if self._api is None:
            return
        track = self._api.get_current_track()
        if track is None:
            return

        fields = {
            "title": track["title"],
            "artist": track["artist"],
            "album": track["album"],
            "duration_ms": track["duration_ms"],
            "progress_ms": track["progress_ms"],
            "progress_update_time": time.monotonic(),
            "playback_speed": 1.0 if track["is_playing"] else 0.0,
            "shuffle": track.get("shuffle", False),
        }

        # Map repeat state
        repeat_raw = track.get("repeat", "off")
        repeat_map = {"off": "off", "context": "all", "track": "one"}
        fields["repeat_mode"] = repeat_map.get(repeat_raw, "off")
        fields["supported_commands"] = list(_SPOTIFY_API_COMMANDS)

        if self._is_active():
            for k, v in fields.items():
                setattr(self._state, k, v)
            self._state.is_playing = track["is_playing"]
            self._state.connected = True
            logger.info("Spotify now playing: %s - %s [%s]",
                        track["artist"], track["title"], track["album"])
        else:
            self._state.write_to_snapshot("spotify", **fields,
                                          is_playing=track["is_playing"])

        # Fetch artwork if URL changed
        artwork_url = track.get("artwork_url", "")
        if artwork_url and artwork_url != self._last_artwork_url:
            self._last_artwork_url = artwork_url
            self._fetch_and_apply_artwork(artwork_url)

    def _fetch_and_apply_artwork(self, url: str) -> None:
        """Download artwork and apply theme extraction."""
        def _worker():
            if self._api is None:
                return
            data = self._api.get_artwork_bytes(url)
            if not data:
                return
            logger.info("Spotify artwork downloaded (%d bytes)", len(data))
            from alfieprime_musiciser.colors import _extract_theme_from_image, ColorTheme
            theme = _extract_theme_from_image(data) or ColorTheme()
            try:
                from alfieprime_musiciser.mpris import write_art_cache
                write_art_cache(data)
            except Exception:
                pass

            if self._is_active():
                self._state.artwork_data = data
                self._state.theme = theme
            else:
                self._state.write_to_snapshot("spotify",
                    artwork_data=data, theme=theme)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Metadata polling (fallback) ──

    def _start_metadata_poll(self) -> None:
        """Start a background thread that polls metadata periodically."""
        self._metadata_thread = threading.Thread(target=self._poll_metadata, daemon=True)
        self._metadata_thread.start()

    def _poll_metadata(self) -> None:
        """Poll Spotify API for metadata updates as a fallback."""
        while self._running:
            time.sleep(self._metadata_poll_interval)
            if not self._running:
                break
            if self._state.spotify_connected:
                self._do_fetch_metadata()

    # ── Transport controls ──

    def _on_transport_command(self, command: str) -> None:
        """Handle transport commands when Spotify is the active source.

        Volume commands always work (local state).  Transport commands
        (play/pause/next/prev/shuffle/repeat) require the Web API — without
        it, playback is controlled from the Spotify app.
        """
        state = self._state
        api = self._api

        if command == "play_pause":
            if api:
                if state.is_playing:
                    api.send_command("pause")
                else:
                    api.send_command("play")
        elif command == "next":
            if api:
                api.send_command("next")
                threading.Timer(0.5, self._fetch_and_update_metadata).start()
        elif command == "previous":
            if api:
                api.send_command("previous")
                threading.Timer(0.5, self._fetch_and_update_metadata).start()
        elif command == "shuffle":
            if api:
                api.send_command("shuffle")
                threading.Timer(0.3, self._fetch_and_update_metadata).start()
        elif command == "repeat":
            if api:
                api.send_command("repeat")
                threading.Timer(0.3, self._fetch_and_update_metadata).start()
        elif command == "volume_up":
            vol, _ = state.get_source_volume("spotify")
            new_vol = min(100, vol + 5)
            state.set_source_volume("spotify", new_vol)
            if api:
                api.send_command("volume", volume_percent=new_vol)
        elif command == "volume_down":
            vol, _ = state.get_source_volume("spotify")
            new_vol = max(0, vol - 5)
            state.set_source_volume("spotify", new_vol)
            if api:
                api.send_command("volume", volume_percent=new_vol)
        elif command == "mute":
            vol, muted = state.get_source_volume("spotify")
            state.set_source_muted("spotify", not muted)
            if api:
                if not muted:
                    api.send_command("volume", volume_percent=0)
                else:
                    api.send_command("volume", volume_percent=vol)
        elif command in ("dj_pause", "dj_play"):
            if api:
                api.send_command("pause" if command == "dj_pause" else "play")

    def dj_play_pause(self, want_pause: bool) -> None:
        """Pause/play Spotify for DJ mode."""
        if self._api is not None:
            self._api.send_command("pause" if want_pause else "play")

    # ── Sink muting (no-op for pipe backend) ──

    def set_sink_muted(self, muted: bool) -> None:
        """No-op — librespot pipes to us, no native sink to mute."""
        pass

    # ── Lifecycle ──

    async def start(self) -> None:
        """Start the Spotify Connect receiver."""
        self._running = True
        self._state.spotify_ready = True

        # Authenticate with Spotify Web API if client_id configured (optional)
        client_id = ""
        if self._config and hasattr(self._config, "spotify_client_id"):
            client_id = self._config.spotify_client_id
        if client_id:
            self._api = _SpotifyAPI(client_id)
            if not self._api.authenticate():
                logger.warning("Spotify Web API auth failed — metadata/controls unavailable")
                self._api = None
        else:
            logger.info("Spotify: no client_id — running without Web API (audio + basic state only)")

        # Intercept TUI command callback for Spotify transport controls
        if self._tui is not None:
            self._original_command_cb = getattr(self._tui, "_command_callback", None)

            def _command_router(command: str) -> None:
                if self._state.active_source == "spotify":
                    self._on_transport_command(command)
                elif self._original_command_cb:
                    self._original_command_cb(command)

            self._tui._command_callback = _command_router

        # Start metadata polling as fallback when API is available
        if self._api is not None:
            self._start_metadata_poll()

        # Spawn librespot (with auto-restart on crash)
        await self._run_librespot_loop()

    async def _run_librespot_loop(self) -> None:
        """Run librespot with auto-restart on crash."""
        backoff = 1.0
        while self._running:
            self._spawn_librespot()
            # Wait for process to exit
            proc = self._process
            if proc is None:
                break
            loop = asyncio.get_running_loop()
            exit_code = await loop.run_in_executor(None, proc.wait)
            logger.warning("librespot exited with code %d", exit_code)

            if self._pcm_reader:
                self._pcm_reader.stop()

            if not self._running:
                break

            self._on_librespot_disconnected()
            self._restart_count += 1

            # Exponential backoff up to 30s
            backoff = min(30.0, backoff * 1.5)
            logger.info("Restarting librespot in %.1fs (attempt %d)", backoff, self._restart_count)
            await asyncio.sleep(backoff)

    def stop(self) -> None:
        """Shut down librespot and cleanup."""
        self._running = False
        self._state.spotify_ready = False

        if self._pcm_reader:
            self._pcm_reader.stop()

        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

        self._state.spotify_connected = False
        self._state.connected = self._state.sendspin_connected or self._state.airplay_connected
        logger.info("Spotify receiver stopped")
