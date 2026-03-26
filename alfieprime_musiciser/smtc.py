"""Windows System Media Transport Controls (SMTC) integration.

Exposes the player to Windows' media overlay (media keys, lock screen,
taskbar thumbnail, volume flyout, etc.) so the OS can display track info
and route play/pause/next/previous commands.

Requires: winsdk (pip install winsdk)
"""

# NOTE: Do NOT use `from __future__ import annotations` here.
# winsdk / winrt inspect annotations eagerly; PEP 563 breaks them.

import asyncio
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from alfieprime_musiciser.state import PlayerState

logger = logging.getLogger(__name__)

_HAS_SMTC = False

if sys.platform == "win32":
    try:
        from winsdk.windows.media import (
            MediaPlaybackStatus,
            MediaPlaybackType,
            SystemMediaTransportControls,
            SystemMediaTransportControlsButton,
            SystemMediaTransportControlsButtonPressedEventArgs,
            SystemMediaTransportControlsDisplayUpdater,
            SystemMediaTransportControlsTimelineProperties,
        )
        from winsdk.windows.media.playback import MediaPlayer
        from winsdk.windows.storage import StorageFile
        from winsdk.windows.storage.streams import (
            RandomAccessStreamReference,
        )
        from winsdk.windows.foundation import Uri, TimeSpan

        _HAS_SMTC = True
    except ImportError:
        try:
            # Try the newer winrt split packages
            from winrt.windows.media import (  # type: ignore[no-redef]
                MediaPlaybackStatus,
                MediaPlaybackType,
                SystemMediaTransportControls,
                SystemMediaTransportControlsButton,
                SystemMediaTransportControlsButtonPressedEventArgs,
                SystemMediaTransportControlsDisplayUpdater,
                SystemMediaTransportControlsTimelineProperties,
            )
            from winrt.windows.media.playback import MediaPlayer  # type: ignore[no-redef]
            from winrt.windows.storage import StorageFile  # type: ignore[no-redef]
            from winrt.windows.storage.streams import (  # type: ignore[no-redef]
                RandomAccessStreamReference,
            )
            from winrt.windows.foundation import Uri, TimeSpan  # type: ignore[no-redef]

            _HAS_SMTC = True
        except ImportError:
            pass

_ART_CACHE = Path(tempfile.gettempdir()) / "alfieprime-musiciser-art.jpg"


def _ticks(ms: int) -> int:
    """Convert milliseconds to Windows TimeSpan ticks (100ns units)."""
    return ms * 10_000


class SMTCServer:
    """Manages the Windows System Media Transport Controls for the player.

    Safe to create even if winsdk/winrt is not installed — it just does nothing.
    """

    def __init__(
        self,
        state: "PlayerState",
        command_cb: "Callable[[str], None]",
    ) -> None:
        self._state = state
        self._command_cb = command_cb
        self._player: "MediaPlayer | None" = None
        self._smtc: "SystemMediaTransportControls | None" = None
        self._poll_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Change detection cache
        self._prev_playing: bool | None = None
        self._prev_title: str = ""
        self._prev_volume: int = -1
        self._prev_progress: int = -1

    async def start(self) -> None:
        """Register with Windows SMTC. No-op if winsdk/winrt is unavailable."""
        if not _HAS_SMTC:
            logger.debug("winsdk/winrt not installed, SMTC disabled")
            return

        self._loop = asyncio.get_running_loop()

        try:
            # MediaPlayer gives us access to SMTC without needing a UWP context
            player = MediaPlayer()
            # Disable automatic command handling — we route manually
            player.command_manager.is_enabled = False
            smtc = player.system_media_transport_controls

            # Enable buttons
            smtc.is_play_enabled = True
            smtc.is_pause_enabled = True
            smtc.is_next_enabled = True
            smtc.is_previous_enabled = True
            smtc.is_stop_enabled = True

            # Register button handler
            smtc.add_button_pressed(self._on_button_pressed)

            self._player = player
            self._smtc = smtc

            # Initial metadata push
            self._update_display()
            self._update_playback_status()

            # Poll for state changes
            self._poll_task = asyncio.create_task(self._poll_changes())

            logger.info("Windows SMTC registered")

        except Exception:
            logger.debug("Failed to register Windows SMTC", exc_info=True)

    def _on_button_pressed(
        self,
        sender: "SystemMediaTransportControls",
        args: "SystemMediaTransportControlsButtonPressedEventArgs",
    ) -> None:
        """Handle media button presses from Windows."""
        button = args.button

        if button == SystemMediaTransportControlsButton.PLAY:
            if not self._state.is_playing:
                self._command_cb("play_pause")
        elif button == SystemMediaTransportControlsButton.PAUSE:
            if self._state.is_playing:
                self._command_cb("play_pause")
        elif button == SystemMediaTransportControlsButton.STOP:
            if self._state.is_playing:
                self._command_cb("play_pause")
        elif button == SystemMediaTransportControlsButton.NEXT:
            self._command_cb("next")
        elif button == SystemMediaTransportControlsButton.PREVIOUS:
            self._command_cb("previous")

    def _update_playback_status(self) -> None:
        """Sync playback status to SMTC."""
        if self._smtc is None:
            return
        try:
            if self._state.is_playing:
                self._smtc.playback_status = MediaPlaybackStatus.PLAYING
            elif self._state.connected:
                self._smtc.playback_status = MediaPlaybackStatus.PAUSED
            else:
                self._smtc.playback_status = MediaPlaybackStatus.STOPPED
        except Exception:
            logger.debug("SMTC status update failed", exc_info=True)

    def _update_display(self) -> None:
        """Push current metadata to SMTC display."""
        if self._smtc is None:
            return
        try:
            updater = self._smtc.display_updater
            updater.type = MediaPlaybackType.MUSIC
            updater.music_properties.title = self._state.title or ""
            updater.music_properties.artist = self._state.artist or ""
            updater.music_properties.album_title = self._state.album or ""

            # Set album art thumbnail if available
            if self._state.artwork_data and _ART_CACHE.exists():
                try:
                    uri = Uri(str(_ART_CACHE))
                    updater.thumbnail = RandomAccessStreamReference.create_from_uri(uri)
                except Exception:
                    pass

            updater.update()
        except Exception:
            logger.debug("SMTC display update failed", exc_info=True)

    def _update_timeline(self) -> None:
        """Push current playback position to SMTC."""
        if self._smtc is None:
            return
        try:
            timeline = SystemMediaTransportControlsTimelineProperties()
            timeline.start_time = TimeSpan(_ticks(0))
            timeline.end_time = TimeSpan(_ticks(self._state.duration_ms))
            timeline.position = TimeSpan(
                _ticks(self._state.get_interpolated_progress())
            )
            timeline.min_seek_time = TimeSpan(_ticks(0))
            timeline.max_seek_time = TimeSpan(_ticks(self._state.duration_ms))
            self._smtc.update_timeline_properties(timeline)
        except Exception:
            logger.debug("SMTC timeline update failed", exc_info=True)

    async def _poll_changes(self) -> None:
        """Periodically check for state changes and update SMTC."""
        while True:
            await asyncio.sleep(1.0)
            try:
                # Playback status
                if self._state.is_playing != self._prev_playing:
                    self._prev_playing = self._state.is_playing
                    self._update_playback_status()

                # Metadata (track change)
                if self._state.title != self._prev_title:
                    self._prev_title = self._state.title
                    self._update_display()

                # Timeline position (every poll when playing)
                if self._state.is_playing or self._state.get_interpolated_progress() != self._prev_progress:
                    self._prev_progress = self._state.get_interpolated_progress()
                    self._update_timeline()

                # Button availability based on supported commands
                if self._smtc is not None:
                    cmds = set(self._state.supported_commands)
                    self._smtc.is_next_enabled = "next" in cmds
                    self._smtc.is_previous_enabled = "previous" in cmds

            except Exception:
                logger.debug("SMTC poll error", exc_info=True)

    async def stop(self) -> None:
        """Clean up SMTC registration."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._smtc is not None:
            try:
                self._smtc.playback_status = MediaPlaybackStatus.CLOSED
            except Exception:
                pass
            self._smtc = None
        if self._player is not None:
            try:
                self._player.close()
            except Exception:
                pass
            self._player = None
            logger.info("Windows SMTC unregistered")
