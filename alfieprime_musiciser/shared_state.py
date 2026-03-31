"""Shared-memory proxies for cross-process TUI isolation.

Provides duck-typed replacements for DJState, AudioVisualizer, and a
lightweight mixer-diagnostic object so the TUI can run in a separate
process while the audio/control logic stays in the main process.
"""
from __future__ import annotations

import multiprocessing
import time
from dataclasses import dataclass, field

from alfieprime_musiciser.colors import ColorTheme

# ── Fields that get synced from control → TUI ────────────────────────────────

# PlayerState scalar fields (set via setattr in the receiver thread)
STATE_FIELDS: list[str] = [
    "title", "artist", "album", "album_artist", "year", "track_number",
    "progress_ms", "duration_ms", "is_playing",
    "server_name", "sendspin_server_name", "airplay_server_name",
    "spotify_server_name", "group_name",
    "connected", "codec", "sample_rate", "bit_depth",
    "volume", "muted", "playback_speed",
    "supported_commands", "repeat_mode", "shuffle",
    "session_stats", "airplay_pin",
    "airplay_ready", "sendspin_ready",
    "sendspin_connected", "airplay_connected",
    "spotify_connected", "spotify_ready",
    "active_source",
    "swap_pending", "swap_pending_source", "swap_pending_name",
    "toast_message", "toast_detail", "toast_expire",
]


# ── Shared DJ State ──────────────────────────────────────────────────────────
# Array layout: [crossfader, a_vol, a_bass, a_mid, a_treble,
#                             b_vol, b_bass, b_mid, b_treble, active_ch]

_IDX_CROSSFADER = 0
_IDX_A_VOL = 1
_IDX_A_BASS = 2
_IDX_A_MID = 3
_IDX_A_TREBLE = 4
_IDX_B_VOL = 5
_IDX_B_BASS = 6
_IDX_B_MID = 7
_IDX_B_TREBLE = 8
_IDX_ACTIVE = 9  # 0.0 = 'a', 1.0 = 'b'
DJ_ARRAY_SIZE = 10


class _SharedChannelState:
    """Proxy for ChannelState backed by a shared Array slice."""

    __slots__ = ("_arr", "_base")

    def __init__(self, arr: multiprocessing.Array, base: int) -> None:
        self._arr = arr
        self._base = base

    @property
    def volume(self) -> int:
        return int(self._arr[self._base])

    @volume.setter
    def volume(self, v: int) -> None:
        self._arr[self._base] = float(v)

    @property
    def eq_bass(self) -> int:
        return int(self._arr[self._base + 1])

    @eq_bass.setter
    def eq_bass(self, v: int) -> None:
        self._arr[self._base + 1] = float(v)

    @property
    def eq_mid(self) -> int:
        return int(self._arr[self._base + 2])

    @eq_mid.setter
    def eq_mid(self, v: int) -> None:
        self._arr[self._base + 2] = float(v)

    @property
    def eq_treble(self) -> int:
        return int(self._arr[self._base + 3])

    @eq_treble.setter
    def eq_treble(self, v: int) -> None:
        self._arr[self._base + 3] = float(v)


class SharedDJState:
    """Drop-in replacement for DJState backed by multiprocessing.Array.

    Both the TUI (writer) and the DJMixer (reader) access the same
    shared memory — no serialisation or pipe round-trip needed.
    """

    def __init__(self, arr: multiprocessing.Array | None = None) -> None:
        if arr is None:
            arr = multiprocessing.Array("d", DJ_ARRAY_SIZE)
            # Defaults
            arr[_IDX_CROSSFADER] = 0.5
            arr[_IDX_A_VOL] = 100.0
            arr[_IDX_B_VOL] = 100.0
        self._arr = arr
        self.channel_a = _SharedChannelState(arr, _IDX_A_VOL)
        self.channel_b = _SharedChannelState(arr, _IDX_B_VOL)

    @property
    def crossfader(self) -> float:
        return self._arr[_IDX_CROSSFADER]

    @crossfader.setter
    def crossfader(self, v: float) -> None:
        self._arr[_IDX_CROSSFADER] = v

    @property
    def active_channel(self) -> str:
        return "b" if self._arr[_IDX_ACTIVE] > 0.5 else "a"

    @active_channel.setter
    def active_channel(self, v: str) -> None:
        self._arr[_IDX_ACTIVE] = 1.0 if v == "b" else 0.0

    def get_focused(self):
        return self.channel_a if self.active_channel == "a" else self.channel_b

    def reset_eq(self, channel: str = "") -> None:
        ch = channel or self.active_channel
        target = self.channel_a if ch == "a" else self.channel_b
        target.eq_bass = 0
        target.eq_mid = 0
        target.eq_treble = 0


# ── Visualizer Proxy ─────────────────────────────────────────────────────────

class VisualizerProxy:
    """Duck-typed AudioVisualizer for the TUI process.

    Spectrum / beat / BPM data is written by the state-receiver thread
    from data pushed by the control process.  The TUI render loop reads
    it through the same ``get_spectrum()`` / ``get_beat()`` / ``get_bpm()``
    API as the real visualizer.
    """

    def __init__(self) -> None:
        self._bands: list[float] = [0.0] * 32
        self._peaks: list[float] = [0.0] * 32
        self._vu_left: float = 0.0
        self._vu_right: float = 0.0
        self._beat_count: int = 0
        self._beat_intensity: float = 0.0
        self._bpm: float = 0.0
        self._paused: bool = False

    # ── Read API (called by TUI render) ──

    def get_spectrum(self):
        return (list(self._bands), list(self._peaks),
                self._vu_left, self._vu_right)

    def get_beat(self):
        return (self._beat_count, self._beat_intensity)

    def get_bpm(self):
        return self._bpm

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    # No-ops for API compat
    def set_format(self, *_a, **_kw) -> None:
        pass

    def feed_audio(self, *_a, **_kw) -> None:
        pass

    def feed_audio_float32(self, *_a, **_kw) -> None:
        pass

    def get_raw_bytes(self, count: int = 512) -> bytes:
        return b"\x00" * count

    # ── Write API (called by state-receiver thread) ──

    def _update(self, data: dict) -> None:
        """Update from a state dict sent by the control process."""
        if "bands" in data:
            self._bands = data["bands"]
        if "peaks" in data:
            self._peaks = data["peaks"]
        if "vu_left" in data:
            self._vu_left = data["vu_left"]
        if "vu_right" in data:
            self._vu_right = data["vu_right"]
        if "beat_count" in data:
            self._beat_count = data["beat_count"]
        if "beat_intensity" in data:
            self._beat_intensity = data["beat_intensity"]
        if "bpm" in data:
            self._bpm = data["bpm"]


# ── Mixer Diagnostic Proxy ───────────────────────────────────────────────────

class MixerDiagProxy:
    """Provides ``_feed_a_count`` etc. so the DJ status bar can render."""

    __slots__ = ("_feed_a_count", "_feed_b_count", "_mix_count", "_ring_b_reads")

    def __init__(self) -> None:
        self._feed_a_count = 0
        self._feed_b_count = 0
        self._mix_count = 0
        self._ring_b_reads = 0

    def _update(self, data: dict) -> None:
        diag = data.get("dj_diag")
        if diag:
            self._feed_a_count, self._feed_b_count, \
                self._mix_count, self._ring_b_reads = diag


# ── State packing (control process side) ─────────────────────────────────────

def pack_state(state, visualizer, dj_mixer=None,
               dj_viz_a=None, dj_viz_b=None,
               dj_active: bool = False,
               source_b_data: dict | None = None) -> dict:
    """Pack current state into a dict for sending to the TUI process."""
    data: dict = {}

    # Scalar PlayerState fields
    for f in STATE_FIELDS:
        data[f] = getattr(state, f, None)

    # ColorTheme — send as dict for pickle
    import dataclasses as _dc
    th = state.theme
    data["theme_dict"] = {f.name: getattr(th, f.name) for f in _dc.fields(th)}

    # progress_update_time must be tagged so TUI can re-base to its own clock
    data["_progress_update_mono"] = state.progress_update_time

    # Source volumes and snapshots (for DJ source data)
    data["_source_volumes"] = dict(state._source_volumes)
    data["_source_snapshots"] = dict(state._source_snapshots)

    # Visualizer
    bands, peaks, vu_l, vu_r = visualizer.get_spectrum()
    beat_count, beat_intensity = visualizer.get_beat()
    data["bands"] = bands
    data["peaks"] = peaks
    data["vu_left"] = vu_l
    data["vu_right"] = vu_r
    data["beat_count"] = beat_count
    data["beat_intensity"] = beat_intensity
    data["bpm"] = visualizer.get_bpm()

    # DJ mode
    data["dj_active"] = dj_active
    if dj_active and dj_viz_a is not None:
        a_b, a_p, a_vl, a_vr = dj_viz_a.get_spectrum()
        a_bc, a_bi = dj_viz_a.get_beat()
        data["dj_viz_a"] = (a_b, a_p, a_vl, a_vr, a_bc, a_bi)
    if dj_active and dj_viz_b is not None:
        b_b, b_p, b_vl, b_vr = dj_viz_b.get_spectrum()
        b_bc, b_bi = dj_viz_b.get_beat()
        data["dj_viz_b"] = (b_b, b_p, b_vl, b_vr, b_bc, b_bi)
    if dj_active and dj_mixer is not None:
        data["dj_diag"] = (
            dj_mixer._feed_a_count, dj_mixer._feed_b_count,
            dj_mixer._mix_count, dj_mixer._ring_b_reads,
        )
    if source_b_data is not None:
        data["source_b_data"] = source_b_data

    return data
