from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import platform
import random
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment,misc]

from alfieprime_musiciser.colors import ColorTheme, _default_theme, _extract_theme_from_image
from alfieprime_musiciser.config import Config
from alfieprime_musiciser.renderer import reset_vu_peaks
from alfieprime_musiciser.state import PlayerState
from alfieprime_musiciser.mpris import MPRIS2Server, clear_art_cache, write_art_cache
from alfieprime_musiciser.smtc import SMTCServer
from alfieprime_musiciser.stats import ListeningStats
from alfieprime_musiciser.visualizer import AudioVisualizer

if TYPE_CHECKING:
    from aiosendspin.client import AudioFormat
    from alfieprime_musiciser.tui import BoomBoxTUI

logger = logging.getLogger(__name__)


# ─── SendSpin Receiver ───────────────────────────────────────────────────────


def _get_device_info():
    """Build DeviceInfo for the client hello."""
    from aiosendspin.models.core import DeviceInfo
    from importlib.metadata import version

    system = platform.system()
    product_name = system
    if system == "Linux":
        try:
            os_release = Path("/etc/os-release")
            if os_release.exists():
                for raw_line in os_release.read_text().splitlines():
                    if raw_line.startswith("PRETTY_NAME="):
                        product_name = raw_line.split("=", 1)[1].strip().strip('"')
                        break
        except OSError:
            pass

    try:
        sw_version = f"alfieprime-musiciser (aiosendspin {version('aiosendspin')})"
    except Exception:
        sw_version = "alfieprime-musiciser"

    return DeviceInfo(product_name=product_name, manufacturer=None, software_version=sw_version)


class SendSpinReceiver:
    """Connects to a Music Assistant / SendSpin server, receives audio + metadata.

    Connection modes:
    - No URL: Listens on port 8928, advertises via mDNS (_sendspin._tcp.local.)
      so Music Assistant / SendSpin servers discover and connect to us automatically.
    - With URL: Client-initiated connection to a specific server with auto-reconnect.
    """

    def __init__(
        self, tui: BoomBoxTUI | None, visualizer: AudioVisualizer,
        server_url: str | None = None, listen_port: int = 8928,
        client_name: str = "MKUltra", config: Config | None = None,
    ) -> None:
        self._tui = tui
        self._visualizer = visualizer
        self._server_url = server_url
        self._listen_port = listen_port
        self._client_name = client_name
        self._client = None
        self._audio_handler = None
        self._listener = None
        self._audio_device = None
        self._supported_formats = None
        self._config = config
        # Use persisted client_id so Music Assistant recognises us across restarts
        if config and config.client_id:
            self._client_id = config.client_id
        else:
            self._client_id = f"alfieprime-musiciser-{uuid.uuid4().hex[:8]}"
            # Persist the newly generated ID for future runs
            if config:
                config.client_id = self._client_id
                config.save()
        self._connection_lock: asyncio.Lock | None = None
        self._flac_decoder = None
        self._flac_fmt = None
        self._dj_mixer = None  # Set by main.py when DJ mode activates
        self._dj_feed_channel = "a"  # Which mixer channel this receiver feeds ("a" or "b")
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Pre-cached themes per artwork channel (channel 0 = current, 1+ = upcoming)
        self._artwork_themes: dict[int, ColorTheme] = {}
        self._stats = ListeningStats()
        self._mpris: MPRIS2Server | None = None

        # In daemon mode there is no TUI — use a standalone state object
        if self._tui is None:
            self._daemon_state = PlayerState()
        else:
            self._daemon_state = None
            # Wire up transport control commands from TUI
            self._tui.set_command_callback(self._on_transport_command)

    @property
    def _state(self) -> PlayerState:
        """Player state — from TUI when available, standalone in daemon mode."""
        if self._daemon_state is not None:
            return self._daemon_state
        return self._tui.state  # type: ignore[union-attr]

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()

        # Register with OS media controls (MPRIS2 on Linux, SMTC on Windows)
        if sys.platform == "linux":
            self._mpris = MPRIS2Server(self._state, self._on_transport_command)
            await self._mpris.start()
        elif sys.platform == "win32":
            self._mpris = SMTCServer(self._state, self._on_transport_command)  # type: ignore[assignment]
            await self._mpris.start()

        from sendspin.audio_devices import detect_supported_audio_formats, query_devices
        from sendspin.audio_connector import AudioStreamHandler

        # Pick the default audio output device
        devices = query_devices()
        self._audio_device = next((d for d in devices if d.is_default), devices[0] if devices else None)
        if self._audio_device is None:
            raise RuntimeError("No audio output devices found")

        logger.info("Using audio device: %s", self._audio_device.name)
        self._supported_formats = detect_supported_audio_formats(self._audio_device)

        # Audio stream handler (manages playback + FLAC decoding)
        self._audio_handler = AudioStreamHandler(
            audio_device=self._audio_device,
            volume=100,
            muted=False,
            on_format_change=self._on_format_change,
            on_event=self._on_stream_event,
        )

        # Connect
        if self._server_url:
            # Client-initiated: we connect to the server
            self._client = self._create_client()
            self._audio_handler.attach_client(self._client)
            await self._connection_loop_url(self._server_url)
        else:
            # Server-initiated: listen + advertise via mDNS, server connects to us
            await self._run_listener()

    def _create_client(self) -> "SendspinClient":
        """Create a new SendspinClient instance."""
        from aiosendspin.client import SendspinClient
        from aiosendspin.models.player import ClientHelloPlayerSupport
        from aiosendspin.models.types import PlayerCommand, Roles

        # Build artwork support if Pillow is available
        artwork_support = None
        artwork_roles: list[Roles] = []
        if Image is not None:
            from aiosendspin.models.artwork import ArtworkChannel, ClientHelloArtworkSupport
            from aiosendspin.models.types import ArtworkSource, PictureFormat
            artwork_support = ClientHelloArtworkSupport(
                channels=[
                    ArtworkChannel(
                        source=ArtworkSource.ALBUM,
                        format=PictureFormat.JPEG,
                        media_width=128,
                        media_height=128,
                    ),
                ],
            )
            artwork_roles = [Roles.ARTWORK]

        client = SendspinClient(
            client_id=self._client_id,
            client_name=self._client_name,
            roles=[Roles.PLAYER, Roles.METADATA, Roles.CONTROLLER, *artwork_roles],
            device_info=_get_device_info(),
            player_support=ClientHelloPlayerSupport(
                supported_formats=self._supported_formats,
                buffer_capacity=32_000_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
            artwork_support=artwork_support,
            initial_volume=100,
            initial_muted=False,
        )

        # Patch binary message handler to also handle artwork channels
        if Image is not None:
            self._patch_artwork_handler(client)

        # Register callbacks
        client.add_audio_chunk_listener(self._on_audio_chunk)
        client.add_metadata_listener(self._on_metadata)
        client.add_group_update_listener(self._on_group_update)
        client.add_controller_state_listener(self._on_controller_state)
        client.add_server_command_listener(self._on_server_command)

        return client

    # ── Server-initiated mode (mDNS listener) ──

    async def _run_listener(self) -> None:
        """Listen for incoming server connections, advertised via mDNS."""
        from aiosendspin.client import ClientListener
        from aiosendspin.models.core import ClientGoodbyeMessage, ClientGoodbyePayload
        from aiosendspin.models.types import GoodbyeReason

        self._connection_lock = asyncio.Lock()

        self._listener = ClientListener(
            client_id=self._client_id,
            on_connection=self._handle_server_connection,
            port=self._listen_port,
            client_name=self._client_name,
        )
        await self._listener.start()

        self._state.sendspin_server_name = f"Listening on :{self._listen_port}"
        self._state.server_name = self._state.sendspin_server_name
        self._state.connected = False
        self._state.sendspin_ready = True
        logger.info(
            "Listening on port %d, advertising via mDNS (_sendspin._tcp.local.)",
            self._listen_port,
        )

        try:
            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)
        finally:
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            if self._audio_handler is not None:
                await self._audio_handler.shutdown()
            await self._listener.stop()
            self._listener = None

    async def _handle_server_connection(self, ws) -> None:
        """Handle an incoming server WebSocket connection."""
        from aiosendspin.models.core import ClientGoodbyeMessage, ClientGoodbyePayload
        from aiosendspin.models.types import GoodbyeReason

        assert self._connection_lock is not None
        assert self._audio_handler is not None

        # Log the remote IP if available
        remote = "unknown"
        try:
            peer = ws._reader._transport.get_extra_info("peername")  # noqa: SLF001
            if peer:
                remote = f"{peer[0]}:{peer[1]}"
        except Exception:
            pass
        logger.info("Server connected from %s", remote)

        # ── Swap gating: if another source is active, ask user ──
        if self._state.active_source and self._state.active_source != "sendspin":
            cfg = self._config
            # Auto-accept previously accepted devices
            device_key = f"sendspin:{remote}"
            if cfg and device_key in cfg.accepted_devices:
                logger.info("Auto-accepting previously approved SendSpin device %s", remote)
            elif cfg and not cfg.swap_prompt:
                # Auto-action without prompting
                if cfg.swap_auto_action == "deny":
                    logger.info("Auto-denying SendSpin connection (active_source=%s)", self._state.active_source)
                    return
                # "accept" falls through
            elif cfg and cfg.swap_prompt:
                self._state.swap_pending = True
                self._state.swap_pending_source = "sendspin"
                self._state.swap_pending_name = remote
                self._state.swap_response = ""
                # Wait for user response (up to 30s)
                for _ in range(300):
                    if self._state.swap_response:
                        break
                    await asyncio.sleep(0.1)
                response = self._state.swap_response
                self._state.swap_pending = False
                self._state.swap_response = ""
                if response != "accept":
                    logger.info("User denied SendSpin connection swap")
                    return
                # Remember this device for future connections
                if device_key not in cfg.accepted_devices:
                    cfg.accepted_devices.append(device_key)
                    cfg.save()

        async with self._connection_lock:
            # Clean up previous client if any
            if self._client is not None:
                logger.info("Disconnecting from previous server")
                self._state.sendspin_connected = False
                self._state.connected = self._state.airplay_connected
                self._state.is_playing = False
                await self._audio_handler.handle_disconnect()
                if self._client.connected:
                    with contextlib.suppress(Exception):
                        await self._client._send_message(  # noqa: SLF001
                            ClientGoodbyeMessage(
                                payload=ClientGoodbyePayload(reason=GoodbyeReason.ANOTHER_SERVER)
                            ).to_json()
                        )
                await self._client.disconnect()

            # Create fresh client for this connection
            client = self._create_client()
            self._client = client
            self._audio_handler.attach_client(client)

            try:
                await client.attach_websocket(ws)
            except TimeoutError:
                logger.warning("Handshake with server timed out")
                await self._audio_handler.handle_disconnect()
                if self._client is client:
                    self._client = None
                return
            except Exception:
                logger.exception("Error during server handshake")
                await self._audio_handler.handle_disconnect()
                if self._client is client:
                    self._client = None
                return

        # Handshake complete - update TUI
        server_info = client.server_info
        server_name = server_info.name if server_info else "Server"
        self._state.sendspin_connected = True
        self._state.connected = True
        if not self._state.active_source:
            self._state.active_source = "sendspin"
        self._state.sendspin_server_name = server_name
        # Only update the displayed server name if SendSpin is the active source
        if self._state.active_source == "sendspin":
            self._state.server_name = server_name
        logger.info("Connected to server: %s (%s)", server_name, remote)
        src_label = "Source 1" if self._dj_feed_channel == "a" else "Source 2"
        self._state.show_toast(
            f"SendSpin connected",
            f"{server_name} → {src_label}",
        )

        # If another source is already active, mute SendSpin audio and pause
        # playback on the MA server so it doesn't stream in the background.
        if self._state.active_source != "sendspin":
            if self._audio_handler is not None:
                self._audio_handler.set_volume(0, muted=True)
            # Ask MA to pause so it doesn't keep streaming while AirPlay is active
            if client.connected:
                from aiosendspin.models.types import MediaCommand
                try:
                    await client.send_group_command(MediaCommand.PAUSE)
                    logger.info("Paused SendSpin playback (AirPlay is active)")
                except Exception:
                    logger.debug("Failed to pause SendSpin on connect", exc_info=True)
        else:
            await self._apply_auto_settings()

        # Wait for disconnect
        try:
            disconnect_event = asyncio.Event()
            unsub = client.add_disconnect_listener(disconnect_event.set)
            await disconnect_event.wait()
            unsub()
            logger.info("Server disconnected")
        except Exception:
            logger.exception("Error waiting for server disconnect")
        finally:
            if self._client is client:
                self._state.sendspin_connected = False
                self._state.connected = self._state.airplay_connected
                self._state.sendspin_server_name = f"Listening on :{self._listen_port}"
                src_label = "Source 1" if self._dj_feed_channel == "a" else "Source 2"
                self._state.show_toast("SendSpin disconnected", src_label)
                if self._state.active_source == "sendspin":
                    self._state.save_snapshot("sendspin")
                    new_src = "airplay" if self._state.airplay_connected else ""
                    self._state.active_source = new_src
                    if new_src:
                        self._state.restore_snapshot(new_src)
                    else:
                        self._state.is_playing = False
                self._state.server_name = self._state.sendspin_server_name
                await self._audio_handler.handle_disconnect()
                if self._dj_mixer is None:
                    self._visualizer.reset()
                reset_vu_peaks()

    # ── Client-initiated mode (explicit URL) ──

    async def _connection_loop_url(self, url: str) -> None:
        """Connect to a specific URL with reconnection."""
        from aiohttp import ClientError

        self._state.sendspin_server_name = url
        self._state.server_name = url
        self._state.connected = False
        self._state.sendspin_ready = True
        backoff = 1.0

        while self._running:
            try:
                logger.info("Connecting to %s", url)
                assert self._client is not None
                await self._client.connect(url)
                self._state.sendspin_connected = True
                self._state.connected = True
                if not self._state.active_source:
                    self._state.active_source = "sendspin"
                self._state.sendspin_server_name = url
                if self._state.active_source == "sendspin":
                    self._state.server_name = url
                src_label = "Source 1" if self._dj_feed_channel == "a" else "Source 2"
                self._state.show_toast(
                    f"SendSpin connected",
                    f"{url} → {src_label}",
                )
                backoff = 1.0

                if self._state.active_source != "sendspin":
                    if self._audio_handler is not None:
                        self._audio_handler.set_volume(0, muted=True)
                    if self._client.connected:
                        from aiosendspin.models.types import MediaCommand
                        try:
                            await self._client.send_group_command(MediaCommand.PAUSE)
                            logger.info("Paused SendSpin playback (AirPlay is active)")
                        except Exception:
                            logger.debug("Failed to pause SendSpin on connect", exc_info=True)
                else:
                    await self._apply_auto_settings()

                # Wait for disconnect
                disconnect_event = asyncio.Event()
                unsub = self._client.add_disconnect_listener(disconnect_event.set)
                await disconnect_event.wait()
                unsub()

                self._state.sendspin_connected = False
                self._state.connected = self._state.airplay_connected
                src_label = "Source 1" if self._dj_feed_channel == "a" else "Source 2"
                self._state.show_toast("SendSpin disconnected", src_label)
                if self._state.active_source == "sendspin":
                    self._state.is_playing = False
                    self._state.active_source = "airplay" if self._state.airplay_connected else ""
                if self._audio_handler:
                    await self._audio_handler.handle_disconnect()
                logger.info("Disconnected, reconnecting...")

            except (TimeoutError, OSError, ClientError) as e:
                logger.warning("Connection error (%s), retrying in %.0fs", type(e).__name__, backoff)
                self._state.sendspin_connected = False
                self._state.connected = self._state.airplay_connected
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
            except Exception:
                logger.exception("Unexpected connection error")
                break

    def _on_audio_chunk(
        self, server_timestamp_us: int, audio_data: bytes | bytearray, fmt: AudioFormat,
    ) -> None:
        """Feed audio to visualizer and DJ mixer (playback is handled by AudioStreamHandler)."""
        from aiosendspin.models.types import AudioCodec

        pcm = fmt.pcm_format

        # Always decode — needed for both visualizer and DJ mixer
        raw_pcm: bytes | bytearray | None = None
        if fmt.codec == AudioCodec.PCM:
            raw_pcm = audio_data
        elif fmt.codec == AudioCodec.FLAC:
            if self._flac_decoder is None or self._flac_fmt != fmt:
                from sendspin.decoder import FlacDecoder
                self._flac_decoder = FlacDecoder(fmt)
                self._flac_fmt = fmt
            decoded = self._flac_decoder.decode(audio_data)
            if decoded:
                raw_pcm = decoded

        if raw_pcm is None:
            return

        # Feed DJ mixer when active — channel determined by _dj_feed_channel
        mixer = self._dj_mixer
        if mixer is not None:
            if self._dj_feed_channel == "b":
                mixer.set_format_b(pcm.sample_rate, pcm.bit_depth, pcm.channels)
                mixer.feed_b(raw_pcm)
            else:
                mixer.set_format_a(pcm.sample_rate, pcm.bit_depth, pcm.channels)
                mixer.feed_a(raw_pcm)

        # Feed visualizer only when SendSpin is the active source
        # and DJ mixer is NOT running (mixer owns the master viz in DJ mode)
        if mixer is None and (not self._state.active_source or self._state.active_source == "sendspin"):
            self._visualizer.set_format(pcm.sample_rate, pcm.bit_depth, pcm.channels)
            self._visualizer.feed_audio(raw_pcm)

    def _sendspin_is_active(self) -> bool:
        return self._state.active_source in ("sendspin", "")

    def _on_metadata(self, payload) -> None:
        """Handle metadata updates from server."""
        from aiosendspin.models.types import RepeatMode, UndefinedField

        state = self._state
        meta = payload.metadata
        if meta is None:
            return

        # Extract raw values from the payload
        fields: dict = {}
        if not isinstance(getattr(meta, "title", UndefinedField()), UndefinedField):
            fields["title"] = meta.title or ""
        if not isinstance(getattr(meta, "artist", UndefinedField()), UndefinedField):
            fields["artist"] = meta.artist or ""
        if not isinstance(getattr(meta, "album", UndefinedField()), UndefinedField):
            fields["album"] = meta.album or ""
        if not isinstance(getattr(meta, "album_artist", UndefinedField()), UndefinedField):
            fields["album_artist"] = meta.album_artist or ""
        if not isinstance(getattr(meta, "year", UndefinedField()), UndefinedField):
            fields["year"] = meta.year or 0
        if not isinstance(getattr(meta, "track", UndefinedField()), UndefinedField):
            fields["track_number"] = meta.track or 0

        repeat = getattr(meta, "repeat", UndefinedField())
        if not isinstance(repeat, UndefinedField) and repeat is not None:
            if repeat == RepeatMode.ONE:
                fields["repeat_mode"] = "one"
            elif repeat == RepeatMode.ALL:
                fields["repeat_mode"] = "all"
            else:
                fields["repeat_mode"] = "off"

        shuffle = getattr(meta, "shuffle", UndefinedField())
        if not isinstance(shuffle, UndefinedField) and shuffle is not None:
            fields["shuffle"] = shuffle

        progress = getattr(meta, "progress", UndefinedField())
        if not isinstance(progress, UndefinedField):
            if progress is not None:
                fields["progress_ms"] = progress.track_progress or 0
                fields["duration_ms"] = progress.track_duration or 0
                speed = progress.playback_speed
                fields["playback_speed"] = (speed or 0) / 1000.0
                fields["progress_update_time"] = time.monotonic()
            else:
                fields["progress_ms"] = 0
                fields["duration_ms"] = 0
                fields["playback_speed"] = 0.0
                fields["progress_update_time"] = 0.0

        # Track-change side effects (always run regardless of active source)
        new_title = fields.get("title", state.title)
        old_title = state._source_snapshots.get("sendspin", {}).get("title", state.title) if not self._sendspin_is_active() else state.title
        if new_title != old_title and new_title:
            logger.info("Now playing: %s - %s [%s]",
                        fields.get("artist", state.artist) or "?", new_title,
                        fields.get("album", state.album) or "?")
            self._send_desktop_notification(new_title, fields.get("artist", ""), fields.get("album", ""))
            self._stats.on_track_change(fields.get("artist", ""), new_title)
            self._state.session_stats = self._stats.get_session_summary()
            for ch in (1, 2, 3):
                cached = self._artwork_themes.get(ch)
                if cached is not None and cached.primary != _default_theme.primary:
                    fields["theme"] = cached
                    self._artwork_themes[0] = cached
                    self._artwork_themes.pop(ch, None)
                    break

        # Write to live state or snapshot
        if self._sendspin_is_active():
            for k, v in fields.items():
                setattr(state, k, v)
        else:
            state.write_to_snapshot("sendspin", **fields)

        self._stats.tick()
        self._state.session_stats = self._stats.get_session_summary()

    def _on_group_update(self, payload) -> None:
        """Handle group update messages."""
        from aiosendspin.models.types import PlaybackStateType

        state = self._state
        is_playing = None
        group_name = None
        if payload.group_name:
            group_name = payload.group_name
        if payload.playback_state:
            is_playing = payload.playback_state == PlaybackStateType.PLAYING
            if self._sendspin_is_active() and self._dj_mixer is None:
                self._visualizer.set_paused(not is_playing)
            self._stats.on_playing(is_playing)

        if self._sendspin_is_active():
            if group_name is not None:
                state.group_name = group_name
            if is_playing is not None:
                was_playing = state.is_playing
                state.is_playing = is_playing
                if was_playing and not state.is_playing:
                    state.progress_ms = state.get_interpolated_progress()
                    state.progress_update_time = 0.0
                elif state.is_playing and not was_playing:
                    state.progress_update_time = time.monotonic()
        else:
            snap: dict = {}
            if group_name is not None:
                snap["group_name"] = group_name
            if is_playing is not None:
                snap["is_playing"] = is_playing
            if snap:
                state.write_to_snapshot("sendspin", **snap)

    def _on_format_change(
        self, codec: str | None, sample_rate: int, bit_depth: int, channels: int,
    ) -> None:
        """Handle audio format changes."""
        logger.info("Audio format: %s %dHz %dbit %dch", codec or "PCM", sample_rate, bit_depth, channels)
        if self._dj_mixer is None:
            self._visualizer.set_format(sample_rate, bit_depth, channels)
        if self._sendspin_is_active():
            self._state.codec = codec or "PCM"
            self._state.sample_rate = sample_rate
            self._state.bit_depth = bit_depth
        else:
            self._state.write_to_snapshot("sendspin",
                codec=codec or "PCM", sample_rate=sample_rate, bit_depth=bit_depth,
            )

    def _on_stream_event(self, event: str) -> None:
        """Handle stream start/stop events."""
        playing = event == "start"
        if self._sendspin_is_active() and self._dj_mixer is None:
            self._visualizer.set_paused(not playing)
        if self._sendspin_is_active():
            self._state.is_playing = playing
        else:
            self._state.write_to_snapshot("sendspin", is_playing=playing)
        if event == "stop":
            # Only reset the audio pipeline — keep the visual state (bands,
            # peaks, VU) so the spectrum and meters decay gracefully on pause.
            if self._dj_mixer is None:
                self._visualizer.reset_pipeline()
            self._flac_decoder = None
            self._flac_fmt = None
        elif event == "start":
            # Reset decoder on new stream (format may change)
            self._flac_decoder = None
            self._flac_fmt = None

    def _on_controller_state(self, payload) -> None:
        """Handle controller state updates (supported commands, volume, mute)."""
        ctrl = payload.controller
        if ctrl is None:
            return
        cmds = [cmd.value for cmd in ctrl.supported_commands]
        # Only apply server volume when SendSpin is the active source —
        # otherwise the server's state overwrites locally-saved volume
        # (e.g. muted=True from idle server clobbers the user's setting).
        if self._sendspin_is_active():
            self._state.set_source_volume("sendspin", ctrl.volume, ctrl.muted)
            self._state.supported_commands = cmds
        else:
            self._state.write_to_snapshot("sendspin", supported_commands=cmds)

    def _on_server_command(self, payload) -> None:
        """Handle server commands (volume, mute)."""
        from aiosendspin.models.types import PlayerCommand

        if payload.player is None:
            return
        cmd = payload.player
        if cmd.command == PlayerCommand.VOLUME and cmd.volume is not None:
            self._state.set_source_volume("sendspin", cmd.volume)
            logger.info("Volume set to %d%%", cmd.volume)
            if self._audio_handler is not None:
                _, ss_muted = self._state.get_source_volume("sendspin")
                self._audio_handler.set_volume(cmd.volume, muted=ss_muted)
        elif cmd.command == PlayerCommand.MUTE and cmd.mute is not None:
            self._state.set_source_muted("sendspin", cmd.mute)
            logger.info("Mute %s", "on" if cmd.mute else "off")
            if self._audio_handler is not None:
                ss_vol, _ = self._state.get_source_volume("sendspin")
                self._audio_handler.set_volume(ss_vol, muted=cmd.mute)

    def _patch_artwork_handler(self, client) -> None:
        """Monkey-patch the client's binary message handler to capture artwork."""
        from aiosendspin.models import BINARY_HEADER_SIZE
        from aiosendspin.models.types import BinaryMessageType

        original_handler = client._handle_binary_message  # noqa: SLF001

        artwork_types = {
            BinaryMessageType.ARTWORK_CHANNEL_0.value,
            BinaryMessageType.ARTWORK_CHANNEL_1.value,
            BinaryMessageType.ARTWORK_CHANNEL_2.value,
            BinaryMessageType.ARTWORK_CHANNEL_3.value,
        }

        def patched_handler(payload: bytes) -> None:
            if len(payload) >= BINARY_HEADER_SIZE:
                raw_type = payload[0]
                if raw_type in artwork_types:
                    image_data = payload[BINARY_HEADER_SIZE:]
                    channel = raw_type - BinaryMessageType.ARTWORK_CHANNEL_0.value
                    if image_data:
                        self._on_artwork(channel, image_data)
                    else:
                        self._on_artwork_cleared(channel)
                    return
            original_handler(payload)

        client._handle_binary_message = patched_handler  # noqa: SLF001

    def _on_artwork(self, channel: int, image_data: bytes) -> None:
        """Handle received album artwork - extract colors and cache theme.

        Channel 0 = current track (apply immediately).
        Channels 1+ = upcoming tracks (pre-cache for instant switch).
        """
        logger.debug("Received artwork for channel %d (%d bytes)", channel, len(image_data))

        def _extract_and_apply() -> None:
            theme = _extract_theme_from_image(image_data)
            self._artwork_themes[channel] = theme or ColorTheme()
            if channel == 0:
                write_art_cache(image_data)
                # Cache theme colours for next startup
                if self._config:
                    th = self._artwork_themes[channel]
                    self._config.cached_theme = {
                        "primary": th.primary, "secondary": th.secondary,
                        "accent": th.accent, "warm": th.warm,
                        "highlight": th.highlight, "cool": th.cool,
                        "primary_dim": th.primary_dim, "bg_subtle": th.bg_subtle,
                        "spectrum_colors": th.spectrum_colors,
                        "border_title": th.border_title,
                        "border_now_playing": th.border_now_playing,
                        "border_spectrum": th.border_spectrum,
                        "border_vu": th.border_vu,
                        "border_party": th.border_party,
                        "border_dance": th.border_dance,
                    }
                    self._config.save()
                # Apply to live state or snapshot
                if self._sendspin_is_active():
                    self._state.theme = self._artwork_themes[channel]
                    self._state.artwork_data = image_data
                else:
                    self._state.write_to_snapshot("sendspin",
                        theme=self._artwork_themes[channel],
                        artwork_data=image_data,
                    )
                logger.info(
                    "Updated theme from album art ch%d: primary=%s",
                    channel, self._artwork_themes[channel].primary,
                )

        # Extract on a thread to avoid blocking the event loop
        threading.Thread(target=_extract_and_apply, daemon=True).start()

    def _on_artwork_cleared(self, channel: int) -> None:
        """Handle artwork cleared - revert to default colours."""
        logger.debug("Artwork cleared for channel %d", channel)
        self._artwork_themes.pop(channel, None)
        if channel == 0:
            clear_art_cache()
            if self._config:
                self._config.cached_theme = {}
            if self._sendspin_is_active():
                self._state.theme = ColorTheme()
                self._state.artwork_data = b""
            else:
                self._state.write_to_snapshot("sendspin",
                    theme=ColorTheme(), artwork_data=b"",
                )
                self._config.save()

    async def _apply_auto_settings(self) -> None:
        """Apply auto-play and auto-volume settings on connect."""
        cfg = self._config
        if cfg is None:
            return

        # Auto volume: set local volume immediately on connect
        if cfg.auto_volume >= 0:
            vol = max(0, min(100, cfg.auto_volume))
            self._state.set_source_volume("sendspin", vol)
            logger.info("Auto-volume: setting to %d%%", vol)
            if self._audio_handler is not None:
                _, ss_muted = self._state.get_source_volume("sendspin")
                self._audio_handler.set_volume(vol, muted=ss_muted)

        # Auto play: send play command on connect
        if cfg.auto_play and self._client is not None and self._client.connected:
            from aiosendspin.models.types import MediaCommand
            try:
                await self._client.send_group_command(MediaCommand.PLAY)
                logger.info("Auto-play: sent PLAY command")
            except Exception:
                logger.debug("Auto-play failed", exc_info=True)

    def _on_transport_command(self, command: str) -> None:
        """Handle a transport command from the TUI (called from input thread)."""
        state = self._state

        # Volume changes are per-source — apply to active source
        if command == "volume_up":
            src = state.active_source or "sendspin"
            cur_vol, cur_muted = state.get_source_volume(src)
            new_vol = min(100, cur_vol + 5)
            state.set_source_volume(src, new_vol, False if cur_muted else None)
            if cur_muted:
                logger.info("Unmuted via volume up")
            logger.info("Volume up (%s): %d%%", src, new_vol)
            if self._audio_handler is not None:
                muted = state.muted if src in ("sendspin", "") else True
                self._audio_handler.set_volume(new_vol if src in ("sendspin", "") else state.get_source_volume("sendspin")[0], muted=muted)
            return
        elif command == "volume_down":
            src = state.active_source or "sendspin"
            cur_vol, cur_muted = state.get_source_volume(src)
            new_vol = max(0, cur_vol - 5)
            state.set_source_volume(src, new_vol, False if cur_muted else None)
            if cur_muted:
                logger.info("Unmuted via volume down")
            logger.info("Volume down (%s): %d%%", src, new_vol)
            if self._audio_handler is not None:
                muted = state.muted if src in ("sendspin", "") else True
                self._audio_handler.set_volume(new_vol if src in ("sendspin", "") else state.get_source_volume("sendspin")[0], muted=muted)
            return
        elif command == "mute":
            src = state.active_source or "sendspin"
            cur_vol, cur_muted = state.get_source_volume(src)
            state.set_source_muted(src, not cur_muted)
            logger.info("Mute %s (%s)", "on" if not cur_muted else "off", src)
            if self._audio_handler is not None:
                ss_vol, ss_muted = state.get_source_volume("sendspin")
                self._audio_handler.set_volume(ss_vol, muted=ss_muted)
            return

        if self._client is None or not self._client.connected:
            logger.debug("Command '%s' dropped — not connected", command)
            return
        if self._loop is None:
            logger.debug("Command '%s' dropped — no event loop", command)
            return

        from aiosendspin.models.types import MediaCommand

        cmds = set(state.supported_commands)

        async def _send() -> None:
            assert self._client is not None
            try:
                if command == "play_pause":
                    if state.is_playing and "pause" in cmds:
                        logger.info("Sending command: PAUSE")
                        await self._client.send_group_command(MediaCommand.PAUSE)
                    elif not state.is_playing and "play" in cmds:
                        logger.info("Sending command: PLAY")
                        await self._client.send_group_command(MediaCommand.PLAY)
                elif command == "next" and "next" in cmds:
                    logger.info("Sending command: NEXT")
                    await self._client.send_group_command(MediaCommand.NEXT)
                elif command == "previous" and "previous" in cmds:
                    logger.info("Sending command: PREVIOUS")
                    await self._client.send_group_command(MediaCommand.PREVIOUS)
                elif command == "shuffle":
                    if state.shuffle and "unshuffle" in cmds:
                        state.shuffle = False
                        logger.info("Sending command: UNSHUFFLE")
                        await self._client.send_group_command(MediaCommand.UNSHUFFLE)
                    elif not state.shuffle and "shuffle" in cmds:
                        state.shuffle = True
                        logger.info("Sending command: SHUFFLE")
                        await self._client.send_group_command(MediaCommand.SHUFFLE)
                elif command == "repeat":
                    if state.repeat_mode == "off" and "repeat_all" in cmds:
                        state.repeat_mode = "all"
                        logger.info("Sending command: REPEAT_ALL")
                        await self._client.send_group_command(MediaCommand.REPEAT_ALL)
                    elif state.repeat_mode == "all" and "repeat_one" in cmds:
                        state.repeat_mode = "one"
                        logger.info("Sending command: REPEAT_ONE")
                        await self._client.send_group_command(MediaCommand.REPEAT_ONE)
                    elif state.repeat_mode == "one" and "repeat_off" in cmds:
                        state.repeat_mode = "off"
                        logger.info("Sending command: REPEAT_OFF")
                        await self._client.send_group_command(MediaCommand.REPEAT_OFF)
            except Exception:
                logger.exception("Error sending command: %s", command)

        asyncio.run_coroutine_threadsafe(_send(), self._loop)

    def _send_desktop_notification(self, title: str, artist: str, album: str) -> None:
        """Send a desktop notification for track change (Linux only)."""
        if sys.platform != "linux":
            return
        if not shutil.which("notify-send"):
            return
        summary = f"\u266a {title}"
        body = artist
        if album:
            body += f" \u2014 {album}"
        try:
            subprocess.Popen(
                ["notify-send", "--app-name=AlfiePRIME", "-t", "5000", summary, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    async def _run_demo_mode(self) -> None:
        """Demo mode with simulated audio."""
        self._state.connected = True
        self._state.server_name = "Demo Mode"
        self._state.group_name = "Party Room"
        self._state.is_playing = True
        self._state.codec = "PCM"
        self._state.sample_rate = 48000
        self._state.bit_depth = 16
        self._visualizer.set_format(48000, 16, 2)

        tracks = [
            ("Neon Dreams", "Synthwave Collective", "Midnight Drive", 234000),
            ("Bass Drop Protocol", "DJ Electron", "Circuit Breaker", 198000),
            ("Retrowave Sunset", "Chrome Future", "Analog Memories", 267000),
            ("Digital Groove", "Bit Crusher", "Sample Rate", 185000),
            ("Phantom Signal", "Ghost Frequency", "Spectral Analysis", 312000),
        ]
        track_idx = 0
        t_title, t_artist, t_album, t_duration = tracks[track_idx]
        self._state.title = t_title
        self._state.artist = t_artist
        self._state.album = t_album
        self._state.duration_ms = t_duration
        self._state.progress_ms = 0

        sample_rate = 48000
        chunk_size = 2048
        bytes_per_chunk = chunk_size * 2 * 2  # 16-bit stereo

        beat_bpm = 128
        beat_freq = beat_bpm / 60.0
        bass_freq = 60.0
        mid_freq = 440.0
        time_pos = 0.0
        dt = chunk_size / sample_rate

        while self._running:
            audio_data = bytearray(bytes_per_chunk)
            t = time_pos

            beat_phase = (t * beat_freq) % 1.0
            kick = max(0, 1.0 - beat_phase * 8) * 0.8
            snare_phase = ((t * beat_freq) + 0.5) % 1.0
            snare = max(0, 1.0 - snare_phase * 12) * 0.3

            for i in range(chunk_size):
                sample_t = t + i / sample_rate
                bass = math.sin(2 * math.pi * bass_freq * sample_t) * kick * 0.6
                melody_env = 0.3 + 0.2 * math.sin(2 * math.pi * 0.25 * sample_t)
                mid = math.sin(2 * math.pi * mid_freq * sample_t) * melody_env * 0.3
                mid += math.sin(2 * math.pi * mid_freq * 1.5 * sample_t) * melody_env * 0.15
                noise = (random.random() * 2 - 1) * snare * 0.2
                hi = math.sin(2 * math.pi * 8000 * sample_t) * 0.05

                left = max(-1.0, min(1.0, bass + mid + noise + hi + math.sin(2 * math.pi * 200 * sample_t) * 0.1))
                right = max(-1.0, min(1.0, bass + mid * 0.8 + noise + hi * 1.2 + math.sin(2 * math.pi * 250 * sample_t) * 0.1))

                offset = i * 4
                struct.pack_into("<hh", audio_data, offset, int(left * 32000), int(right * 32000))

            self._visualizer.feed_audio(bytes(audio_data))
            time_pos += dt

            self._state.progress_ms = int(time_pos * 1000) % t_duration
            if self._state.progress_ms < 100 and time_pos > 1.0:
                track_idx = (track_idx + 1) % len(tracks)
                t_title, t_artist, t_album, t_duration = tracks[track_idx]
                self._state.title = t_title
                self._state.artist = t_artist
                self._state.album = t_album
                self._state.duration_ms = t_duration

            await asyncio.sleep(chunk_size / sample_rate * 0.5)

    def stop(self) -> None:
        # Pause playback on Music Assistant before shutting down
        if (
            self._state.is_playing
            and self._client is not None
            and self._client.connected
            and self._loop is not None
        ):
            from aiosendspin.models.types import MediaCommand
            cmds = set(self._state.supported_commands)
            if "pause" in cmds:
                logger.info("Pausing playback before shutdown")
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._client.send_group_command(MediaCommand.PAUSE),
                        self._loop,
                    )
                    future.result(timeout=2.0)  # wait up to 2s for pause to send
                except Exception:
                    logger.debug("Failed to pause on shutdown", exc_info=True)
        self._stats.save()
        if self._mpris is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._mpris.stop(), self._loop)
        self._running = False
