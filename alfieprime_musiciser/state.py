from __future__ import annotations

import time
from dataclasses import dataclass, field

from alfieprime_musiciser.colors import ColorTheme


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
    group_name: str = ""
    connected: bool = False
    codec: str = "pcm"
    sample_rate: int = 48000
    bit_depth: int = 16
    volume: int = 100
    muted: bool = False
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

    def get_interpolated_progress(self) -> int:
        """Get progress interpolated from last server update."""
        if not self.is_playing or self.progress_update_time <= 0 or self.duration_ms <= 0:
            return self.progress_ms
        elapsed = time.monotonic() - self.progress_update_time
        speed = self.playback_speed if self.playback_speed > 0 else 1.0
        interpolated = self.progress_ms + int(elapsed * 1000 * speed)
        return max(0, min(interpolated, self.duration_ms))
