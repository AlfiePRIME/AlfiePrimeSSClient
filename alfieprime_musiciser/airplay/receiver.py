"""AirPlay 2 receiver bridge.

Embeds the vendored ap2-receiver as a background server thread and routes
decoded audio + metadata into the AlfiePRIME pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import socket
import struct
import sys
import threading
import time
import uuid
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from alfieprime_musiciser.state import PlayerState
    from alfieprime_musiciser.visualizer import AudioVisualizer
    from alfieprime_musiciser.tui import BoomBoxTUI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patch the vendored ap2 package so imports resolve correctly.
# The vendored code does ``from ap2.xxx import …`` – we redirect that to
# ``alfieprime_musiciser.airplay.vendor.ap2.xxx``.
# ---------------------------------------------------------------------------

_VENDOR_ROOT = os.path.join(os.path.dirname(__file__), "vendor")


def _patch_vendor_imports() -> None:
    """Add the vendor directory to sys.path and alias 'ap2' → vendored copy."""
    if _VENDOR_ROOT not in sys.path:
        sys.path.insert(0, _VENDOR_ROOT)


# ---------------------------------------------------------------------------
# Hooks – thin wrappers injected into the vendored AP2 audio pipeline
# ---------------------------------------------------------------------------


class _AudioHook:
    """Replaces ``pyaudio`` sink writes to capture decoded PCM."""

    def __init__(
        self,
        visualizer: AudioVisualizer,
        sample_rate: int = 44100,
        sample_size: int = 16,
        channels: int = 2,
        on_audio: Callable[[bytes], None] | None = None,
    ):
        self.visualizer = visualizer
        self.sample_rate = sample_rate
        self.sample_size = sample_size
        self.channels = channels
        self._on_audio = on_audio
        self._pa: object | None = None
        self._real_sink: object | None = None

    # Called by Audio.init_audio_sink – we still open a real pyaudio stream
    # for playback, but *also* feed the visualizer.
    def wrap_sink(self, audio_obj: object) -> None:
        """Monkey-patch *audio_obj*.sink.write to also feed our visualizer."""
        sink = getattr(audio_obj, "sink", None)
        if sink is None:
            return

        original_write = sink.write

        def _hooked_write(data: bytes) -> int:
            # Feed visualizer
            self.visualizer.set_format(self.sample_rate, self.sample_size, self.channels)
            self.visualizer.feed_audio(data)
            if self._on_audio:
                self._on_audio(data)
            # Still play through speakers
            return original_write(data)

        sink.write = _hooked_write
        logger.info(
            "AirPlay audio hook installed (%d Hz, %d-bit, %d ch)",
            self.sample_rate, self.sample_size, self.channels,
        )


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


# ---------------------------------------------------------------------------
# Patched AP2Handler that calls our hooks
# ---------------------------------------------------------------------------


def _create_patched_handler(audio_hook: _AudioHook, meta_hook: _MetadataHook):
    """Import and return a subclass of AP2Handler with our hooks injected."""
    _patch_vendor_imports()

    # Now we can import the vendored modules
    from ap2_receiver import AP2Handler  # type: ignore[import-untyped]
    from ap2.dxxp import parse_dxxp  # type: ignore[import-untyped]

    class PatchedAP2Handler(AP2Handler):
        """AP2Handler with audio/metadata hooks for AlfiePRIME integration."""

        _audio_hook = audio_hook
        _meta_hook = meta_hook

        def do_SET_PARAMETER(self):
            """Override to intercept metadata, artwork, and volume."""
            content_type = self.headers.get("Content-Type", "")
            content_len = int(self.headers.get("Content-Length", 0))

            if content_type == "text/parameters" and content_len > 0:
                body = self.rfile.read(content_len)
                for line in body.split(b"\r\n"):
                    if not line:
                        continue
                    parts = line.split(b":", 1)
                    if len(parts) == 2:
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
                            except ValueError:
                                pass

                self.send_response(200)
                self.send_header("Server", self.version_string())
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            elif content_type.startswith("image/") and content_len > 0:
                data = self.rfile.read(content_len)
                self._meta_hook.on_artwork(data)
                self.send_response(200)
                self.send_header("Server", self.version_string())
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            elif content_type == "application/x-dmap-tagged" and content_len > 0:
                data = self.rfile.read(content_len)
                try:
                    info = parse_dxxp(data)
                    title = info.get("itemname", "") or info.get("minm", "")
                    artist = info.get("songartist", "") or info.get("asar", "")
                    album = info.get("songalbum", "") or info.get("asal", "")
                    self._meta_hook.on_metadata(title, artist, album)
                except Exception:
                    logger.debug("Failed to parse DMAP metadata", exc_info=True)
                self.send_response(200)
                self.send_header("Server", self.version_string())
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            # Fall back to parent for anything else
            super().do_SET_PARAMETER()

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
    ):
        self._tui = tui
        self._visualizer = visualizer
        self._device_name = device_name
        self._port = port
        self._running = False
        self._server: object | None = None
        self._server_thread: threading.Thread | None = None
        self._zeroconf: object | None = None

    @property
    def _state(self) -> PlayerState:
        if self._tui is not None:
            return self._tui.state
        # Fallback for daemon mode
        from alfieprime_musiciser.state import PlayerState
        if not hasattr(self, "_daemon_state"):
            self._daemon_state = PlayerState()
        return self._daemon_state

    async def start(self) -> None:
        """Start the AirPlay receiver in a background thread."""
        self._running = True
        loop = asyncio.get_running_loop()

        self._state.server_name = f"AirPlay on :{self._port}"
        self._state.connected = False
        self._state.codec = "airplay"

        logger.info("Starting AirPlay 2 receiver on port %d as '%s'", self._port, self._device_name)

        # Start the server in a thread (it's blocking)
        await loop.run_in_executor(None, self._run_server)

    def _run_server(self) -> None:
        """Blocking server start – runs in a thread."""
        _patch_vendor_imports()

        try:
            import netifaces as ni
            from ap2_receiver import AP2Server, setup_global_structs, register_mdns  # type: ignore[import-untyped]
            import ap2_receiver as ap2mod  # type: ignore[import-untyped]
        except ImportError as exc:
            logger.error("AirPlay dependencies not available: %s", exc)
            return

        # Find a network interface
        iface = None
        for name in ni.interfaces():
            addrs = ni.ifaddresses(name)
            if ni.AF_INET in addrs:
                for addr in addrs[ni.AF_INET]:
                    ip = addr.get("addr", "")
                    if ip and not ip.startswith("127."):
                        iface = name
                        break
            if iface:
                break

        if not iface:
            logger.error("No suitable network interface found for AirPlay")
            return

        # Configure the global state that ap2-receiver expects
        try:
            setup_global_structs(iface, self._device_name)
        except Exception:
            logger.exception("Failed to setup AirPlay global structs")
            return

        # Create hooks
        audio_hook = _AudioHook(self._visualizer)
        meta_hook = _MetadataHook(self._state)

        # Create patched handler
        HandlerClass = _create_patched_handler(audio_hook, meta_hook)

        # Monkey-patch Audio.init_audio_sink to install our hook after real init
        from ap2.connections.audio import Audio  # type: ignore[import-untyped]
        original_init_sink = Audio.init_audio_sink

        def _patched_init_sink(self_audio):
            original_init_sink(self_audio)
            audio_hook.sample_rate = self_audio.sample_rate
            audio_hook.sample_size = self_audio.sample_size
            audio_hook.channels = self_audio.channel_count
            audio_hook.wrap_sink(self_audio)

        Audio.init_audio_sink = _patched_init_sink

        # Register mDNS
        try:
            register_mdns(ap2mod.MDNS_OBJ)
            logger.info("AirPlay mDNS registered as '%s'", self._device_name)
        except Exception:
            logger.warning("mDNS registration failed, AirPlay discovery may not work", exc_info=True)

        # Update state
        self._state.connected = True
        self._state.server_name = f"AirPlay: {self._device_name}"

        # Start server
        try:
            ip = ap2mod.IPV4 or "0.0.0.0"
            self._server = AP2Server((ip, self._port), HandlerClass)
            logger.info("AirPlay server listening on %s:%d", ip, self._port)
            self._server.serve_forever()
        except Exception:
            logger.exception("AirPlay server error")
        finally:
            self._running = False
            self._state.connected = False

    def stop(self) -> None:
        """Shut down the AirPlay server."""
        self._running = False
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
        logger.info("AirPlay receiver stopped")
