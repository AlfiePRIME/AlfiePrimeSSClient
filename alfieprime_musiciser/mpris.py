"""MPRIS2 D-Bus integration — advertise as a media player to the OS.

Exposes org.mpris.MediaPlayer2 and org.mpris.MediaPlayer2.Player on the
session bus so desktop environments, media keys, KDE Connect, etc. can
see what's playing and send play/pause/next/previous commands.

Requires: dbus-next (pip install dbus-next)
"""

# NOTE: Do NOT use `from __future__ import annotations` here.
# dbus-next inspects annotations eagerly at class-definition time;
# PEP 563 deferred evaluation breaks its signature parsing.

import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from dbus_next import BusType, Variant
    from dbus_next.aio import MessageBus
    from dbus_next.service import PropertyAccess, ServiceInterface, dbus_property, method, signal

    _HAS_DBUS = True
except ImportError:
    _HAS_DBUS = False

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_BUS_NAME = "org.mpris.MediaPlayer2.alfieprime_musiciser"
_OBJ_PATH = "/org/mpris/MediaPlayer2"
_ART_CACHE = Path(tempfile.gettempdir()) / "alfieprime-musiciser-art.jpg"


def _get_art_cache_path() -> Path:
    return _ART_CACHE


def write_art_cache(data: bytes) -> None:
    """Write artwork bytes to the temp file for MPRIS artUrl."""
    try:
        _ART_CACHE.write_bytes(data)
    except OSError:
        pass


def clear_art_cache() -> None:
    """Remove the cached artwork file."""
    try:
        _ART_CACHE.unlink(missing_ok=True)
    except OSError:
        pass


# ── D-Bus interfaces (only defined when dbus-next is available) ───────────

if _HAS_DBUS:
    from alfieprime_musiciser.state import PlayerState

    def _build_metadata(state: PlayerState) -> dict[str, Variant]:
        """Build the MPRIS Metadata dict from current player state."""
        meta: dict[str, Variant] = {
            "mpris:trackid": Variant("o", "/org/mpris/MediaPlayer2/CurrentTrack"),
        }
        if state.title:
            meta["xesam:title"] = Variant("s", state.title)
        if state.artist:
            meta["xesam:artist"] = Variant("as", [state.artist])
        if state.album:
            meta["xesam:album"] = Variant("s", state.album)
        if state.duration_ms > 0:
            meta["mpris:length"] = Variant("x", state.duration_ms * 1000)  # microseconds
        if state.artwork_data:
            art_path = _get_art_cache_path()
            if art_path.exists():
                meta["mpris:artUrl"] = Variant("s", art_path.as_uri())
        return meta

    class _RootInterface(ServiceInterface):
        """org.mpris.MediaPlayer2 — application identity and basic control."""

        def __init__(self) -> None:
            super().__init__("org.mpris.MediaPlayer2")

        @method()
        def Raise(self) -> None:  # noqa: N802
            pass

        @method()
        def Quit(self) -> None:  # noqa: N802
            pass

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":  # noqa: N802
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":  # noqa: N802
            return False

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":  # noqa: N802
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":  # noqa: N802
            return "AlfiePRIME Musiciser"

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":  # noqa: N802
            return "alfieprime-musiciser"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":  # noqa: N802
            return []

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":  # noqa: N802
            return []

    class _PlayerInterface(ServiceInterface):
        """org.mpris.MediaPlayer2.Player — playback state and controls."""

        def __init__(
            self,
            state: PlayerState,
            command_cb: Callable[[str], None],
        ) -> None:
            super().__init__("org.mpris.MediaPlayer2.Player")
            self._state = state
            self._command_cb = command_cb
            # Cache for change detection
            self._prev_playing: bool | None = None
            self._prev_title: str = ""
            self._prev_artist: str = ""
            self._prev_album: str = ""
            self._prev_artwork_id: int = 0
            self._prev_volume: int = -1
            self._prev_shuffle: bool | None = None
            self._prev_repeat: str = ""
            self._prev_connected: bool = False

        # ── Methods ──

        @method()
        def Next(self) -> None:  # noqa: N802
            logger.info("MPRIS: Next pressed")
            self._command_cb("next")

        @method()
        def Previous(self) -> None:  # noqa: N802
            logger.info("MPRIS: Previous pressed")
            self._command_cb("previous")

        @method()
        def Pause(self) -> None:  # noqa: N802
            logger.info("MPRIS: Pause pressed (is_playing=%s)", self._state.is_playing)
            if self._state.is_playing:
                self._command_cb("play_pause")

        @method()
        def PlayPause(self) -> None:  # noqa: N802
            logger.info("MPRIS: PlayPause pressed (is_playing=%s)", self._state.is_playing)
            self._command_cb("play_pause")

        @method()
        def Stop(self) -> None:  # noqa: N802
            logger.info("MPRIS: Stop pressed")
            if self._state.is_playing:
                self._command_cb("play_pause")

        @method()
        def Play(self) -> None:  # noqa: N802
            logger.info("MPRIS: Play pressed (is_playing=%s)", self._state.is_playing)
            if not self._state.is_playing:
                self._command_cb("play_pause")

        @method()
        def Seek(self, offset: "x") -> None:  # noqa: N802
            pass  # Seek not supported by SendSpin

        @method()
        def SetPosition(self, track_id: "o", position: "x") -> None:  # noqa: N802
            pass  # Seek not supported by SendSpin

        @method()
        def OpenUri(self, uri: "s") -> None:  # noqa: N802
            pass

        # ── Signals ──

        @signal()
        def Seeked(self) -> "x":  # noqa: N802
            return self._state.get_interpolated_progress() * 1000

        # ── Properties ──

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":  # noqa: N802
            if self._state.is_playing:
                return "Playing"
            if self._state.connected:
                return "Paused"
            return "Stopped"

        @dbus_property()
        def LoopStatus(self) -> "s":  # noqa: N802
            m = self._state.repeat_mode
            if m == "one":
                return "Track"
            if m == "all":
                return "Playlist"
            return "None"

        @LoopStatus.setter  # type: ignore[no-redef]
        def LoopStatus(self, value: "s") -> None:  # noqa: N802
            self._command_cb("repeat")

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":  # noqa: N802
            return 1.0

        @dbus_property()
        def Shuffle(self) -> "b":  # noqa: N802
            return self._state.shuffle

        @Shuffle.setter  # type: ignore[no-redef]
        def Shuffle(self, value: "b") -> None:  # noqa: N802
            if value != self._state.shuffle:
                self._command_cb("shuffle")

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":  # noqa: N802
            return _build_metadata(self._state)

        @dbus_property()
        def Volume(self) -> "d":  # noqa: N802
            if self._state.muted:
                return 0.0
            return self._state.volume / 100.0

        @Volume.setter  # type: ignore[no-redef]
        def Volume(self, value: "d") -> None:  # noqa: N802
            target = max(0, min(100, int(value * 100)))
            current = self._state.volume
            # Step volume up/down in 5% increments towards target
            while current != target:
                if target > current:
                    self._command_cb("volume_up")
                    current = min(current + 5, target)
                else:
                    self._command_cb("volume_down")
                    current = max(current - 5, target)

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":  # noqa: N802
            return self._state.get_interpolated_progress() * 1000  # microseconds

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":  # noqa: N802
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":  # noqa: N802
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":  # noqa: N802
            return "next" in self._state.supported_commands

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":  # noqa: N802
            return "previous" in self._state.supported_commands

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":  # noqa: N802
            return self._state.connected

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":  # noqa: N802
            return self._state.connected

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":  # noqa: N802
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":  # noqa: N802
            return self._state.connected

        # ── Property change emission ──

        def check_and_emit_changes(self) -> None:
            """Compare state to cached values and emit PropertiesChanged."""
            changed: dict[str, Variant] = {}

            if self._state.is_playing != self._prev_playing:
                self._prev_playing = self._state.is_playing
                changed["PlaybackStatus"] = Variant("s", self.PlaybackStatus)  # type: ignore[attr-defined]

            # Track metadata changes: title, artist, album, artwork
            art_id = id(self._state.artwork_data) if self._state.artwork_data else 0
            metadata_changed = (
                self._state.title != self._prev_title
                or self._state.artist != self._prev_artist
                or self._state.album != self._prev_album
                or art_id != self._prev_artwork_id
            )
            if metadata_changed:
                self._prev_title = self._state.title
                self._prev_artist = self._state.artist
                self._prev_album = self._state.album
                self._prev_artwork_id = art_id
                changed["Metadata"] = Variant("a{sv}", _build_metadata(self._state))

            if self._state.volume != self._prev_volume:
                self._prev_volume = self._state.volume
                changed["Volume"] = Variant("d", self.Volume)  # type: ignore[attr-defined]

            if self._state.shuffle != self._prev_shuffle:
                self._prev_shuffle = self._state.shuffle
                changed["Shuffle"] = Variant("b", self._state.shuffle)

            if self._state.repeat_mode != self._prev_repeat:
                self._prev_repeat = self._state.repeat_mode
                changed["LoopStatus"] = Variant("s", self.LoopStatus)  # type: ignore[attr-defined]

            # Update CanPlay/CanPause/CanControl when connection state changes
            if self._state.connected != self._prev_connected:
                self._prev_connected = self._state.connected
                changed["CanPlay"] = Variant("b", self._state.connected)
                changed["CanPause"] = Variant("b", self._state.connected)
                changed["CanControl"] = Variant("b", self._state.connected)

            if changed:
                self.emit_properties_changed(changed)


# ── Public API ────────────────────────────────────────────────────────────


class MPRIS2Server:
    """Manages the MPRIS2 D-Bus presence for the player.

    Safe to create even if dbus-next is not installed — it just does nothing.
    """

    def __init__(
        self,
        state: "PlayerState",
        command_cb: "Callable[[str], None]",
    ) -> None:
        self._state = state
        self._command_cb = command_cb
        self._bus = None
        self._player_iface = None
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Register on the session D-Bus. No-op if dbus-next is unavailable."""
        if not _HAS_DBUS:
            logger.debug("dbus-next not installed, MPRIS2 disabled")
            return

        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()
            root_iface = _RootInterface()
            player_iface = _PlayerInterface(self._state, self._command_cb)

            bus.export(_OBJ_PATH, root_iface)
            bus.export(_OBJ_PATH, player_iface)

            await bus.request_name(_BUS_NAME)
            self._bus = bus
            self._player_iface = player_iface
            logger.info("MPRIS2 registered on D-Bus as %s", _BUS_NAME)

            # Poll for state changes and emit PropertiesChanged
            self._poll_task = asyncio.create_task(self._poll_changes())

        except Exception:
            logger.debug("Failed to register MPRIS2 on D-Bus", exc_info=True)

    async def _poll_changes(self) -> None:
        """Periodically check for state changes and emit D-Bus signals."""
        while True:
            await asyncio.sleep(0.5)
            if self._player_iface is not None:
                try:
                    self._player_iface.check_and_emit_changes()
                except Exception:
                    logger.debug("MPRIS2 property emit error", exc_info=True)

    async def stop(self) -> None:
        """Disconnect from D-Bus."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None
            logger.info("MPRIS2 unregistered from D-Bus")
