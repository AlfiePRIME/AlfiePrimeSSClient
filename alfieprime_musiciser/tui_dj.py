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
        # Left channel
        if vu_left * height > row:
            line.append("█", _cached_style(color))
        else:
            line.append("░", _cached_style("#222222"))
        # Right channel
        if vu_right * height > row:
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

    # ── Helpers to read per-source data from snapshots ───────────────────

    def _dj_source_data(self, source: str) -> dict:
        """Get snapshot data for a source, or live state if it's active."""
        state: PlayerState = self.state  # type: ignore[attr-defined]
        if state.active_source == source:
            return {
                "title": state.title,
                "artist": state.artist,
                "album": state.album,
                "artwork_data": state.artwork_data,
                "theme": state.theme,
                "is_playing": state.is_playing,
                "progress_ms": state.get_interpolated_progress(),
                "duration_ms": state.duration_ms,
            }
        snap = state._source_snapshots.get(source, {})
        return {
            "title": snap.get("title", ""),
            "artist": snap.get("artist", ""),
            "album": snap.get("album", ""),
            "artwork_data": snap.get("artwork_data", b""),
            "theme": snap.get("theme", ColorTheme()),
            "is_playing": snap.get("is_playing", False),
            "progress_ms": snap.get("progress_ms", 0),
            "duration_ms": snap.get("duration_ms", 0),
        }

    # ── Layout ───────────────────────────────────────────────────────────

    def _build_dj_layout(self) -> Group:
        """Build the DJ mixing console layout."""
        self._term_width, self._term_height = self._get_terminal_size()  # type: ignore[attr-defined]
        term_w = self._term_width
        term_h = self._term_height
        t = time.time()

        dj = self._dj_state
        state: PlayerState = self.state  # type: ignore[attr-defined]

        # Get per-source data
        data_a = self._dj_source_data("sendspin")
        data_b = self._dj_source_data("airplay")
        theme_a: ColorTheme = data_a.get("theme", ColorTheme())  # type: ignore[assignment]
        theme_b: ColorTheme = data_b.get("theme", ColorTheme())  # type: ignore[assignment]
        master_theme = blend_themes(theme_a, theme_b, dj.crossfader)

        # Get visualizer data — per-channel when available
        bands, peaks, vu_left, vu_right = self._visualizer.get_spectrum()  # type: ignore[attr-defined]
        beat_count, beat_intensity = self._visualizer.get_beat()  # type: ignore[attr-defined]

        # Per-channel VU/beat from dedicated visualizers
        if self._dj_viz_a is not None:
            _, _, vu_a_l, vu_a_r = self._dj_viz_a.get_spectrum()
            _, beat_a = self._dj_viz_a.get_beat()
        else:
            vu_a_l = vu_a_r = vu_left * (1 - dj.crossfader)
            beat_a = beat_intensity if state.active_source == "sendspin" else 0.0

        if self._dj_viz_b is not None:
            _, _, vu_b_l, vu_b_r = self._dj_viz_b.get_spectrum()
            _, beat_b = self._dj_viz_b.get_beat()
        else:
            vu_b_l = vu_b_r = vu_left * dj.crossfader
            beat_b = beat_intensity if state.active_source == "airplay" else 0.0

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

        # Turntable A (SendSpin)
        a_playing = data_a.get("is_playing", False) and state.sendspin_connected
        tt_a = _render_turntable(
            theme_a,
            data_a.get("title", "") or "No track",
            data_a.get("artist", "") or "SendSpin",
            a_playing,
            beat_a,
            t, dj.active_channel == "a",
            "A · SENDSPIN",
            turntable_w,
            connected=state.sendspin_connected,
            progress=min(1.0, prog_a),
        )

        # Turntable B (AirPlay)
        b_playing = data_b.get("is_playing", False) and state.airplay_connected
        tt_b = _render_turntable(
            theme_b,
            data_b.get("title", "") or "No track",
            data_b.get("artist", "") or "AirPlay",
            b_playing,
            beat_b,
            t, dj.active_channel == "b",
            "B · AIRPLAY",
            turntable_w,
            connected=state.airplay_connected,
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
        mixer_status = "● MIX" if self._dj_mixer is not None else "○ OFF"
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

        # ── Master spectrum ──
        flush_inner = max(term_w - 2, 20)
        # Calculate remaining height
        used_rows = 3 + max_tt_h + 4 + 6 + 2 + 3 + 2  # title + decks + eq + spectrum borders + hints
        spec_height = max(4, term_h - used_rows)

        parts.append(Panel(
            Group(*render_spectrum(bands, peaks, flush_inner, spec_height, theme=master_theme)),
            title=" ≋ MASTER OUTPUT ≋ ",
            title_align="center",
            border_style=_cached_style(master_theme.border_spectrum),
            padding=(0, 0),
        ))

        # ── Key hints ──
        focus_label = "A·SendSpin" if dj.active_channel == "a" else "B·AirPlay"
        hint = Text()
        hint_parts = [
            f"[Tab]Switch({focus_label}) ",
            "[←→]Xfade ",
            "[↑↓]Vol ",
            "[1/2/3]EQ± ",
            "[0]Flat ",
            "[X]Center ",
            "[D]Exit DJ ",
        ]
        total = sum(len(h) for h in hint_parts)
        pad_l = max(0, (term_w - total) // 2)
        hint.append(" " * pad_l)
        for h in hint_parts:
            hint.append(h, _cached_style("#555555"))
        parts.append(hint)

        return Group(*parts)

    # ── Key handling ─────────────────────────────────────────────────────

    def _handle_dj_key(self, k: str) -> None:
        """Handle keys when DJ mode is active."""
        dj = self._dj_state

        if k == "d":
            # Exit DJ mode
            from_mode = "dj"
            self._stop_dj_mode()
            to_mode = self._get_current_mode_name()  # type: ignore[attr-defined]
            self._start_transition(from_mode, to_mode)  # type: ignore[attr-defined]
            return

        if k == "\t" or k == "tab":
            dj.active_channel = "b" if dj.active_channel == "a" else "a"
            return

        ch = dj.get_focused()

        if k == "arrow_up":
            ch.volume = min(100, ch.volume + 5)
        elif k == "arrow_down":
            ch.volume = max(0, ch.volume - 5)
        elif k == "arrow_left":
            dj.crossfader = max(0.0, dj.crossfader - 0.05)
        elif k == "arrow_right":
            dj.crossfader = min(1.0, dj.crossfader + 0.05)
        elif k == "1":
            ch.eq_bass = min(12, ch.eq_bass + 2)
        elif k == "!":
            ch.eq_bass = max(-12, ch.eq_bass - 2)
        elif k == "2":
            ch.eq_mid = min(12, ch.eq_mid + 2)
        elif k == "@":
            ch.eq_mid = max(-12, ch.eq_mid - 2)
        elif k == "3":
            ch.eq_treble = min(12, ch.eq_treble + 2)
        elif k == "#":
            ch.eq_treble = max(-12, ch.eq_treble - 2)
        elif k == "0":
            dj.reset_eq()
        elif k == "x":
            dj.crossfader = 0.5
        elif k == "q":
            # Allow quit from DJ mode
            self._stop_dj_mode()
            # Re-dispatch to main handler
            self._handle_key(k)  # type: ignore[attr-defined]
