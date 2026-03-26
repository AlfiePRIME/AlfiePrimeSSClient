from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from alfieprime_musiciser.config import CONFIG_DIR

logger = logging.getLogger(__name__)

STATS_FILE = CONFIG_DIR / "stats.json"


class ListeningStats:
    """Persistent listening statistics."""

    def __init__(self) -> None:
        self.total_seconds: float = 0.0
        self.total_tracks: int = 0
        # Per-artist and per-track cumulative seconds
        self.artist_seconds: dict[str, float] = {}
        self.track_seconds: dict[str, float] = {}
        # Current session
        self._session_start: float = time.monotonic()
        self.session_seconds: float = 0.0
        self.session_tracks: int = 0
        # Current track timing
        self._current_artist: str = ""
        self._current_title: str = ""
        self._track_start: float = 0.0
        self._playing: bool = False
        # Save throttle
        self._last_save: float = 0.0

        self._load()

    def _load(self) -> None:
        if not STATS_FILE.exists():
            return
        try:
            data = json.loads(STATS_FILE.read_text())
            self.total_seconds = data.get("total_seconds", 0.0)
            self.total_tracks = data.get("total_tracks", 0)
            self.artist_seconds = data.get("artist_seconds", {})
            self.track_seconds = data.get("track_seconds", {})
        except (json.JSONDecodeError, OSError):
            logger.debug("Failed to load stats", exc_info=True)

    def save(self) -> None:
        """Write stats to disk."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            # Keep only top 50 artists/tracks to avoid unbounded growth
            top_artists = dict(sorted(self.artist_seconds.items(), key=lambda x: x[1], reverse=True)[:50])
            top_tracks = dict(sorted(self.track_seconds.items(), key=lambda x: x[1], reverse=True)[:50])
            data = {
                "total_seconds": self.total_seconds,
                "total_tracks": self.total_tracks,
                "artist_seconds": top_artists,
                "track_seconds": top_tracks,
            }
            STATS_FILE.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            logger.debug("Failed to save stats", exc_info=True)

    def on_playing(self, playing: bool) -> None:
        """Call when playback state changes."""
        if playing and not self._playing:
            self._track_start = time.monotonic()
        elif not playing and self._playing:
            self._flush_current()
        self._playing = playing

    def on_track_change(self, artist: str, title: str) -> None:
        """Call when the track changes."""
        self._flush_current()
        self._current_artist = artist
        self._current_title = title
        self._track_start = time.monotonic()
        self.total_tracks += 1
        self.session_tracks += 1
        self._auto_save()

    def _flush_current(self) -> None:
        """Accumulate time for the current track."""
        if self._track_start <= 0 or not self._playing:
            return
        elapsed = time.monotonic() - self._track_start
        if elapsed < 1.0:
            return
        self.total_seconds += elapsed
        self.session_seconds += elapsed
        if self._current_artist:
            self.artist_seconds[self._current_artist] = (
                self.artist_seconds.get(self._current_artist, 0.0) + elapsed
            )
        if self._current_title:
            key = f"{self._current_artist} - {self._current_title}" if self._current_artist else self._current_title
            self.track_seconds[key] = self.track_seconds.get(key, 0.0) + elapsed
        self._track_start = time.monotonic()

    def _auto_save(self) -> None:
        now = time.monotonic()
        if now - self._last_save > 30.0:
            self.save()
            self._last_save = now

    def tick(self) -> None:
        """Call periodically to flush and auto-save."""
        if self._playing:
            self._flush_current()
        self._auto_save()

    def get_session_summary(self) -> str:
        """Short session summary for the status bar."""
        mins = int(self.session_seconds // 60)
        secs = int(self.session_seconds % 60)
        if mins > 60:
            hours = mins // 60
            mins = mins % 60
            time_str = f"{hours}h{mins:02d}m"
        elif mins > 0:
            time_str = f"{mins}m{secs:02d}s"
        else:
            time_str = f"{secs}s"
        return f"{self.session_tracks} tracks, {time_str}"

    def get_top_artists(self, n: int = 5) -> list[tuple[str, float]]:
        """Top N artists by listening time."""
        return sorted(self.artist_seconds.items(), key=lambda x: x[1], reverse=True)[:n]

    def get_top_tracks(self, n: int = 5) -> list[tuple[str, float]]:
        """Top N tracks by listening time."""
        return sorted(self.track_seconds.items(), key=lambda x: x[1], reverse=True)[:n]
