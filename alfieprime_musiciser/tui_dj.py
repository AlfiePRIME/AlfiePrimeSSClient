"""DJ mixing console screen mixin for BoomBoxTUI."""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from alfieprime_musiciser.colors import ColorTheme, blend_themes, _lerp_color
from alfieprime_musiciser.dj_state import DJState, ChannelState
from alfieprime_musiciser.renderer import _cached_style, render_spectrum
from alfieprime_musiciser.visualizer import AudioVisualizer

if TYPE_CHECKING:
    from alfieprime_musiciser.dj_mixer import DJMixer
    from alfieprime_musiciser.state import PlayerState

logger = logging.getLogger(__name__)

# Attach file handler so DJ diagnostics appear in airplay_debug.log
def _setup_tui_dj_log() -> None:
    import os
    log_file = os.path.join(os.path.expanduser("~"), ".cache", "alfieprime", "airplay_debug.log")
    if os.path.isdir(os.path.dirname(log_file)):
        for h in logger.handlers:
            if isinstance(h, logging.FileHandler):
                return
        try:
            fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            fh.setLevel(logging.DEBUG)
            logger.addHandler(fh)
            logger.setLevel(logging.DEBUG)
        except Exception:
            pass
_setup_tui_dj_log()


# ── Turntable ASCII frames (7 lines × 13 cols) ──────────────────────────────

_PLATTER_FRAMES = [
    [
        "  ╭───────╮  ",
        " ╱ ╭─────╮ ╲ ",
        "│ ╱  ╭─╮  ╲ │",
        "│ │  │●│  │ │",
        "│ ╲  ╰─╯  ╱ │",
        " ╲ ╰─────╯ ╱ ",
        "  ╰───────╯  ",
    ],
    [
        "  ╭───────╮  ",
        " ╱ ╭─────╮ ╲ ",
        "│ ╱  ╭─╮  ╲ │",
        "│ │  │◐│  │ │",
        "│ ╲  ╰─╯  ╱ │",
        " ╲ ╰─────╯ ╱ ",
        "  ╰───────╯  ",
    ],
    [
        "  ╭───────╮  ",
        " ╱ ╭─────╮ ╲ ",
        "│ ╱  ╭─╮  ╲ │",
        "│ │  │○│  │ │",
        "│ ╲  ╰─╯  ╱ │",
        " ╲ ╰─────╯ ╱ ",
        "  ╰───────╯  ",
    ],
    [
        "  ╭───────╮  ",
        " ╱ ╭─────╮ ╲ ",
        "│ ╱  ╭─╮  ╲ │",
        "│ │  │◑│  │ │",
        "│ ╲  ╰─╯  ╱ │",
        " ╲ ╰─────╯ ╱ ",
        "  ╰───────╯  ",
    ],
]

# Groove ring characters that rotate to simulate vinyl spin
_GROOVE_CHARS = ["~", "≈", "~", "≈"]


def _render_turntable(
    theme: ColorTheme,
    title: str,
    artist: str,
    is_playing: bool,
    beat_intensity: float,
    t: float,
    focused: bool,
    label: str,
    width: int,
    connected: bool = True,
    progress: float = 0.0,
) -> list[Text]:
    """Render a single turntable with spinning vinyl effect."""
    lines: list[Text] = []

    # Dim everything when source is disconnected
    if not connected:
        is_playing = False
        beat_intensity = 0.0

    frame_idx = int(t * 3) % 4 if is_playing else 0
    platter = _PLATTER_FRAMES[frame_idx]

    # Groove shimmer based on time
    groove_offset = int(t * 6) % 4 if is_playing else 0
    gc = _GROOVE_CHARS[groove_offset]

    # Colors — brighten on beat
    beat_boost = min(1.0, beat_intensity * 0.5) if is_playing else 0.0
    if not connected:
        border_color = "#333333"
        vinyl_color = "#222222"
        label_color = "#444444"
        groove_color = "#333333"
        focus_indicator = "#333333" if not focused else "#555555"
    else:
        border_color = _lerp_color(theme.primary_dim, theme.primary, 0.5 + beat_boost * 0.5)
        vinyl_color = _lerp_color("#333333", theme.primary_dim, 0.3 + beat_boost * 0.2)
        label_color = theme.accent if is_playing else "#555555"
        groove_color = _lerp_color("#444444", theme.primary, beat_boost)
        focus_indicator = theme.accent if focused else "#444444"

    # Channel label
    ch_line = Text()
    lbl = f"─── {label} ───"
    pad = max(0, (width - len(lbl)) // 2)
    ch_line.append(" " * pad)
    ch_line.append(lbl, _cached_style(focus_indicator, bold=focused))
    lines.append(ch_line)

    # Tonearm — position moves inward as track progresses
    arm_line = Text()
    if is_playing:
        # Arm position: 0=outer (8 spaces), 1.0=inner (5 spaces)
        arm_offset = int(8 - progress * 3)
        arm_offset = max(5, min(8, arm_offset))
        arm = " " * arm_offset + "╲│"
    else:
        arm = "          │"
    arm_pad = max(0, (width - len(arm)) // 2)
    arm_line.append(" " * arm_pad)
    arm_color = _lerp_color("#888888", theme.accent, beat_boost * 0.3) if connected else "#555555"
    arm_line.append(arm, _cached_style(arm_color))
    lines.append(arm_line)

    # Platter with groove rings
    for i, row in enumerate(platter):
        line = Text()
        pad_l = max(0, (width - 13) // 2)
        line.append(" " * pad_l)
        for ch in row:
            if ch in ("╭", "╮", "╰", "╯", "─", "│", "╱", "╲"):
                line.append(ch, _cached_style(border_color))
            elif ch in ("●", "◐", "○", "◑"):
                line.append(ch, _cached_style(label_color, bold=True))
            elif ch in ("─", "╭", "╮", "╰", "╯") and i in (2, 3, 4):
                line.append(ch, _cached_style(label_color))
            elif ch == " " and 1 <= i <= 5:
                # Inside the platter — show grooves
                line.append(gc if is_playing else "·", _cached_style(groove_color if is_playing else vinyl_color))
            else:
                line.append(ch, _cached_style(vinyl_color))
        lines.append(line)

    # Track info under turntable
    lines.append(Text(""))
    title_line = Text()
    t_display = (title[:width - 4] + "…") if len(title) > width - 3 else title
    t_pad = max(0, (width - len(t_display)) // 2)
    title_line.append(" " * t_pad)
    title_line.append(t_display, _cached_style(theme.secondary, bold=True))
    lines.append(title_line)

    artist_line = Text()
    a_display = (artist[:width - 4] + "…") if len(artist) > width - 3 else artist
    a_pad = max(0, (width - len(a_display)) // 2)
    artist_line.append(" " * a_pad)
    artist_line.append(a_display, _cached_style(theme.primary_dim))
    lines.append(artist_line)

    return lines


def _render_eq_slider(
    label: str, value: int, width: int, theme: ColorTheme, focused: bool,
) -> Text:
    """Render a horizontal EQ slider: L ═══■═══ +12."""
    line = Text()
    line.append(f" {label} ", _cached_style(theme.secondary if focused else "#666666", bold=focused))

    # Slider track
    track_w = max(5, width - 10)
    center = track_w // 2
    # Map value (-12..+12) to position (0..track_w-1)
    pos = int((value + 12) / 24 * (track_w - 1) + 0.5)
    pos = max(0, min(track_w - 1, pos))

    for i in range(track_w):
        if i == pos:
            line.append("■", _cached_style(theme.accent, bold=True))
        elif i == center:
            line.append("┼", _cached_style("#555555"))
        else:
            line.append("═", _cached_style("#444444"))

    val_str = f"{value:+3d}"
    line.append(f" {val_str}", _cached_style(theme.primary_dim))
    return line


def _render_volume_fader(
    volume: int, height: int, theme: ColorTheme, label: str, focused: bool,
) -> list[Text]:
    """Render a vertical volume fader."""
    lines: list[Text] = []
    header = Text()
    header.append(f"  {label}  ", _cached_style(theme.secondary if focused else "#666666", bold=focused))
    lines.append(header)

    filled = int(volume / 100 * height + 0.5)
    for i in range(height):
        row_idx = height - 1 - i  # top to bottom
        line = Text()
        if row_idx < filled:
            # Filled section — color gradient
            frac = row_idx / max(1, height - 1)
            if frac > 0.7:
                color = "#ff3333"  # hot
            elif frac > 0.4:
                color = theme.accent
            else:
                color = theme.primary
            line.append("  ██  ", _cached_style(color))
        else:
            line.append("  ░░  ", _cached_style("#333333"))
        lines.append(line)

    pct = Text()
    pct.append(f" {volume:3d}% ", _cached_style(theme.primary_dim))
    lines.append(pct)
    return lines


def _render_crossfader(
    position: float, width: int, theme_a: ColorTheme, theme_b: ColorTheme,
) -> list[Text]:
    """Render the crossfader strip."""
    lines: list[Text] = []
    header = Text()
    hw = max(0, (width - 10) // 2)
    header.append(" " * hw)
    header.append("CROSSFADER", _cached_style("#888888", bold=True))
    lines.append(header)

    track_w = max(7, width - 4)
    pos = int(position * (track_w - 1) + 0.5)
    pos = max(0, min(track_w - 1, pos))

    track = Text()
    track.append("  ")
    for i in range(track_w):
        if i == pos:
            track.append("▓", _cached_style("#ffffff", bold=True))
        elif i < pos:
            frac = i / max(1, track_w - 1)
            color = _lerp_color(theme_a.accent, "#555555", frac)
            track.append("─", _cached_style(color))
        else:
            frac = i / max(1, track_w - 1)
            color = _lerp_color("#555555", theme_b.accent, frac)
            track.append("─", _cached_style(color))
    lines.append(track)

    labels = Text()
    labels.append("  A", _cached_style(theme_a.primary))
    labels.append(" " * max(0, track_w - 2))
    labels.append("B", _cached_style(theme_b.primary))
    lines.append(labels)

    return lines


def _render_dj_vu(
    vu_left: float, vu_right: float, height: int, theme: ColorTheme, label: str,
) -> list[Text]:
    """Render a compact stereo VU meter column for a channel."""
    # Apply sqrt scaling (matches boombox VU) so quiet audio still shows movement
    disp_l = math.sqrt(max(min(vu_left, 1.0), 0.0))
    disp_r = math.sqrt(max(min(vu_right, 1.0), 0.0))
    filled_l = int(disp_l * height)
    filled_r = int(disp_r * height)

    lines: list[Text] = []
    header = Text()
    header.append(f" {label} ", _cached_style("#888888"))
    lines.append(header)

    for i in range(height):
        row = height - 1 - i
        frac = row / max(1, height - 1)
        if frac > 0.7:
            color = "#ff3333"
        elif frac > 0.4:
            color = theme.accent
        else:
            color = theme.primary

        line = Text()
        if row < filled_l:
            line.append("█", _cached_style(color))
        else:
            line.append("░", _cached_style("#222222"))
        if row < filled_r:
            line.append("█", _cached_style(color))
        else:
            line.append("░", _cached_style("#222222"))
        lines.append(line)

    return lines


class DJMixin:
    """Mixin providing the DJ mixing console screen."""

    def _init_dj(self) -> None:
        """Initialise DJ state. Called from BoomBoxTUI.__init__."""
        self._dj_mode: bool = False
        self._dj_state = DJState()
        self._dj_mixer: DJMixer | None = None
        self._dj_viz_a: AudioVisualizer | None = None
        self._dj_viz_b: AudioVisualizer | None = None
        # Smart-fade animation state
        self._dj_fade_active: bool = False
        self._dj_fade_start: float = 0.0        # crossfader value at start
        self._dj_fade_target: float = 0.0        # crossfader value at end
        self._dj_fade_start_time: float = 0.0    # time.time() when fade began
        self._dj_fade_duration: float = 4.0       # seconds
        self._dj_fade_bass_duck: str = ""         # channel to duck bass ("a" or "b")
        self._dj_fade_orig_bass: int = 0          # original bass EQ to restore

    def _start_dj_mode(self) -> None:
        """Activate DJ mode: create mixer + per-channel visualizers, mute native audio."""
        from alfieprime_musiciser.dj_mixer import DJMixer

        self._dj_viz_a = AudioVisualizer()
        self._dj_viz_b = AudioVisualizer()
        self._dj_mixer = DJMixer(
            self._dj_state,
            self._visualizer,  # type: ignore[attr-defined]
            viz_a=self._dj_viz_a,
            viz_b=self._dj_viz_b,
        )
        self._dj_mixer.start()
        self._dj_mode = True
        # Ensure master viz is unpaused — mixer owns it now
        self._visualizer.set_paused(False)  # type: ignore[attr-defined]
        # Notify receivers to mute native audio and start feeding mixer
        if self._dj_activate_callback:  # type: ignore[attr-defined]
            self._dj_activate_callback(True, self._dj_mixer)  # type: ignore[attr-defined]
        logger.info("DJ mode activated")

    def _stop_dj_mode(self) -> None:
        """Deactivate DJ mode: stop mixer, restore native audio."""
        if self._dj_mixer is not None:
            self._dj_mixer.stop()
            self._dj_mixer = None
        self._dj_viz_a = None
        self._dj_viz_b = None
        self._dj_mode = False
        # Notify receivers to restore native audio
        if self._dj_activate_callback:  # type: ignore[attr-defined]
            self._dj_activate_callback(False, None)  # type: ignore[attr-defined]
        logger.info("DJ mode deactivated")

    def _dj_trigger_smartfade(self) -> None:
        """Start a smart crossfade to the opposite side."""
        dj = self._dj_state
        if self._dj_fade_active:
            self._cancel_smartfade()
            return

        # Determine direction: go to the other side
        if dj.crossfader < 0.5:
            target = 1.0  # fade A → B
            duck_ch = "a"  # outgoing channel gets bass ducked
        else:
            target = 0.0  # fade B → A
            duck_ch = "b"

        # Try to use BPM for duration (8 beats), fallback to 4s
        duration = 4.0
        try:
            bpm = self._visualizer.get_bpm()  # type: ignore[attr-defined]
            if bpm and bpm > 40:
                beat_sec = 60.0 / bpm
                duration = beat_sec * 8  # 8 beats
                duration = max(2.0, min(8.0, duration))  # clamp 2-8s
        except Exception:
            pass

        # Store outgoing channel's original bass for restoration
        outgoing = dj.channel_a if duck_ch == "a" else dj.channel_b
        self._dj_fade_orig_bass = outgoing.eq_bass

        self._dj_fade_start = dj.crossfader
        self._dj_fade_target = target
        self._dj_fade_start_time = time.time()
        self._dj_fade_duration = duration
        self._dj_fade_bass_duck = duck_ch
        self._dj_fade_active = True
        logger.info("Smart fade: %.2f → %.2f over %.1fs (duck %s bass)",
                     dj.crossfader, target, duration, duck_ch.upper())

    def _dj_tick_smartfade(self) -> None:
        """Advance the smart-fade animation (called each render frame)."""
        if not self._dj_fade_active:
            return

        dj = self._dj_state
        elapsed = time.time() - self._dj_fade_start_time
        progress = min(1.0, elapsed / self._dj_fade_duration)

        # Smooth ease-in-out curve
        t = progress * progress * (3.0 - 2.0 * progress)

        # Interpolate crossfader
        dj.crossfader = self._dj_fade_start + (self._dj_fade_target - self._dj_fade_start) * t

        # Bass duck on outgoing channel: ramp down from original to -12
        outgoing = dj.channel_a if self._dj_fade_bass_duck == "a" else dj.channel_b
        duck_amount = t  # 0→1 as fade progresses
        outgoing.eq_bass = int(self._dj_fade_orig_bass * (1.0 - duck_amount) + (-12) * duck_amount)

        if progress >= 1.0:
            # Fade complete — restore original bass EQ on outgoing channel
            dj.crossfader = self._dj_fade_target
            outgoing.eq_bass = self._dj_fade_orig_bass
            self._dj_fade_active = False
            logger.info("Smart fade complete")

    def _cancel_smartfade(self) -> None:
        """Cancel an in-progress smart fade, restoring outgoing channel's bass EQ."""
        if not self._dj_fade_active:
            return
        dj = self._dj_state
        outgoing = dj.channel_a if self._dj_fade_bass_duck == "a" else dj.channel_b
        outgoing.eq_bass = self._dj_fade_orig_bass
        self._dj_fade_active = False

    # ── Helpers to read per-source data from snapshots ───────────────────

    def _dj_get_mode(self) -> tuple[str, str, str, str, str]:
        """Return (mode, source_a, source_b, label_a, label_b) based on config.

        In 'mixed' mode, auto-detect which sources are actually connected
        rather than always defaulting to sendspin + airplay.
        """
        cfg = getattr(self, "_config", None)
        mode = cfg.dj_source_mode if cfg else "mixed"
        if mode == "dual_sendspin":
            return mode, "sendspin", "sendspin_b", "A · SENDSPIN", "B · SENDSPIN 2"
        elif mode == "dual_airplay":
            return mode, "airplay", "airplay_b", "A · AIRPLAY", "B · AIRPLAY 2"
        elif mode == "spotify_sendspin":
            return mode, "sendspin", "spotify", "A · SENDSPIN", "B · SPOTIFY"
        elif mode == "spotify_airplay":
            return mode, "airplay", "spotify", "A · AIRPLAY", "B · SPOTIFY"
        elif mode == "dual_spotify":
            return mode, "spotify", "spotify", "A · SPOTIFY", "B · SPOTIFY"
        # Mixed mode: auto-detect connected sources
        state = self.state  # type: ignore[attr-defined]
        _labels = {"sendspin": "SENDSPIN", "airplay": "AIRPLAY", "spotify": "SPOTIFY"}
        connected = []
        if getattr(state, "sendspin_connected", False):
            connected.append("sendspin")
        if getattr(state, "airplay_connected", False):
            connected.append("airplay")
        if getattr(state, "spotify_connected", False):
            connected.append("spotify")
        if len(connected) >= 2:
            src_a, src_b = connected[0], connected[1]
        elif len(connected) == 1:
            src_a = connected[0]
            src_b = "airplay" if src_a != "airplay" else "sendspin"
        else:
            src_a, src_b = "sendspin", "airplay"
        return mode, src_a, src_b, f"A · {_labels.get(src_a, src_a.upper())}", f"B · {_labels.get(src_b, src_b.upper())}"

    def _dj_source_data(self, source: str) -> dict:
        """Get snapshot data for a source, or live state if it's active."""
        # For "_b" sources, read from the second receiver's standalone state
        if source.endswith("_b"):
            rcv_b = getattr(self, "_dj_receiver_b", None)
            if rcv_b is not None:
                st = getattr(rcv_b, "_daemon_state", None) or getattr(rcv_b, "_state", None)
                if st is None:
                    # AirPlayReceiver doesn't have _daemon_state; check for _state via property
                    try:
                        st = rcv_b._state
                    except Exception:
                        st = None
                if st is not None:
                    return {
                        "title": st.title,
                        "artist": st.artist,
                        "album": st.album,
                        "artwork_data": st.artwork_data,
                        "theme": st.theme,
                        "is_playing": st.is_playing,
                        "progress_ms": st.get_interpolated_progress() if hasattr(st, "get_interpolated_progress") else st.progress_ms,
                        "duration_ms": st.duration_ms,
                        "server_name": getattr(st, "sendspin_server_name", "") or getattr(st, "airplay_server_name", "") or "",
                        "codec": getattr(st, "codec", "pcm"),
                        "sample_rate": getattr(st, "sample_rate", 48000),
                        "bit_depth": getattr(st, "bit_depth", 16),
                    }
            return {
                "title": "", "artist": "", "album": "", "artwork_data": b"",
                "theme": ColorTheme(), "is_playing": False,
                "progress_ms": 0, "duration_ms": 0,
                "server_name": "", "codec": "pcm", "sample_rate": 48000, "bit_depth": 16,
            }
        state: PlayerState = self.state  # type: ignore[attr-defined]
        if state.active_source == source:
            _sn = ""
            if source == "sendspin":
                _sn = state.sendspin_server_name or ""
            elif source == "airplay":
                _sn = state.airplay_server_name or ""
            elif source == "spotify":
                _sn = state.spotify_server_name or ""
            return {
                "title": state.title,
                "artist": state.artist,
                "album": state.album,
                "artwork_data": state.artwork_data,
                "theme": state.theme,
                "is_playing": state.is_playing,
                "progress_ms": state.get_interpolated_progress(),
                "duration_ms": state.duration_ms,
                "server_name": _sn,
                "codec": state.codec,
                "sample_rate": state.sample_rate,
                "bit_depth": state.bit_depth,
            }
        snap = state._source_snapshots.get(source, {})
        # Interpolate snapshot progress so the DJ tonearm moves smoothly
        snap_progress = snap.get("progress_ms", 0)
        snap_duration = snap.get("duration_ms", 0)
        snap_playing = snap.get("is_playing", False)
        snap_speed = snap.get("playback_speed", 1.0)
        snap_update = snap.get("progress_update_time", 0.0)
        if snap_playing and snap_update > 0 and snap_duration > 0:
            elapsed = time.monotonic() - snap_update
            speed = snap_speed if snap_speed > 0 else 1.0
            snap_progress = max(0, min(snap_progress + int(elapsed * 1000 * speed), snap_duration))
        return {
            "title": snap.get("title", ""),
            "artist": snap.get("artist", ""),
            "album": snap.get("album", ""),
            "artwork_data": snap.get("artwork_data", b""),
            "theme": snap.get("theme", ColorTheme()),
            "is_playing": snap_playing,
            "progress_ms": snap_progress,
            "duration_ms": snap_duration,
            "server_name": snap.get("server_name", ""),
            "codec": snap.get("codec", "pcm"),
            "sample_rate": snap.get("sample_rate", 48000),
            "bit_depth": snap.get("bit_depth", 16),
        }

    def _dj_connected_b(self) -> bool:
        """Check if the second receiver (channel B in dual modes) is connected."""
        rcv_b = getattr(self, "_dj_receiver_b", None)
        if rcv_b is None:
            return False
        st = getattr(rcv_b, "_daemon_state", None)
        if st is not None:
            return st.connected
        try:
            return rcv_b._state.connected
        except Exception:
            return False

    # ── Layout ───────────────────────────────────────────────────────────

    def _build_dj_layout(self) -> Group:
        """Build the DJ mixing console layout."""
        # Advance smart-fade animation before rendering
        self._dj_tick_smartfade()

        self._term_width, self._term_height = self._get_terminal_size()  # type: ignore[attr-defined]
        term_w = self._term_width
        term_h = self._term_height
        t = time.time()

        dj = self._dj_state
        state: PlayerState = self.state  # type: ignore[attr-defined]

        # Get per-source data — dynamic based on DJ source mode
        _dj_mode, _src_a, _src_b, _label_a, _label_b = self._dj_get_mode()
        data_a = self._dj_source_data(_src_a)
        data_b = self._dj_source_data(_src_b)
        theme_a: ColorTheme = data_a.get("theme", ColorTheme())  # type: ignore[assignment]
        theme_b: ColorTheme = data_b.get("theme", ColorTheme())  # type: ignore[assignment]
        master_theme = blend_themes(theme_a, theme_b, dj.crossfader)

        # Get visualizer data — per-channel when available
        # Master viz is fed by the DJ mixer which already applies per-channel
        # volume and crossfade — no global volume scaling needed here.
        bands, peaks, vu_left, vu_right = self._visualizer.get_spectrum()  # type: ignore[attr-defined]
        beat_count, beat_intensity = self._visualizer.get_beat()  # type: ignore[attr-defined]

        # Per-channel VU/beat from dedicated visualizers
        # The mixer feeds per-channel vizs with post-volume audio, so VU values
        # already reflect channel volume. Only scale by crossfader gain here.
        xf = dj.crossfader
        xf_gain_a = math.cos(xf * math.pi / 2)
        xf_gain_b = math.sin(xf * math.pi / 2)
        if self._dj_viz_a is not None:
            _, _, vu_a_l, vu_a_r = self._dj_viz_a.get_spectrum()
            _, beat_a = self._dj_viz_a.get_beat()
            vu_a_l *= xf_gain_a
            vu_a_r *= xf_gain_a
            beat_a *= xf_gain_a
        else:
            vu_a_l = vu_a_r = vu_left * xf_gain_a
            beat_a = beat_intensity * xf_gain_a if state.active_source == "sendspin" else 0.0

        if self._dj_viz_b is not None:
            _bands_b, _peaks_b, vu_b_l, vu_b_r = self._dj_viz_b.get_spectrum()
            _, beat_b = self._dj_viz_b.get_beat()
            vu_b_l *= xf_gain_b
            vu_b_r *= xf_gain_b
            beat_b *= xf_gain_b
        else:
            vu_b_l = vu_b_r = vu_left * xf_gain_b
            beat_b = beat_intensity * xf_gain_b if state.active_source == "airplay" else 0.0

        padded_inner = max(term_w - 4, 20)
        parts: list = []

        # ── Title banner ──
        title_text = Text()
        banner = " ♪ A L F I E P R I M E   D J ♪ "
        pad = max(0, (padded_inner - len(banner)) // 2)
        title_text.append(" " * pad)
        title_text.append(banner, _cached_style(master_theme.accent, bold=True))
        parts.append(Panel(
            title_text,
            border_style=_cached_style(master_theme.primary),
            padding=(0, 1),
        ))

        # ── Main DJ console: turntables + mixer ──
        turntable_w = max(20, (term_w - 20) // 2)
        mixer_w = max(14, term_w - turntable_w * 2 - 8)

        # Track progress for tonearm position (0.0–1.0)
        prog_a = data_a.get("progress_ms", 0) / max(1, data_a.get("duration_ms", 1))
        prog_b = data_b.get("progress_ms", 0) / max(1, data_b.get("duration_ms", 1))

        # Determine connection states per mode
        if _dj_mode == "dual_sendspin":
            conn_a = state.sendspin_connected
            conn_b = self._dj_connected_b()
            fallback_a, fallback_b = "SendSpin", "SendSpin 2"
        elif _dj_mode == "dual_airplay":
            conn_a = state.airplay_connected
            conn_b = self._dj_connected_b()
            fallback_a, fallback_b = "AirPlay", "AirPlay 2"
        elif _dj_mode == "spotify_sendspin":
            conn_a = state.sendspin_connected
            conn_b = state.spotify_connected
            fallback_a, fallback_b = "SendSpin", "Spotify"
        elif _dj_mode == "spotify_airplay":
            conn_a = state.airplay_connected
            conn_b = state.spotify_connected
            fallback_a, fallback_b = "AirPlay", "Spotify"
        elif _dj_mode == "dual_spotify":
            conn_a = state.spotify_connected
            conn_b = state.spotify_connected
            fallback_a, fallback_b = "Spotify", "Spotify"
        else:
            conn_a = state.sendspin_connected
            conn_b = state.airplay_connected
            fallback_a, fallback_b = "SendSpin", "AirPlay"

        # Turntable A
        a_playing = data_a.get("is_playing", False) and conn_a
        tt_a = _render_turntable(
            theme_a,
            data_a.get("title", "") or "No track",
            data_a.get("artist", "") or fallback_a,
            a_playing,
            beat_a,
            t, dj.active_channel == "a",
            _label_a,
            turntable_w,
            connected=conn_a,
            progress=min(1.0, prog_a),
        )

        # Turntable B
        b_playing = data_b.get("is_playing", False) and conn_b
        tt_b = _render_turntable(
            theme_b,
            data_b.get("title", "") or "No track",
            data_b.get("artist", "") or fallback_b,
            b_playing,
            beat_b,
            t, dj.active_channel == "b",
            _label_b,
            turntable_w,
            connected=conn_b,
            progress=min(1.0, prog_b),
        )

        # Center mixer: VU meters + crossfader
        mixer_lines: list[Text] = []
        mixer_lines.append(Text(""))

        # Per-channel VU
        vu_h = 6
        vu_a_lines = _render_dj_vu(vu_a_l, vu_a_r, vu_h, theme_a, "A")
        vu_b_lines = _render_dj_vu(vu_b_l, vu_b_r, vu_h, theme_b, "B")

        # Combine VU A and B side by side
        for i in range(max(len(vu_a_lines), len(vu_b_lines))):
            combined = Text()
            combined.append("  ")
            if i < len(vu_a_lines):
                combined.append_text(vu_a_lines[i])
            else:
                combined.append("   ")
            combined.append("  ")
            if i < len(vu_b_lines):
                combined.append_text(vu_b_lines[i])
            else:
                combined.append("   ")
            mixer_lines.append(combined)

        mixer_lines.append(Text(""))

        # Crossfader
        xf_lines = _render_crossfader(dj.crossfader, mixer_w, theme_a, theme_b)
        mixer_lines.extend(xf_lines)

        # Assemble turntables + mixer in a grid
        console_grid = Table.grid(padding=0, expand=True)
        console_grid.add_column(ratio=2)
        console_grid.add_column(ratio=1)
        console_grid.add_column(ratio=2)

        # Pad turntable lines to match heights
        max_tt_h = max(len(tt_a), len(tt_b), len(mixer_lines))
        while len(tt_a) < max_tt_h:
            tt_a.append(Text(""))
        while len(tt_b) < max_tt_h:
            tt_b.append(Text(""))
        while len(mixer_lines) < max_tt_h:
            mixer_lines.append(Text(""))

        console_grid.add_row(
            Group(*tt_a),
            Group(*mixer_lines),
            Group(*tt_b),
        )

        # Beat-reactive border glow on the decks panel
        deck_border = _lerp_color(
            master_theme.primary_dim, master_theme.primary,
            min(1.0, beat_intensity * 0.6),
        )
        if self._dj_fade_active:
            fade_dir = "A→B" if self._dj_fade_target > 0.5 else "B→A"
            mixer_status = f"● FADE {fade_dir}"
        elif self._dj_mixer is not None:
            mixer_status = "● MIX"
        else:
            mixer_status = "○ OFF"
        parts.append(Panel(
            console_grid,
            title=f" ◈ DECKS  {mixer_status} ◈ ",
            title_align="center",
            border_style=_cached_style(deck_border),
            padding=(0, 1),
        ))

        # ── EQ section ──
        eq_grid = Table.grid(padding=0, expand=True)
        eq_grid.add_column(ratio=1)
        eq_grid.add_column(width=4)
        eq_grid.add_column(ratio=1)

        ch_a = dj.channel_a
        ch_b = dj.channel_b
        eq_w = max(15, (term_w - 12) // 2)

        eq_a_lines = [
            _render_eq_slider("B", ch_a.eq_bass, eq_w, theme_a, dj.active_channel == "a"),
            _render_eq_slider("M", ch_a.eq_mid, eq_w, theme_a, dj.active_channel == "a"),
            _render_eq_slider("T", ch_a.eq_treble, eq_w, theme_a, dj.active_channel == "a"),
        ]
        eq_b_lines = [
            _render_eq_slider("B", ch_b.eq_bass, eq_w, theme_b, dj.active_channel == "b"),
            _render_eq_slider("M", ch_b.eq_mid, eq_w, theme_b, dj.active_channel == "b"),
            _render_eq_slider("T", ch_b.eq_treble, eq_w, theme_b, dj.active_channel == "b"),
        ]

        # Volume bars inline
        vol_a = Text()
        vol_a.append(" VOL ", _cached_style(theme_a.secondary if dj.active_channel == "a" else "#666666",
                                             bold=dj.active_channel == "a"))
        v_track = max(5, eq_w - 12)
        filled_a = int(ch_a.volume / 100 * v_track + 0.5)
        for i in range(v_track):
            if i < filled_a:
                frac = i / max(1, v_track - 1)
                color = "#ff3333" if frac > 0.8 else (theme_a.accent if frac > 0.5 else theme_a.primary)
                vol_a.append("█", _cached_style(color))
            else:
                vol_a.append("░", _cached_style("#333333"))
        vol_a.append(f" {ch_a.volume:3d}%", _cached_style(theme_a.primary_dim))

        vol_b = Text()
        vol_b.append(" VOL ", _cached_style(theme_b.secondary if dj.active_channel == "b" else "#666666",
                                             bold=dj.active_channel == "b"))
        filled_b = int(ch_b.volume / 100 * v_track + 0.5)
        for i in range(v_track):
            if i < filled_b:
                frac = i / max(1, v_track - 1)
                color = "#ff3333" if frac > 0.8 else (theme_b.accent if frac > 0.5 else theme_b.primary)
                vol_b.append("█", _cached_style(color))
            else:
                vol_b.append("░", _cached_style("#333333"))
        vol_b.append(f" {ch_b.volume:3d}%", _cached_style(theme_b.primary_dim))

        eq_a_lines.append(vol_a)
        eq_b_lines.append(vol_b)

        eq_grid.add_row(
            Group(*eq_a_lines),
            Text(""),
            Group(*eq_b_lines),
        )

        eq_border = _lerp_color(
            master_theme.primary_dim, master_theme.warm,
            min(1.0, beat_intensity * 0.4),
        )
        parts.append(Panel(
            eq_grid,
            title=" ◈ EQ & VOLUME ◈ ",
            title_align="center",
            border_style=_cached_style(eq_border),
            padding=(0, 1),
        ))

        # ── Dual-protocol status bar ──
        status = Text()
        _dim = _cached_style("#666666")
        _on = _cached_style(master_theme.accent, bold=True)
        _off = _cached_style("#555555")

        # Channel A
        _short_a = _label_a.split("·")[-1].strip() if "·" in _label_a else _label_a
        _short_b = _label_b.split("·")[-1].strip() if "·" in _label_b else _label_b
        if conn_a:
            status.append(f" A·{_short_a}", _on if dj.active_channel == "a" else _cached_style("#888888"))
            # Server name for channel A
            _sn_a = data_a.get("server_name", "")
            if _sn_a:
                status.append(f" ⚡ {_sn_a}", _cached_style(theme_a.secondary))
            codec_a = data_a.get("codec", "pcm")
            sr_a = data_a.get("sample_rate", 48000)
            bd_a = data_a.get("bit_depth", 16)
            status.append(f"  ♪ {codec_a.upper()} {sr_a // 1000}kHz {bd_a}bit", _cached_style("#888888"))
            status.append(" ●", _on if dj.active_channel == "a" else _cached_style("#888888"))
        else:
            status.append(f" A·{_short_a} ○", _off)

        status.append("  │", _dim)

        # Channel B
        if conn_b:
            status.append(f"  B·{_short_b}", _on if dj.active_channel == "b" else _cached_style("#888888"))
            _sn_b = data_b.get("server_name", "")
            if _sn_b:
                status.append(f" ⚡ {_sn_b}", _cached_style(theme_b.secondary))
            codec_b = data_b.get("codec", "pcm")
            sr_b = data_b.get("sample_rate", 48000)
            bd_b = data_b.get("bit_depth", 16)
            status.append(f"  ♪ {codec_b.upper()} {sr_b // 1000}kHz {bd_b}bit", _cached_style("#888888"))
            status.append(" ●", _on if dj.active_channel == "b" else _cached_style("#888888"))
        else:
            status.append(f"  B·{_short_b} ○", _off)

        # Diagnostic counters for debugging data flow
        if self._dj_mixer is not None:
            _fa = self._dj_mixer._feed_a_count
            _fb = self._dj_mixer._feed_b_count
            _mc = self._dj_mixer._mix_count
            _rb = self._dj_mixer._ring_b_reads
            status.append(f"  │ A:{_fa} B:{_fb} mix:{_mc} rb:{_rb}", _cached_style("#666666"))

        parts.append(Panel(
            status,
            border_style="#444444", padding=(0, 0),
        ))

        # ── Master spectrum ──
        flush_inner = max(term_w - 2, 20)
        # Calculate remaining height
        status_rows = 3  # status bar: content + 2 borders
        used_rows = 3 + max_tt_h + 4 + 6 + 2 + status_rows + 3 + 2  # title + decks + eq + spectrum borders + status + hints
        spec_height = max(4, term_h - used_rows)

        parts.append(Panel(
            Group(*render_spectrum(bands, peaks, flush_inner, spec_height, theme=master_theme)),
            title=" ≋ MASTER OUTPUT ≋ ",
            title_align="center",
            border_style=_cached_style(master_theme.border_spectrum),
            padding=(0, 0),
        ))

        # ── Key hints (with flash effects) ──
        _hs = self._hint_style  # type: ignore[attr-defined]
        focus_label = f"A·{_short_a}" if dj.active_channel == "a" else f"B·{_short_b}"
        fade_label = "[F]ade▸" if self._dj_fade_active else "[F]ade "
        hint = Text()
        hint_parts = [
            ("[P]lay ", "p", False),
            (f"[Tab]Switch({focus_label}) ", "tab", False),
            ("[←→]Xfade ", "xfade", False),
            ("[↑↓]Vol ", "vol", False),
            ("[1/2/3]EQ± ", "eq", False),
            ("[0]Flat ", "0", False),
            ("[X]Center ", "x", False),
            (fade_label, "f", self._dj_fade_active),
            ("[D]Exit DJ ", "d", False),
        ]
        total = sum(len(h) for h, _, _ in hint_parts)
        pad_l = max(0, (term_w - total) // 2)
        hint.append(" " * pad_l)
        for label, key, active in hint_parts:
            hint.append(label, _hs(key, active=active))
        parts.append(hint)

        return Group(*parts)

    # ── Key handling ─────────────────────────────────────────────────────

    def _handle_dj_key(self, k: str) -> None:
        """Handle keys when DJ mode is active."""
        dj = self._dj_state

        _flash = self._flash_hint  # type: ignore[attr-defined]

        if k == "d":
            # Exit DJ mode
            if self._dj_fade_active:
                self._cancel_smartfade()
            _flash("d")
            from_mode = "dj"
            self._stop_dj_mode()
            to_mode = self._get_current_mode_name()  # type: ignore[attr-defined]
            self._start_transition(from_mode, to_mode)  # type: ignore[attr-defined]
            return

        if k == "p":
            # In DJ mode, pause/play ALL sources so both decks stop/start.
            state = getattr(self, "state", None)
            ss_cb = getattr(self, "_sendspin_command_callback", None)
            ap_cb = getattr(self, "_airplay_dj_play_pause", None)
            sp_cb = getattr(self, "_spotify_dj_play_pause", None)
            want_pause = state.is_playing if state else True
            # SendSpin: explicit dj_pause/dj_play (avoids stale is_playing race)
            if ss_cb:
                ss_cb("dj_pause" if want_pause else "dj_play")
            # AirPlay: dedicated DJ play/pause (bypasses active_source routing)
            if ap_cb:
                ap_cb(want_pause)
            # Spotify: dedicated DJ play/pause
            if sp_cb:
                sp_cb(want_pause)
            _flash("p")
            return

        if k == "f":
            self._dj_trigger_smartfade()
            _flash("f")
            return

        if k == "\t" or k == "tab":
            dj.active_channel = "b" if dj.active_channel == "a" else "a"
            _flash("tab")
            return

        ch = dj.get_focused()

        if k == "arrow_up":
            ch.volume = min(100, ch.volume + 5)
            _flash("vol")
        elif k == "arrow_down":
            ch.volume = max(0, ch.volume - 5)
            _flash("vol")
        elif k == "arrow_left":
            if self._dj_fade_active:
                self._cancel_smartfade()
            dj.crossfader = max(0.0, dj.crossfader - 0.05)
            _flash("xfade")
        elif k == "arrow_right":
            if self._dj_fade_active:
                self._cancel_smartfade()
            dj.crossfader = min(1.0, dj.crossfader + 0.05)
            _flash("xfade")
        elif k == "1":
            ch.eq_bass = min(12, ch.eq_bass + 2)
            _flash("eq")
        elif k == "!":
            ch.eq_bass = max(-12, ch.eq_bass - 2)
            _flash("eq")
        elif k == "2":
            ch.eq_mid = min(12, ch.eq_mid + 2)
            _flash("eq")
        elif k == "@":
            ch.eq_mid = max(-12, ch.eq_mid - 2)
            _flash("eq")
        elif k == "3":
            ch.eq_treble = min(12, ch.eq_treble + 2)
            _flash("eq")
        elif k == "#":
            ch.eq_treble = max(-12, ch.eq_treble - 2)
            _flash("eq")
        elif k == "0":
            dj.reset_eq()
            _flash("0")
        elif k == "x":
            if self._dj_fade_active:
                self._cancel_smartfade()
            dj.crossfader = 0.5
            _flash("x")
        elif k == "q":
            # Allow quit from DJ mode
            self._stop_dj_mode()
            # Re-dispatch to main handler
            self._handle_key(k)  # type: ignore[attr-defined]
