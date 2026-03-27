from __future__ import annotations

import asyncio
import contextlib
import io as _io
import math
import os
import random
import re
import shutil
import signal
import sys
import time

from collections.abc import Callable

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty

from rich.console import Console, Group
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from alfieprime_musiciser.colors import ColorTheme, _hex_to_rgb, _hsv_to_rgb, _rgb_to_hex
from alfieprime_musiciser.config import Config
from alfieprime_musiciser.state import PlayerState
from alfieprime_musiciser.visualizer import AudioVisualizer
from alfieprime_musiciser.tui_settings import SettingsMixin
from alfieprime_musiciser.tui_animations import AnimationsMixin, _STANDBY_PHRASES, STANDBY_TIMEOUT
from alfieprime_musiciser.renderer import (
    render_title_banner,
    render_transport_controls,
    render_now_playing,
    render_spectrum,
    render_vu_meter,
    render_volume_gauge,
    render_party_lights,
    render_stereo_lights,
    render_party_scene,
    render_server_info,
    render_codec_info,
    render_stats_info,
    render_braille_art,
    render_art_scene,
    _process as _psutil_process,
)


# Standard ANSI 16 colours (0-15) mapped to hex for the GUI renderer.
_RICH_STANDARD_COLORS: dict[int, str] = {
    0: "#000000", 1: "#aa0000", 2: "#00aa00", 3: "#aa5500",
    4: "#0000aa", 5: "#aa00aa", 6: "#00aaaa", 7: "#aaaaaa",
    8: "#555555", 9: "#ff5555", 10: "#55ff55", 11: "#ffff55",
    12: "#5555ff", 13: "#ff55ff", 14: "#55ffff", 15: "#ffffff",
}


def _safe_hex(r: int | float, g: int | float, b: int | float) -> str:
    """Format RGB to hex string, clamping to 0-255."""
    return f"#{max(0,min(255,int(r))):02x}{max(0,min(255,int(g))):02x}{max(0,min(255,int(b))):02x}"


def _rich_256_color(n: int) -> str:
    """Convert an 8-bit (256) colour number to a hex string."""
    if n < 16:
        return _RICH_STANDARD_COLORS.get(n, "#aaaaaa")
    if n < 232:
        # 6×6×6 colour cube (indices 16-231)
        n -= 16
        b = (n % 6) * 51
        n //= 6
        g = (n % 6) * 51
        r = (n // 6) * 51
        return f"#{r:02x}{g:02x}{b:02x}"
    # Greyscale ramp (indices 232-255)
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


class BoomBoxTUI(SettingsMixin, AnimationsMixin):
    """The party-themed boom box terminal UI."""

    def __init__(self, visualizer: AudioVisualizer, gui: bool = False, config: Config | None = None) -> None:
        self._visualizer = visualizer
        self.state = PlayerState()
        self._running = False
        self._gui_mode = gui
        self._config = config
        # Restore cached theme and artwork from last session
        self._restore_cached_state(config)
        self._gui_window = None  # TerminalEmulator instance when in GUI mode
        self._command_callback: Callable[[str], None] | None = None
        # Track button positions for mouse clicks: {name: (col_start, col_end)}
        self._button_regions: dict[str, tuple[int, int]] = {}
        # Row (0-based from top of screen) where transport controls are rendered
        self._controls_row: int = 0
        # Cached terminal dimensions, updated each frame
        self._term_width: int = 120
        self._term_height: int = 50
        # Standby screensaver
        self._last_playing_time: float = time.monotonic()
        self._standby_active: bool = False
        self._standby_phrase_idx: int = 0
        self._standby_phrase_time: float = 0.0
        # Full-screen album art mode (restore from config)
        self._art_mode: bool = config.art_mode if config else False
        self._art_calm: bool = config.art_calm if config else False
        self._art_particles: list[dict] = []
        # Settings menu
        self._settings_open: bool = False
        self._settings_cursor: int = 0
        self._settings_items: list[str] = [
            "auto_play", "auto_volume", "fps_limit", "brightness",
            "show_artwork", "use_art_colors", "static_color",
        ]
        self._settings_sub: str = ""  # "" = main, "advanced", "color_picker"
        self._advanced_cursor: int = 0
        self._advanced_items: list[str] = ["client_name", "client_id", "reset_config"]
        self._advanced_editing: str = ""  # which field is being text-edited
        self._advanced_edit_buf: str = ""  # text input buffer
        self._advanced_confirm_reset: bool = False  # reset confirmation dialog
        # Color picker state
        self._color_cursor: int = 0
        self._color_hex_editing: bool = False
        self._color_hex_buf: str = ""
        # Easter egg: 33% chance of menu dancers
        self._settings_dancers: bool = False
        self._settings_dancer_tick: float = 0.0
        # Hint flash state: maps key label to flash start time
        self._hint_flash: dict[str, float] = {}
        self._hint_flash_duration: float = 0.4
        # Menu fade transition
        self._menu_fade_start: float = 0.0
        self._menu_fade_duration: float = 0.3
        self._menu_fading_in: bool = False
        self._menu_fading_out: bool = False
        self._menu_fade_callback: Callable[[], None] | None = None
        # Mode transition animation
        self._transition_active: bool = False
        self._transition_start: float = 0.0
        self._transition_duration: float = 0.6
        self._transition_from: str = ""  # "main", "art", "art_calm"
        self._transition_to: str = ""
        # Cached Rich Console instances (reused across frames)
        self._render_console: Console | None = None
        self._render_console_size: tuple[int, int] = (0, 0)
        self._gui_console: Console | None = None
        self._gui_console_size: tuple[int, int] = (0, 0)
        self._crt_console: Console | None = None
        self._crt_console_size: tuple[int, int] = (0, 0)
        # Connecting timeout hint
        self._connect_wait_start: float = 0.0

    def _restore_cached_state(self, config: Config | None) -> None:
        """Restore theme and artwork from last session for the intro animation."""
        if not config:
            return
        # Restore theme colours from config cache
        ct = config.cached_theme
        if ct and isinstance(ct, dict) and "primary" in ct:
            try:
                self.state.theme = ColorTheme(
                    primary=ct.get("primary", "#ff00ff"),
                    secondary=ct.get("secondary", "#00ccff"),
                    accent=ct.get("accent", "#00ff88"),
                    warm=ct.get("warm", "#ffaa00"),
                    highlight=ct.get("highlight", "#ff6644"),
                    cool=ct.get("cool", "#8855ff"),
                    primary_dim=ct.get("primary_dim", "#666666"),
                    bg_subtle=ct.get("bg_subtle", "#1a1a1a"),
                    spectrum_colors=ct.get("spectrum_colors", []),
                    border_title=ct.get("border_title", "bright_magenta"),
                    border_now_playing=ct.get("border_now_playing", "bright_cyan"),
                    border_spectrum=ct.get("border_spectrum", "bright_green"),
                    border_vu=ct.get("border_vu", "bright_yellow"),
                    border_party=ct.get("border_party", "bright_magenta"),
                    border_dance=ct.get("border_dance", "bright_yellow"),
                )
            except (TypeError, ValueError):
                pass
        # Restore cached artwork image from temp file (used by MPRIS too)
        if config.art_mode:
            from alfieprime_musiciser.mpris import _get_art_cache_path
            art_path = _get_art_cache_path()
            try:
                if art_path.exists():
                    data = art_path.read_bytes()
                    if data:
                        self.state.artwork_data = data
            except OSError:
                pass

    def set_command_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for transport commands (play_pause, next, previous, shuffle, repeat)."""
        self._command_callback = callback

    def _fire_command(self, command: str) -> None:
        if self._command_callback:
            self._command_callback(command)

    def _get_terminal_size(self) -> tuple[int, int]:
        """Query live terminal/window dimensions."""
        if self._gui_window is not None:
            return self._gui_window.get_size()
        try:
            size = shutil.get_terminal_size((120, 50))
            return size.columns, size.lines
        except Exception:
            return 120, 50

    def _update_terminal_title(self) -> None:
        """Set the terminal tab title via OSC escape sequence."""
        if self._gui_mode:
            return
        if self.state.is_playing and self.state.artist and self.state.title:
            title = f"\u266a {self.state.artist} - {self.state.title} | AlfiePRIME"
        elif self.state.connected:
            title = "\u23f8 AlfiePRIME Musiciser"
        else:
            title = "AlfiePRIME Musiciser"
        sys.stdout.write(f"\x1b]0;{title}\x07")

    def _get_current_mode_name(self) -> str:
        """Return the name of the current view mode."""
        if self._art_mode and self.state.artwork_data:
            return "art_calm" if self._art_calm else "art"
        return "main"

    def _start_transition(self, from_mode: str, to_mode: str) -> None:
        """Begin a mode transition animation."""
        self._transition_active = True
        self._transition_start = time.monotonic()
        self._transition_from = from_mode
        self._transition_to = to_mode

    def _build_layout(self) -> Group:
        if self._settings_open:
            return self._build_settings_layout()
        if self._transition_active:
            return self._build_transition_layout()
        if self._art_mode and self.state.artwork_data:
            return self._build_art_layout()
        return self._build_main_layout()

    def _get_effective_theme(self) -> ColorTheme:
        """Return theme with config overrides applied (static color, art colors off)."""
        from alfieprime_musiciser.colors import _generate_monochrome_theme
        cfg = self._config
        if cfg and not cfg.use_art_colors:
            if cfg.static_color:
                return _generate_monochrome_theme(cfg.static_color)
            return ColorTheme()  # default rainbow
        return self.state.theme

    def _save_ui_state(self) -> None:
        """Persist UI state (art mode, calm mode) to config file."""
        if self._config is None:
            return
        changed = False
        if self._config.art_mode != self._art_mode:
            self._config.art_mode = self._art_mode
            changed = True
        if self._config.art_calm != self._art_calm:
            self._config.art_calm = self._art_calm
            changed = True
        if changed:
            self._config.save()

    def _hint_style(self, key: str, active: bool = False) -> Style:
        """Return a style for a hint label. Green if active, flash accent on recent press, else dim."""
        if active:
            return Style(color="#66aa66")
        flash_start = self._hint_flash.get(key, 0.0)
        elapsed = time.time() - flash_start
        if elapsed < self._hint_flash_duration:
            th = self.state.theme
            frac = elapsed / self._hint_flash_duration
            r1, g1, b1 = _hex_to_rgb(th.accent)
            r2, g2, b2 = 0x44, 0x44, 0x44
            r = int(r1 + (r2 - r1) * frac)
            g = int(g1 + (g2 - g1) * frac)
            b = int(b1 + (b2 - b1) * frac)
            return Style(color=_safe_hex(r, g, b), bold=frac < 0.3)
        return Style(color="#444444")

    def _flash_hint(self, key: str) -> None:
        """Trigger a flash animation on a hint label."""
        self._hint_flash[key] = time.time()

    def _build_art_layout(self) -> Group:
        """Full-screen album art mode with party scene or calm view."""
        self._term_width, self._term_height = self._get_terminal_size()
        term_w = self._term_width
        term_h = self._term_height
        padded_inner = max(term_w - 4, 20)
        flush_inner = max(term_w - 2, 20)
        th = self._get_effective_theme()

        parts: list = []

        # Reserve rows: now_playing(~5) + volume(3) + hint(1) + panel borders(~6)
        reserved_rows = 5 + 3 + 1 + 6

        if self._art_calm:
            # ── Calm mode: full-screen HQ braille art (no effects) ──
            parts.append(Panel(
                render_title_banner(padded_inner, theme=th),
                border_style=th.border_title, padding=(0, 1),
            ))
            reserved_rows += 3  # title banner
            max_h = max(8, term_h - reserved_rows)
            max_w = max(20, flush_inner)

            # Build info panel content from available metadata
            info_lines: list[Text] = []
            s = self.state
            label_style = Style(color=th.accent, bold=True)
            value_style = Style(color=th.secondary)
            dim_style = Style(color="#555555", italic=True)

            if s.artist:
                info_lines.append(Text.assemble(("Artist", label_style)))
                info_lines.append(Text(s.artist, style=value_style))
                info_lines.append(Text(""))
            if s.album_artist and s.album_artist != s.artist:
                info_lines.append(Text.assemble(("Album Artist", label_style)))
                info_lines.append(Text(s.album_artist, style=value_style))
                info_lines.append(Text(""))
            if s.album:
                info_lines.append(Text.assemble(("Album", label_style)))
                info_lines.append(Text(s.album, style=value_style))
                info_lines.append(Text(""))
            if s.year:
                info_lines.append(Text.assemble(("Year", label_style)))
                info_lines.append(Text(str(s.year), style=value_style))
                info_lines.append(Text(""))
            if s.track_number:
                info_lines.append(Text.assemble(("Track", label_style)))
                info_lines.append(Text(f"#{s.track_number}", style=value_style))
                info_lines.append(Text(""))
            if s.codec and s.codec != "pcm":
                info_lines.append(Text.assemble(("Codec", label_style)))
                info_lines.append(Text(
                    f"{s.codec.upper()}  {s.sample_rate // 1000}kHz / {s.bit_depth}bit",
                    style=value_style,
                ))
                info_lines.append(Text(""))

            if not info_lines:
                info_lines.append(Text("No info available", style=dim_style))

            # Decide layout: side-by-side if terminal wide enough for art + info panel
            info_panel_w = 30  # width of the info box (including border)
            has_info = bool(info_lines)
            side_by_side = has_info and max_w >= 60 + info_panel_w

            if side_by_side:
                # Art takes remaining space left of info panel
                avail_art_w = max_w - info_panel_w - 2
                art_h = min(max_h, avail_art_w // 2)
                art_w = art_h * 2
            else:
                art_h = min(max_h, max_w // 2)
                art_w = art_h * 2

            art_lines = render_braille_art(
                self.state.artwork_data, art_w, art_h, theme=th, hq=True,
            )

            art_content: Panel | Text
            if art_lines:
                art_content = Panel(
                    Group(*art_lines),
                    title=" \u2592 ALBUM ART \u2592 ",
                    title_align="center", border_style=th.border_now_playing, padding=(0, 0),
                )
            else:
                art_content = Panel(
                    Text("No artwork available", style=Style(color="#555555", italic=True)),
                    title=" \u2592 ALBUM ART \u2592 ",
                    title_align="center", border_style=th.border_now_playing, padding=(0, 0),
                )

            if side_by_side:
                info_content = Panel(
                    Group(*info_lines),
                    title=" \u266b INFO \u266b ",
                    title_align="center",
                    border_style=th.border_now_playing,
                    padding=(1, 2),
                    width=info_panel_w,
                )
                layout_table = Table.grid(padding=(0, 1))
                layout_table.add_column(ratio=1)
                layout_table.add_column(width=info_panel_w)
                layout_table.add_row(art_content, info_content)
                parts.append(layout_table)
            else:
                parts.append(art_content)
        else:
            # ── Party mode: centered art square with dancers/confetti/fireworks ──
            bands, peaks, vu_left, vu_right = self._visualizer.get_spectrum()
            beat_count, beat_intensity = self._visualizer.get_beat()
            bpm = self._visualizer.get_bpm()
            scene_h = max(8, term_h - reserved_rows)
            scene_w = max(20, flush_inner)
            scene_lines = render_art_scene(
                self.state.artwork_data, scene_w, scene_h,
                vu_left, vu_right, beat_count, beat_intensity, bpm,
                self._art_particles, theme=th,
            )
            if scene_lines:
                parts.append(Panel(
                    Group(*scene_lines),
                    border_style=th.border_now_playing, padding=(0, 0),
                ))
            else:
                placeholder = Text("No artwork available", style=Style(color="#555555", italic=True))
                parts.append(Panel(
                    placeholder,
                    border_style=th.border_now_playing, padding=(0, 0),
                ))

        # ── Now playing + progress bar ──
        np_lines = render_now_playing(
            self.state.title or "Waiting for music...",
            self.state.artist, self.state.album,
            self.state.get_interpolated_progress(), self.state.duration_ms, padded_inner,
            theme=th,
        )
        controls_text, buttons = render_transport_controls(
            self.state.is_playing, self.state.shuffle, self.state.repeat_mode,
            self.state.supported_commands, theme=th,
        )
        content_col_offset = 3
        self._button_regions = {
            name: (c0 + content_col_offset, c1 + content_col_offset)
            for name, (c0, c1) in buttons.items()
        }
        parts.append(Panel(
            Group(*np_lines, Text(""), controls_text),
            border_style=th.border_now_playing, padding=(0, 1),
        ))

        # ── Volume gauge ──
        vol_lines = render_volume_gauge(
            self.state.volume, self.state.muted, padded_inner, height=2, theme=th,
        )
        parts.append(Panel(
            Group(*vol_lines),
            title=" \u266b VOLUME \u266b ",
            title_align="center", border_style=th.warm, padding=(0, 0),
        ))

        # ── Key hint at bottom (centered) ──
        hint_parts = [
            ("[A]rt ", "a", self._art_mode),
            ("[C]alm ", "c", self._art_calm),
            ("[P]lay ", "p", False),
            ("[N]ext ", "n", False),
            ("[B]ack ", "b", False),
            ("[S]huf ", "s", self.state.shuffle),
            ("[R]epeat ", "r", self.state.repeat_mode != "off"),
            ("[↑↓]Vol ", "vol", False),
            ("[/]Settings ", "/", False),
            ("[Q]uit", "q", False),
        ]
        hint_text_len = sum(len(label) for label, _, _ in hint_parts)
        pad_l = max(0, (term_w - hint_text_len) // 2)
        hint = Text()
        hint.append(" " * pad_l)
        for label, key, active in hint_parts:
            hint.append(label, self._hint_style(key, active=active))
        parts.append(hint)

        return Group(*parts)

    def _build_main_layout(self) -> Group:
        # Query real terminal size each frame
        self._term_width, self._term_height = self._get_terminal_size()
        term_w = self._term_width
        # Content width inside panels: border takes 2 chars, padding varies
        padded_inner = max(term_w - 4, 20)    # padding=(0,1) → 2 border + 2 padding
        flush_inner = max(term_w - 2, 20)     # padding=(0,0) → 2 border only
        bands, peaks, vu_left, vu_right = self._visualizer.get_spectrum()
        th = self._get_effective_theme()

        parts: list = []
        row = 1  # start after boom box frame top line

        # Title banner
        parts.append(Panel(
            render_title_banner(padded_inner, theme=th),
            border_style=th.border_title, padding=(0, 1),
        ))
        row += 3  # border top + content + border bottom

        # Now playing cassette deck
        np_lines = render_now_playing(
            self.state.title or "Waiting for music...",
            self.state.artist, self.state.album,
            self.state.get_interpolated_progress(), self.state.duration_ms, padded_inner,
            theme=th,
        )
        t = time.time()
        reel_frames = ["\u25d0", "\u25d3", "\u25d1", "\u25d2"]
        reel_l = reel_frames[int(t * 4) % 4] if self.state.is_playing else "\u25ef"
        reel_r = reel_frames[int(t * 4 + 2) % 4] if self.state.is_playing else "\u25ef"

        controls_text, buttons = render_transport_controls(
            self.state.is_playing, self.state.shuffle, self.state.repeat_mode,
            self.state.supported_commands, theme=th,
        )
        # Panel border(1) + padding(0,1) means content starts at col 3 (border+space+padding)
        content_col_offset = 3
        self._button_regions = {
            name: (c0 + content_col_offset, c1 + content_col_offset)
            for name, (c0, c1) in buttons.items()
        }
        # Controls row: panel border(1) + np_lines + blank line
        np_panel_content_lines = len(np_lines) + 1 + 1  # np_lines + blank + controls
        self._controls_row = row + 1 + len(np_lines) + 1  # +1 for panel top border

        np_content = Group(*np_lines, Text(""), controls_text)

        # Braille album art panel alongside now playing
        art_lines: list[Text] = []
        cfg = self._config
        show_art = cfg.show_artwork if cfg else True
        if self.state.artwork_data and show_art:
            art_w = 20
            art_h = 8  # gives 16x32 effective pixel grid
            art_lines = render_braille_art(self.state.artwork_data, art_w, art_h, theme=th)

        if art_lines:
            np_grid = Table.grid(padding=0, expand=True)
            np_grid.add_column(width=22)  # art column: 20 chars + 2 border
            np_grid.add_column(ratio=1)
            art_panel = Panel(Group(*art_lines), border_style=th.border_now_playing, padding=(0, 0))
            np_grid.add_row(art_panel, np_content)
            np_inner = np_grid
        else:
            np_inner = np_content

        parts.append(Panel(
            np_inner,
            title=f" {reel_l} NOW PLAYING {reel_r} ",
            title_align="center", border_style=th.border_now_playing, padding=(0, 1),
        ))
        row += np_panel_content_lines + 2  # +2 for panel borders

        # ── Calculate available rows for variable-height sections ──
        # Fixed-height rows used by other panels:
        #   frame_top(1) + title(3) + now_playing(np_panel_content_lines+2)
        #   + VU(4) + party_lights(4) + status(2 or 3) + frame_bot(1)
        #   + spectrum borders(2) + dance_floor borders(2)
        np_rows = np_panel_content_lines + 2
        has_stats_row = _psutil_process is not None or bool(self.state.session_stats)
        status_rows = 3 if has_stats_row else 2
        fixed_rows = 1 + 3 + np_rows + 4 + 4 + status_rows + 1 + 2 + 2
        available = max(self._term_height - fixed_rows, 8)
        # Dance floor gets a fixed size; spectrum expands to fill all remaining space
        dance_height = max(11, min(available // 2, 14))
        spec_height = max(4, available - dance_height)

        # Spectrum analyzer
        parts.append(Panel(
            Group(*render_spectrum(bands, peaks, flush_inner, spec_height, theme=th)),
            title=" \u224b SPECTRUM ANALYZER \u224b ",
            title_align="center", border_style=th.border_spectrum, padding=(0, 0),
        ))

        # VU meters + Volume gauge side by side
        vu_half = flush_inner // 2
        vol_half = flush_inner - vu_half

        vu_vol_table = Table.grid(padding=0, expand=True)
        vu_vol_table.add_column(ratio=1)
        vu_vol_table.add_column(ratio=1)

        # Left: VU meters (stacked L/R)
        vu_inner = Table.grid(padding=0, expand=True)
        vu_inner.add_column(ratio=1)
        vu_inner.add_row(render_vu_meter(vu_left, vu_half, "L", th.accent, theme=th))
        vu_inner.add_row(render_vu_meter(vu_right, vu_half, "R", th.warm, theme=th))

        # Right: Volume gauge
        vol_lines = render_volume_gauge(
            self.state.volume, self.state.muted, vol_half, height=2, theme=th,
        )
        vol_inner = Table.grid(padding=0, expand=True)
        vol_inner.add_column(ratio=1)
        for vl in vol_lines:
            vol_inner.add_row(vl)

        vu_vol_table.add_row(
            Panel(vu_inner, title=" \u25c8 VU \u25c8 ", title_align="center",
                  border_style=th.border_vu, padding=(0, 0)),
            Panel(vol_inner, title=" \u266b VOLUME \u266b ", title_align="center",
                  border_style=th.warm, padding=(0, 0)),
        )
        parts.append(vu_vol_table)

        # Party lights
        parts.append(Panel(
            Group(
                render_party_lights(flush_inner, vu_left, vu_right),
                render_stereo_lights(flush_inner, vu_left, vu_right),
            ),
            title=" \u2605 PARTY LIGHTS \u2605 ",
            title_align="center", border_style=th.border_party, padding=(0, 0),
        ))

        # Party scene animation
        beat_count, beat_intensity = self._visualizer.get_beat()
        current_bpm = self._visualizer.get_bpm()
        party_lines = render_party_scene(
            flush_inner, vu_left, vu_right, beat_count, beat_intensity,
            theme=th, height=dance_height, bpm=current_bpm,
        )
        parts.append(Panel(
            Group(*party_lines),
            title=" \u266b DANCE FLOOR \u266b ",
            title_align="center", border_style=th.border_dance, padding=(0, 0),
        ))

        # Status bar — connection info + codec, optional system stats + session stats
        status_table = Table.grid(padding=0, expand=True)
        status_table.add_column(ratio=1)
        status_table.add_column(ratio=1)
        status_table.add_row(
            render_server_info(self.state.server_name, self.state.group_name, self.state.connected, theme=th),
            render_codec_info(self.state.codec, self.state.sample_rate, self.state.bit_depth, theme=th),
        )
        # Only show system stats row if psutil is available, or session stats exist
        if _psutil_process is not None or self.state.session_stats:
            stats_row = render_stats_info(theme=th) if _psutil_process is not None else Text()
            session_text = Text()
            if self.state.session_stats:
                session_text.append(" \U0001f3a7 ", Style(color="#666666"))
                session_text.append(self.state.session_stats, Style(color=th.secondary))
            status_table.add_row(stats_row, session_text)
        parts.append(Panel(status_table, border_style="#444444", padding=(0, 0)))

        # Boom box frame accents
        frame_inner = max(term_w - 2, 0)
        speaker = "\u2550\u25cf\u2550\u25cf\u2550\u25cf\u2550"
        mid_w = max(frame_inner - len(speaker) * 2, 0)
        frame_top = Text()
        frame_top.append("\u2554", Style(color="#888888"))
        frame_top.append(speaker, Style(color=th.primary_dim))
        frame_top.append("\u2550" * mid_w, Style(color="#888888"))
        frame_top.append(speaker, Style(color=th.primary_dim))
        frame_top.append("\u2557", Style(color="#888888"))

        frame_bot = Text()
        frame_bot.append("\u255a", Style(color="#888888"))
        frame_bot.append(speaker, Style(color=th.primary_dim))
        frame_bot.append("\u2550" * mid_w, Style(color="#888888"))
        frame_bot.append(speaker, Style(color=th.primary_dim))
        frame_bot.append("\u255d", Style(color="#888888"))

        return Group(frame_top, *parts, frame_bot)

    def _handle_key(self, key: str) -> None:
        """Handle a single keypress (including special keys like 'arrow_up')."""
        k = key.lower()

        # ── Settings menu controls ──
        if self._settings_open:
            # 'c' exits settings from any submenu (unless editing text)
            if k == "c" and not self._advanced_editing and not self._color_hex_editing and not self._advanced_confirm_reset:
                self._settings_sub = ""
                self._start_menu_fade_out(lambda: setattr(self, '_settings_open', False))
                return
            if self._settings_sub == "advanced":
                self._handle_advanced_key(k, key)
            elif self._settings_sub == "color_picker":
                self._handle_color_picker_key(k, key)
            else:
                self._handle_settings_main_key(k)
            return

        if k == "/":
            self._settings_open = True
            self._settings_sub = ""
            self._settings_cursor = 0
            self._settings_dancers = random.random() < 0.33
            self._settings_dancer_tick = time.time()
            self._start_menu_fade_in()
            return

        if k == "p":
            self._fire_command("play_pause")
            self._flash_hint("p")
        elif k == "n":
            self._fire_command("next")
            self._flash_hint("n")
        elif k == "b":
            self._fire_command("previous")
            self._flash_hint("b")
        elif k == "s":
            self._fire_command("shuffle")
            self._flash_hint("s")
        elif k == "r":
            self._fire_command("repeat")
            self._flash_hint("r")
        elif k == "a":
            from_mode = self._get_current_mode_name()
            self._art_mode = not self._art_mode
            self._art_particles.clear()
            self._save_ui_state()
            to_mode = self._get_current_mode_name()
            if from_mode != to_mode:
                self._start_transition(from_mode, to_mode)
        elif k == "c" and self._art_mode:
            from_mode = self._get_current_mode_name()
            self._art_calm = not self._art_calm
            self._art_particles.clear()
            self._save_ui_state()
            to_mode = self._get_current_mode_name()
            if from_mode != to_mode:
                self._start_transition(from_mode, to_mode)
            self._flash_hint("c")
        elif k == "arrow_up":
            self._fire_command("volume_up")
            self._flash_hint("vol")
        elif k == "arrow_down":
            self._fire_command("volume_down")
            self._flash_hint("vol")

    def _handle_mouse_click(self, col: int, row: int) -> None:
        """Handle a mouse click at terminal coordinates (1-based)."""
        if row != self._controls_row + 1:  # +1 because terminal rows are 1-based
            return
        for name, (c0, c1) in self._button_regions.items():
            if c0 <= col - 1 < c1:  # col is 1-based
                self._fire_command(name)
                return

    def _parse_input(self, data: bytes) -> None:
        """Parse raw terminal input bytes for keys and mouse events."""
        i = 0
        while i < len(data):
            if data[i:i + 3] == b"\x1b[<":
                # SGR mouse event: \x1b[<button;col;rowM or m
                end = -1
                for j in range(i + 3, min(i + 32, len(data))):
                    if data[j:j + 1] in (b"M", b"m"):
                        end = j
                        break
                if end == -1:
                    i += 1
                    continue
                params = data[i + 3:end].decode("ascii", errors="ignore")
                press = data[end:end + 1] == b"M"
                i = end + 1
                parts = params.split(";")
                if len(parts) == 3 and press:
                    try:
                        btn, col, row = int(parts[0]), int(parts[1]), int(parts[2])
                        if btn == 0:  # left click
                            self._handle_mouse_click(col, row)
                    except ValueError:
                        pass
            elif data[i:i + 3] == b"\x1b[A":
                self._handle_key("arrow_up")
                i += 3
            elif data[i:i + 3] == b"\x1b[B":
                self._handle_key("arrow_down")
                i += 3
            elif data[i:i + 3] == b"\x1b[C":
                self._handle_key("arrow_right")
                i += 3
            elif data[i:i + 3] == b"\x1b[D":
                self._handle_key("arrow_left")
                i += 3
            elif data[i:i + 1] == b"\x1b":
                # Skip other escape sequences
                i += 1
                while i < len(data) and data[i:i + 1] not in (b"", b"~") and not data[i:i + 1].isalpha():
                    i += 1
                i += 1  # skip the final char
            elif data[i:i + 1] == b"\x03":
                # Ctrl+C
                self.stop()
                os.kill(os.getpid(), signal.SIGINT)
                i += 1
            else:
                ch = data[i:i + 1]
                if (ch == b"q" or ch == b"Q") and not self._settings_open:
                    self.stop()
                    os.kill(os.getpid(), signal.SIGINT)
                else:
                    self._handle_key(ch.decode("ascii", errors="ignore"))
                i += 1

    async def _input_loop(self) -> None:
        """Read keyboard and mouse input in a background task."""
        if IS_WINDOWS:
            await self._input_loop_windows()
        else:
            await self._input_loop_unix()

    async def _input_loop_windows(self) -> None:
        """Windows keyboard input using msvcrt."""
        _ARROW_MAP = {
            b"H": "arrow_up",
            b"P": "arrow_down",
            b"M": "arrow_right",
            b"K": "arrow_left",
        }
        loop = asyncio.get_event_loop()
        while self._running:
            has_key = await loop.run_in_executor(None, msvcrt.kbhit)
            if has_key:
                data = await loop.run_in_executor(None, msvcrt.getch)
                if data in (b"\xe0", b"\x00"):
                    # Special key prefix — read the second byte for the actual key
                    data2 = await loop.run_in_executor(None, msvcrt.getch)
                    arrow = _ARROW_MAP.get(data2)
                    if arrow:
                        self._handle_key(arrow)
                elif data:
                    self._parse_input(data)
            else:
                await asyncio.sleep(0.05)

    async def _input_loop_unix(self) -> None:
        """Unix keyboard input using termios/tty."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        loop = asyncio.get_event_loop()
        try:
            tty.setcbreak(fd)
            # Enable SGR mouse tracking
            sys.stdout.write("\x1b[?1000h\x1b[?1006h")
            sys.stdout.flush()
            while self._running:
                ready = await loop.run_in_executor(
                    None, lambda: select.select([fd], [], [], 0.05)[0],
                )
                if ready:
                    data = os.read(fd, 64)
                    if data:
                        self._parse_input(data)
        finally:
            # Disable mouse tracking and restore terminal
            sys.stdout.write("\x1b[?1006l\x1b[?1000l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _render_frame(self) -> str:
        """Render the full UI into an ANSI string (terminal mode)."""
        term_w, term_h = self._get_terminal_size()
        self._term_width = term_w
        self._term_height = term_h

        buf = _io.StringIO()
        # Reuse Console object when terminal size hasn't changed
        if self._render_console is None or self._render_console_size != (term_w, term_h):
            self._render_console = Console(
                file=buf, width=term_w, height=term_h,
                force_terminal=True, color_system="truecolor",
                no_color=False,
            )
            self._render_console_size = (term_w, term_h)
        else:
            self._render_console._file = buf  # type: ignore[attr-defined]

        layout = self._build_layout()
        self._render_console.print(layout)

        rendered = buf.getvalue()

        # Apply brightness multiplier to all RGB colours in ANSI escapes
        br = (self._config.brightness if self._config else 100) / 100.0
        if br != 1.0:
            def _scale(m: re.Match) -> str:
                prefix = m.group(1)  # "38;2;" or "48;2;"
                r = min(255, int(int(m.group(2)) * br))
                g = min(255, int(int(m.group(3)) * br))
                b = min(255, int(int(m.group(4)) * br))
                return f"{prefix}{r};{g};{b}"
            rendered = re.sub(r"([34]8;2;)(\d+);(\d+);(\d+)", _scale, rendered)

        # Trim/pad lines to exactly term_h — use rsplit to avoid full copy
        lines = rendered.split("\n")
        # Strip trailing empty lines
        while lines and lines[-1] == "":
            lines.pop()
        n = len(lines)
        if n > term_h:
            lines = lines[:term_h]
        elif n < term_h:
            lines.extend([""] * (term_h - n))

        return "\n".join(lines)

    def _render_frame_gui(self) -> list[tuple[str, str | None, str | None, bool]]:
        """Render the UI directly to (text, fg, bg, bold) segments for the GUI.

        Skips the ANSI encode/decode round-trip by walking Rich Segment objects
        directly — much faster than going through string parsing.
        """
        from rich.color import ColorType
        from rich.segment import Segment

        term_w, term_h = self._get_terminal_size()
        self._term_width = term_w
        self._term_height = term_h

        # Reuse a headless console sized to the window
        if self._gui_console is None or self._gui_console_size != (term_w, term_h):
            self._gui_console = Console(
                file=_io.StringIO(), width=term_w, height=term_h,
                force_terminal=True, color_system="truecolor",
            )
            self._gui_console_size = (term_w, term_h)
        console = self._gui_console

        layout = self._build_layout()
        raw_segments = console.render(layout)

        result: list[tuple[str, str | None, str | None, bool]] = []
        line_count = 0

        for seg in raw_segments:
            text = seg.text
            if seg.control:
                # Control segments are newlines / carriage returns
                for _code, _params in seg.control:
                    pass
                continue
            if not text:
                continue

            # Count lines and stop at terminal height
            nl_count = text.count("\n")
            if nl_count > 0:
                line_count += nl_count
                if line_count >= term_h:
                    # Include text up to the limit
                    keep_lines = nl_count - (line_count - term_h)
                    if keep_lines > 0:
                        parts = text.split("\n")
                        text = "\n".join(parts[:keep_lines])
                    else:
                        break

            style = seg.style
            fg_hex: str | None = None
            bg_hex: str | None = None
            bold = False

            if style:
                bold = bool(style.bold)
                color = style.color
                if color is not None:
                    if color.type == ColorType.TRUECOLOR and color.triplet:
                        t = color.triplet
                        fg_hex = f"#{t.red:02x}{t.green:02x}{t.blue:02x}"
                    elif color.type == ColorType.STANDARD and color.number is not None:
                        fg_hex = _RICH_STANDARD_COLORS.get(color.number)
                    elif color.type == ColorType.EIGHT_BIT and color.number is not None:
                        fg_hex = _rich_256_color(color.number)

                bgcolor = style.bgcolor
                if bgcolor is not None:
                    if bgcolor.type == ColorType.TRUECOLOR and bgcolor.triplet:
                        t = bgcolor.triplet
                        bg_hex = f"#{t.red:02x}{t.green:02x}{t.blue:02x}"
                    elif bgcolor.type == ColorType.STANDARD and bgcolor.number is not None:
                        bg_hex = _RICH_STANDARD_COLORS.get(bgcolor.number)
                    elif bgcolor.type == ColorType.EIGHT_BIT and bgcolor.number is not None:
                        bg_hex = _rich_256_color(bgcolor.number)

            result.append((text, fg_hex, bg_hex, bold))

        return result

    # Standby/CRT animation methods are in AnimationsMixin (tui_animations.py)

    async def run(self) -> None:
        if self._gui_mode:
            await self._run_gui()
        else:
            await self._run_terminal()

    async def _run_terminal(self) -> None:
        """Run in a real terminal (original mode)."""
        self._running = True
        input_task = asyncio.create_task(self._input_loop())

        # Enter alternate screen and hide cursor
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()

        try:
            term_w, term_h = self._get_terminal_size()

            # ── Phase 1: CRT boot animation (1.5s) ──
            duration = 1.5
            start = time.monotonic()
            while time.monotonic() - start < duration:
                progress = (time.monotonic() - start) / duration
                segs = self._crt_startup_segments(progress, term_w, term_h)
                frame = self._crt_to_ansi(segs, term_w, term_h)
                sys.stdout.write(f"\x1b[H{frame}")
                sys.stdout.flush()
                await asyncio.sleep(1 / 60)

            # ── Phase 2: Hold on static until connected ──
            self._connect_wait_start = time.monotonic()
            while self._running and not self.state.connected:
                term_w, term_h = self._get_terminal_size()
                segs = self._crt_static_hold_segments(term_w, term_h)
                frame = self._crt_to_ansi(segs, term_w, term_h)
                sys.stdout.write(f"\x1b[H{frame}")
                sys.stdout.flush()
                await asyncio.sleep(1 / 30)

            # ── Phase 3: Lights-on sweep (1.8s) ──
            if self._running:
                duration = 1.8
                start = time.monotonic()
                while time.monotonic() - start < duration and self._running:
                    term_w, term_h = self._get_terminal_size()
                    progress = (time.monotonic() - start) / duration
                    segs = self._crt_lights_on_segments(progress, term_w, term_h)
                    frame = self._crt_to_ansi(segs, term_w, term_h)
                    sys.stdout.write(f"\x1b[H{frame}")
                    sys.stdout.flush()
                    await asyncio.sleep(1 / 60)

            # ── Main render loop ──
            while self._running:
                fps = self._config.fps_limit if self._config else 30
                fps = max(5, min(120, fps))
                self._update_terminal_title()
                if self._check_standby():
                    term_w, term_h = self._get_terminal_size()
                    segs = self._standby_segments(term_w, term_h)
                    frame = self._crt_to_ansi(segs, term_w, term_h)
                    sys.stdout.write(f"\x1b[H{frame}")
                    sys.stdout.flush()
                    await asyncio.sleep(1 / max(5, fps // 2))  # slower in standby
                else:
                    frame = self._render_frame()
                    sys.stdout.write(f"\x1b[H{frame}")
                    sys.stdout.flush()
                    await asyncio.sleep(1 / fps)

        finally:
            # ── CRT shutdown animation ──
            term_w, term_h = self._get_terminal_size()
            last_segs = self._render_frame_gui()
            duration = 1.2
            start = time.monotonic()
            while time.monotonic() - start < duration:
                progress = (time.monotonic() - start) / duration
                segs = self._crt_shutdown_segments(progress, term_w, term_h, last_segs)
                frame = self._crt_to_ansi(segs, term_w, term_h)
                sys.stdout.write(f"\x1b[H{frame}")
                sys.stdout.flush()
                await asyncio.sleep(1 / 60)

            self._running = False
            # Reset terminal title and leave alternate screen
            sys.stdout.write("\x1b]0;\x07\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            input_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await input_task

    async def _run_gui(self) -> None:
        """Run inside a standalone tkinter window (separate process)."""
        from alfieprime_musiciser.gui import GUIProcess

        self._running = True
        gui = GUIProcess(
            title="AlfiePRIME Musiciser",
            on_key=self._handle_key,
            on_close=self.stop,
        )
        gui.start()
        self._gui_window = gui

        try:
            # ── Phase 1: CRT boot animation (1.5s) ──
            duration = 1.5
            start = time.monotonic()
            while time.monotonic() - start < duration and gui.alive:
                gui.process_events()
                progress = (time.monotonic() - start) / duration
                term_w, term_h = self._get_terminal_size()
                segs = self._crt_startup_segments(progress, term_w, term_h)
                gui.send_segments(segs)
                await asyncio.sleep(1 / 60)

            # ── Phase 2: Hold on static until connected ──
            self._connect_wait_start = time.monotonic()
            while self._running and gui.alive and not self.state.connected:
                gui.process_events()
                term_w, term_h = self._get_terminal_size()
                segs = self._crt_static_hold_segments(term_w, term_h)
                gui.send_segments(segs)
                await asyncio.sleep(1 / 30)

            # ── Phase 3: Lights-on sweep (1.8s) ──
            if self._running and gui.alive:
                duration = 1.8
                start = time.monotonic()
                while time.monotonic() - start < duration and gui.alive and self._running:
                    gui.process_events()
                    progress = (time.monotonic() - start) / duration
                    term_w, term_h = self._get_terminal_size()
                    segs = self._crt_lights_on_segments(progress, term_w, term_h)
                    gui.send_segments(segs)
                    await asyncio.sleep(1 / 60)

            # ── Main render loop ──
            while self._running and gui.alive:
                fps = self._config.fps_limit if self._config else 30
                fps = max(5, min(120, fps))
                gui.process_events()
                if self._check_standby():
                    term_w, term_h = self._get_terminal_size()
                    segments = self._standby_segments(term_w, term_h)
                    gui.send_segments(segments)
                    await asyncio.sleep(1 / max(5, fps // 2))
                else:
                    segments = self._render_frame_gui()
                    gui.send_segments(segments)
                    await asyncio.sleep(1 / fps)

        finally:
            # ── CRT shutdown animation ──
            if gui.alive:
                last_segs = self._render_frame_gui()
                term_w, term_h = self._get_terminal_size()
                duration = 1.2
                start = time.monotonic()
                while time.monotonic() - start < duration and gui.alive:
                    gui.process_events()
                    progress = (time.monotonic() - start) / duration
                    segs = self._crt_shutdown_segments(progress, term_w, term_h, last_segs)
                    gui.send_segments(segs)
                    await asyncio.sleep(1 / 60)

            self._running = False
            self._gui_window = None
            gui.stop()

    def stop(self) -> None:
        self._running = False
