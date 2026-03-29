from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field

from alfieprime_musiciser.colors import ColorTheme


# Fields saved/restored when switching between sources.
_SNAPSHOT_FIELDS = (
    "title", "artist", "album", "album_artist", "year", "track_number",
    "progress_ms", "duration_ms", "is_playing",
    "codec", "sample_rate", "bit_depth",
    "playback_speed", "progress_update_time",
    "supported_commands", "repeat_mode", "shuffle",
    "group_name",
    "theme", "artwork_data",
)


# ─── Player State ────────────────────────────────────────────────────────────


@dataclass
class PlayerState:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: int = 0
    track_number: int = 0
    progress_ms: int = 0
    duration_ms: int = 0
    is_playing: bool = False
    server_name: str = ""
    sendspin_server_name: str = ""
    airplay_server_name: str = ""
    group_name: str = ""
    connected: bool = False
    codec: str = "pcm"
    sample_rate: int = 48000
    bit_depth: int = 16
    volume: int = 100
    muted: bool = False
    # Per-source volume/mute: {source: {"volume": int, "muted": bool}}
    _source_volumes: dict[str, dict[str, object]] = field(default_factory=dict, repr=False)
    # For progress interpolation between server updates
    playback_speed: float = 1.0  # 1.0 = normal, 0.0 = paused
    progress_update_time: float = 0.0  # monotonic time of last progress update
    # Controller state
    supported_commands: list[str] = field(default_factory=list)
    repeat_mode: str = "off"  # off, one, all
    shuffle: bool = False
    # Color theme from album art
    theme: ColorTheme = field(default_factory=ColorTheme)
    # Raw artwork bytes for braille art rendering (current track)
    artwork_data: bytes = b""
    # Session stats summary string (updated periodically by receiver)
    session_stats: str = ""
    # AirPlay pairing PIN (shown on connecting screen)
    airplay_pin: str = ""
    # AirPlay server is listening (not yet connected to a client)
    airplay_ready: bool = False
    # SendSpin server is listening (not yet connected to a client)
    sendspin_ready: bool = False
    # Per-protocol connection state
    sendspin_connected: bool = False
    airplay_connected: bool = False
    spotify_connected: bool = False
    spotify_ready: bool = False
    spotify_server_name: str = ""
    # Which protocol is currently active: "" (none), "sendspin", "airplay", "spotify"
    active_source: str = ""
    # Device swap prompt state
    swap_pending: bool = False
    swap_pending_source: str = ""  # "sendspin", "airplay", or "spotify"
    swap_pending_name: str = ""  # display name of the new device
    swap_response: str = ""  # "accept", "deny", or "" (pending)
    # Toast notification (auto-dismiss overlay)
    toast_message: str = ""  # message to display
    toast_detail: str = ""  # secondary detail line
    toast_expire: float = 0.0  # time.monotonic() when to dismiss
    # Per-source state snapshots: {"sendspin": {...}, "airplay": {...}, "spotify": {...}}
    _source_snapshots: dict[str, dict] = field(default_factory=dict, repr=False)

    def show_toast(self, message: str, detail: str = "", duration: float = 3.0) -> None:
        """Show a toast notification that auto-dismisses after *duration* seconds."""
        self.toast_message = message
        self.toast_detail = detail
        self.toast_expire = time.monotonic() + duration

    def set_source_volume(self, source: str, volume: int, muted: bool | None = None) -> None:
        """Set volume for a specific source. Updates live state if source is active."""
        sv = self._source_volumes.setdefault(source, {"volume": 100, "muted": False})
        sv["volume"] = volume
        if muted is not None:
            sv["muted"] = muted
        if self.active_source in (source, ""):
            self.volume = volume
            if muted is not None:
                self.muted = muted

    def set_source_muted(self, source: str, muted: bool) -> None:
        """Set muted for a specific source. Updates live state if source is active."""
        sv = self._source_volumes.setdefault(source, {"volume": 100, "muted": False})
        sv["muted"] = muted
        if self.active_source in (source, ""):
            self.muted = muted

    def get_source_volume(self, source: str) -> tuple[int, bool]:
        """Return (volume, muted) for a source."""
        sv = self._source_volumes.get(source)
        if sv is not None:
            return sv["volume"], sv["muted"]  # type: ignore[return-value]
        return self.volume, self.muted

    def get_interpolated_progress(self) -> int:
        """Get progress interpolated from last server update."""
        if not self.is_playing or self.progress_update_time <= 0 or self.duration_ms <= 0:
            return self.progress_ms
        elapsed = time.monotonic() - self.progress_update_time
        speed = self.playback_speed if self.playback_speed > 0 else 1.0
        interpolated = self.progress_ms + int(elapsed * 1000 * speed)
        return max(0, min(interpolated, self.duration_ms))

    def save_snapshot(self, source: str) -> None:
        """Save current display fields into the snapshot for *source*."""
        snap: dict = {}
        for f in _SNAPSHOT_FIELDS:
            val = getattr(self, f)
            if isinstance(val, list):
                val = list(val)
            elif isinstance(val, ColorTheme):
                val = dataclasses.replace(val)
            snap[f] = val
        self._source_snapshots[source] = snap
        # Persist current volume/muted into the source's volume store
        self._source_volumes[source] = {"volume": self.volume, "muted": self.muted}

    def restore_snapshot(self, source: str) -> None:
        """Restore display fields from the snapshot for *source*.

        If no snapshot exists, reset all snapshot fields to their dataclass
        defaults so the TUI shows the "waiting for music" idle screen.
        """
        snap = self._source_snapshots.get(source)
        if snap is None:
            # No data yet for this source — reset to defaults.
            defaults = PlayerState()
            for f in _SNAPSHOT_FIELDS:
                setattr(self, f, getattr(defaults, f))
        else:
            # Reset all snapshot fields to defaults first, then overlay
            # with whatever the snapshot contains.  This prevents stale
            # data (e.g. the previous source's theme) from bleeding through
            # when the snapshot is partial.
            defaults = PlayerState()
            for f in _SNAPSHOT_FIELDS:
                val = snap.get(f)
                if val is None:
                    val = getattr(defaults, f)
                if isinstance(val, list):
                    val = list(val)
                elif isinstance(val, ColorTheme):
                    val = dataclasses.replace(val)
                setattr(self, f, val)
        # Restore per-source volume/muted
        sv = self._source_volumes.get(source)
        if sv is not None:
            self.volume = sv["volume"]  # type: ignore[assignment]
            self.muted = sv["muted"]  # type: ignore[assignment]

    def write_to_snapshot(self, source: str, **fields: object) -> None:
        """Buffer field updates into *source*'s snapshot without touching live state."""
        snap = self._source_snapshots.setdefault(source, {})
        for k, v in fields.items():
            if isinstance(v, list):
                v = list(v)
            elif isinstance(v, ColorTheme):
                v = dataclasses.replace(v)
            snap[k] = v
