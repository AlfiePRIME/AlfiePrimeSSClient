from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import platform
import random
import struct
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
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Pre-cached themes per artwork channel (channel 0 = current, 1+ = upcoming)
        self._artwork_themes: dict[int, ColorTheme] = {}

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

        self._state.server_name = f"Listening on :{self._listen_port}"
        self._state.connected = False
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

        async with self._connection_lock:
            # Clean up previous client if any
            if self._client is not None:
                logger.info("Disconnecting from previous server")
                self._state.connected = False
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
        self._state.connected = True
        self._state.server_name = server_name
        logger.info("Connected to server: %s (%s)", server_name, remote)

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
                self._state.connected = False
                self._state.is_playing = False
                self._state.server_name = f"Listening on :{self._listen_port}"
                await self._audio_handler.handle_disconnect()
                self._visualizer.reset()
                reset_vu_peaks()

    # ── Client-initiated mode (explicit URL) ──

    async def _connection_loop_url(self, url: str) -> None:
        """Connect to a specific URL with reconnection."""
        from aiohttp import ClientError

        self._state.server_name = url
        self._state.connected = False
        backoff = 1.0

        while self._running:
            try:
                logger.info("Connecting to %s", url)
                assert self._client is not None
                await self._client.connect(url)
                self._state.connected = True
                self._state.server_name = url
                backoff = 1.0

                # Wait for disconnect
                disconnect_event = asyncio.Event()
                unsub = self._client.add_disconnect_listener(disconnect_event.set)
                await disconnect_event.wait()
                unsub()

                self._state.connected = False
                self._state.is_playing = False
                if self._audio_handler:
                    await self._audio_handler.handle_disconnect()
                logger.info("Disconnected, reconnecting...")

            except (TimeoutError, OSError, ClientError) as e:
                logger.warning("Connection error (%s), retrying in %.0fs", type(e).__name__, backoff)
                self._state.connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
            except Exception:
                logger.exception("Unexpected connection error")
                break

    def _on_audio_chunk(
        self, server_timestamp_us: int, audio_data: bytes | bytearray, fmt: AudioFormat,
    ) -> None:
        """Feed audio to visualizer (audio playback is handled by AudioStreamHandler)."""
        from aiosendspin.models.types import AudioCodec

        pcm = fmt.pcm_format
        self._visualizer.set_format(pcm.sample_rate, pcm.bit_depth, pcm.channels)

        if fmt.codec == AudioCodec.PCM:
            self._visualizer.feed_audio(audio_data)
        elif fmt.codec == AudioCodec.FLAC:
            # Decode FLAC to PCM before feeding visualizer
            if self._flac_decoder is None or self._flac_fmt != fmt:
                from sendspin.decoder import FlacDecoder
                self._flac_decoder = FlacDecoder(fmt)
                self._flac_fmt = fmt
            decoded = self._flac_decoder.decode(audio_data)
            if decoded:
                self._visualizer.feed_audio(decoded)

    def _on_metadata(self, payload) -> None:
        """Handle metadata updates from server."""
        from aiosendspin.models.types import RepeatMode, UndefinedField

        state = self._state
        meta = payload.metadata
        if meta is None:
            return

        old_title = state.title
        if not isinstance(getattr(meta, "title", UndefinedField()), UndefinedField):
            state.title = meta.title or ""
        if not isinstance(getattr(meta, "artist", UndefinedField()), UndefinedField):
            state.artist = meta.artist or ""
        if not isinstance(getattr(meta, "album", UndefinedField()), UndefinedField):
            state.album = meta.album or ""

        # On track change, log and apply pre-cached artwork theme if available
        if state.title != old_title and state.title:
            logger.info("Now playing: %s - %s [%s]", state.artist or "?", state.title, state.album or "?")
            for ch in (1, 2, 3):
                cached = self._artwork_themes.get(ch)
                if cached is not None and cached.primary != _default_theme.primary:
                    state.theme = cached
                    logger.info("Applied pre-cached theme from channel %d for new track", ch)
                    self._artwork_themes[0] = cached
                    self._artwork_themes.pop(ch, None)
                    break

        repeat = getattr(meta, "repeat", UndefinedField())
        if not isinstance(repeat, UndefinedField) and repeat is not None:
            if repeat == RepeatMode.ONE:
                state.repeat_mode = "one"
            elif repeat == RepeatMode.ALL:
                state.repeat_mode = "all"
            else:
                state.repeat_mode = "off"

        shuffle = getattr(meta, "shuffle", UndefinedField())
        if not isinstance(shuffle, UndefinedField) and shuffle is not None:
            state.shuffle = shuffle

        progress = getattr(meta, "progress", UndefinedField())
        if not isinstance(progress, UndefinedField):
            if progress is not None:
                state.progress_ms = progress.track_progress or 0
                state.duration_ms = progress.track_duration or 0
                # playback_speed is multiplied by 1000 (1000 = normal)
                speed = progress.playback_speed
                state.playback_speed = (speed or 0) / 1000.0
                state.progress_update_time = time.monotonic()
                logger.debug(
                    "Progress update: %dms / %dms, speed=%s",
                    state.progress_ms, state.duration_ms, speed,
                )
            else:
                state.progress_ms = 0
                state.duration_ms = 0
                state.playback_speed = 0.0
                state.progress_update_time = 0.0

    def _on_group_update(self, payload) -> None:
        """Handle group update messages."""
        from aiosendspin.models.types import PlaybackStateType

        state = self._state
        if payload.group_name:
            state.group_name = payload.group_name
        if payload.playback_state:
            was_playing = state.is_playing
            state.is_playing = payload.playback_state == PlaybackStateType.PLAYING
            self._visualizer.set_paused(not state.is_playing)
            # Log state transitions
            if was_playing and not state.is_playing:
                state.progress_ms = state.get_interpolated_progress()
                state.progress_update_time = 0.0
                logger.info("Playback paused")
            elif state.is_playing and not was_playing:
                state.progress_update_time = time.monotonic()
                logger.info("Playback started")

    def _on_format_change(
        self, codec: str | None, sample_rate: int, bit_depth: int, channels: int,
    ) -> None:
        """Handle audio format changes."""
        self._state.codec = codec or "PCM"
        self._state.sample_rate = sample_rate
        self._state.bit_depth = bit_depth
        logger.info("Audio format: %s %dHz %dbit %dch", codec or "PCM", sample_rate, bit_depth, channels)
        self._visualizer.set_format(sample_rate, bit_depth, channels)

    def _on_stream_event(self, event: str) -> None:
        """Handle stream start/stop events."""
        self._state.is_playing = event == "start"
        self._visualizer.set_paused(event != "start")
        if event == "stop":
            # Only reset the audio pipeline — keep the visual state (bands,
            # peaks, VU) so the spectrum and meters decay gracefully on pause.
            self._visualizer.reset_pipeline()
            self._flac_decoder = None
            self._flac_fmt = None
        elif event == "start":
            # Reset decoder on new stream (format may change)
            self._flac_decoder = None
            self._flac_fmt = None

    def _on_controller_state(self, payload) -> None:
        """Handle controller state updates (supported commands, volume, mute)."""
        state = self._state
        ctrl = payload.controller
        if ctrl is None:
            return
        state.supported_commands = [cmd.value for cmd in ctrl.supported_commands]
        state.volume = ctrl.volume
        state.muted = ctrl.muted

    def _on_server_command(self, payload) -> None:
        """Handle server commands (volume, mute)."""
        from aiosendspin.models.types import PlayerCommand

        if payload.player is None:
            return
        cmd = payload.player
        if cmd.command == PlayerCommand.VOLUME and cmd.volume is not None:
            self._state.volume = cmd.volume
            logger.info("Volume set to %d%%", cmd.volume)
            if self._audio_handler is not None:
                self._audio_handler.set_volume(cmd.volume, muted=self._state.muted)
        elif cmd.command == PlayerCommand.MUTE and cmd.mute is not None:
            self._state.muted = cmd.mute
            logger.info("Mute %s", "on" if cmd.mute else "off")
            if self._audio_handler is not None:
                self._audio_handler.set_volume(self._state.volume, muted=cmd.mute)

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
                self._state.theme = self._artwork_themes[channel]
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
            self._state.theme = ColorTheme()

    def _on_transport_command(self, command: str) -> None:
        """Handle a transport command from the TUI (called from input thread)."""
        state = self._state

        # Volume changes are local — apply immediately even without server
        if command == "volume_up":
            new_vol = min(100, state.volume + 5)
            state.volume = new_vol
            logger.info("Volume up: %d%%", new_vol)
            if self._audio_handler is not None:
                self._audio_handler.set_volume(new_vol, muted=state.muted)
            return
        elif command == "volume_down":
            new_vol = max(0, state.volume - 5)
            state.volume = new_vol
            logger.info("Volume down: %d%%", new_vol)
            if self._audio_handler is not None:
                self._audio_handler.set_volume(new_vol, muted=state.muted)
            return

        if self._client is None or not self._client.connected:
            return
        if self._loop is None:
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
        self._running = False
