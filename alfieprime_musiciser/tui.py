from __future__ import annotations

import asyncio
import contextlib
import io as _io
import math
import os
import random
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


_STANDBY_PHRASES = [
    "Standing by for further ear massages",
    "Waiting for the next sonic hug",
    "Ears on standby, ready for action",
    "The dance floor misses you",
    "Recharging the vibe capacitors",
    "Silence is just a long intro",
    "Your speakers called, they're bored",
    "Ready to drop beats at a moment's notice",
    "The party is just sleeping, not dead",
    "Buffering good vibes for later",
    "Sound check complete, awaiting deployment",
    "The bass is patiently waiting",
    "On hold for auditory adventures",
    "Shhh... the woofers are napping",
    "Standing by to convert electricity into joy",
    "The DJ stepped out for a coffee",
    "Your ears deserve a break too",
    "Loading next session of audio therapy",
    "Idle hands make no music, press play",
    "The speakers whisper: feed us",
    "Paused but not forgotten",
    "Music break in progress, stay tuned",
    "The rhythm will return shortly",
    "Conserving energy for maximum party output",
    "Awaiting orders from the groove commander",
]

STANDBY_TIMEOUT = 5 * 60  # seconds


class BoomBoxTUI:
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
            "auto_play", "auto_volume", "fps_limit",
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

    def _build_transition_layout(self) -> Group:
        """Render a CRT-style transition between modes.

        Phase 1 (0.0-0.4): Content collapses to a bright horizontal scanline
        Phase 2 (0.4-0.6): Scanline holds with phosphor glow
        Phase 3 (0.6-1.0): New content expands from the scanline
        """
        elapsed = time.monotonic() - self._transition_start
        progress = min(1.0, elapsed / self._transition_duration)

        if progress >= 1.0:
            self._transition_active = False
            # Fall through to the target layout
            if self._art_mode and self.state.artwork_data:
                return self._build_art_layout()
            return self._build_main_layout()

        self._term_width, self._term_height = self._get_terminal_size()
        term_w = self._term_width
        term_h = self._term_height
        mid_row = term_h // 2
        t = time.monotonic()
        th = self._get_effective_theme()

        lines: list[Text] = []

        if progress < 0.4:
            # Phase 1: collapse to scanline
            p = progress / 0.4
            visible_half = max(0, int((1.0 - p) * mid_row))
            for row in range(term_h):
                line = Text()
                dist = abs(row - mid_row)
                if dist <= visible_half:
                    edge_fade = 1.0 - (dist / max(visible_half, 1)) * 0.5
                    brightness = max(0.1, (1.0 - p) * edge_fade)
                    noise_chance = p * 0.4
                    chars = []
                    for _ in range(term_w):
                        if random.random() < noise_chance:
                            chars.append(random.choice("░▒▓"))
                        elif random.random() < 0.3:
                            chars.append(random.choice("·.─"))
                        else:
                            chars.append(" ")
                    br = 180 * brightness
                    pr, pg, pb = _hex_to_rgb(th.primary)
                    c = _safe_hex(
                        (br * 0.5 + pr * 0.5) * brightness,
                        (br * 0.5 + pg * 0.5) * brightness,
                        (br * 0.5 + pb * 0.5) * brightness,
                    )
                    if dist == 0:
                        scan_br = 100 + 155 * p
                        sc = _safe_hex(scan_br, scan_br + 20, scan_br)
                        line.append("━" * term_w, Style(color=sc, bold=True))
                    else:
                        line.append("".join(chars), Style(color=c))
                else:
                    line.append(" " * term_w)
                lines.append(line)

        elif progress < 0.6:
            # Phase 2: bright scanline hold with phosphor glow
            p = (progress - 0.4) / 0.2
            flicker = 0.9 + 0.1 * math.sin(t * 60)
            for row in range(term_h):
                line = Text()
                dist = abs(row - mid_row)
                if dist == 0:
                    br = 255 * flicker
                    pr, pg, pb = _hex_to_rgb(th.accent)
                    c = _safe_hex(br * 0.4 + pr * 0.6, br * 0.4 + pg * 0.6, br * 0.4 + pb * 0.6)
                    line.append("━" * term_w, Style(color=c, bold=True))
                elif dist <= 2:
                    glow = max(0, 0.4 - dist * 0.15) * flicker
                    br = 80 * glow
                    c = _safe_hex(br, br + 10, br)
                    noise = "".join(random.choice("░·  ") for _ in range(term_w))
                    line.append(noise, Style(color=c))
                else:
                    line.append(" " * term_w)
                lines.append(line)

        else:
            # Phase 3: expand new content from scanline
            p = (progress - 0.6) / 0.4
            visible_half = max(0, int(p * mid_row * 1.5))
            for row in range(term_h):
                line = Text()
                dist = abs(row - mid_row)
                if dist <= visible_half:
                    edge_fade = 1.0 - (dist / max(visible_half, 1)) * 0.4
                    brightness = min(1.0, p * 1.5) * edge_fade
                    flicker = 0.9 + 0.1 * math.sin(t * 40 + row * 2)
                    brightness *= flicker
                    noise_chance = max(0, (1.0 - p) * 0.5)
                    chars = []
                    for _ in range(term_w):
                        if random.random() < noise_chance:
                            chars.append(random.choice("░▒▓█"))
                        else:
                            chars.append(random.choice(" ·"))
                    br = 200 * brightness
                    pr, pg, pb = _hex_to_rgb(th.secondary)
                    c = _safe_hex(br * 0.6 + pr * 0.4, br * 0.6 + pg * 0.4, br * 0.6 + pb * 0.4)
                    line.append("".join(chars), Style(color=c))
                else:
                    line.append(" " * term_w)
                lines.append(line)

        return Group(*lines)

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

    # ── Settings menu dancers (easter egg) ──

    _MENU_DANCERS = [
        [[" o/", "/| ", "/ \\"], ["\\o ", " |\\", "/ \\"], ["\\o/", " | ", "/ \\"], [" o ", "/|\\", "/ \\"]],
        [["\\o/", " | ", "/ \\"], ["_o_", " | ", "| |"], ["\\o/", " | ", "/ \\"], [" o ", "-|-", "/ \\"]],
        [["*o*", "/|\\", "/ \\"], ["°o°", "\\|/", "\\ /"], ["*o*", "/|\\", ">< "], ["°o°", "\\|/", "/ \\"]],
        [["[o]", "/|\\", "| |"], ["[o]", "\\|/", "/ \\"], ["[o]", "-|-", "| |"], ["[o]", "_|_", "\\ /"]],
    ]

    # 16 preset colours for the static colour picker
    _COLOR_PRESETS: list[tuple[str, str]] = [
        ("Red", "#ff0000"), ("Orange", "#ff8800"), ("Yellow", "#ffff00"), ("Lime", "#88ff00"),
        ("Green", "#00ff00"), ("Teal", "#00ff88"), ("Cyan", "#00ffff"), ("Sky", "#0088ff"),
        ("Blue", "#0000ff"), ("Purple", "#8800ff"), ("Magenta", "#ff00ff"), ("Pink", "#ff0088"),
        ("White", "#ffffff"), ("Silver", "#aaaaaa"), ("Grey", "#555555"), ("Custom Hex", ""),
    ]

    _SKULL_GLYPHS = "☠💀⚠☢☣⛔"

    def _build_crt_background(self, term_w: int, term_h: int, danger: bool = False) -> list[Text]:
        """Generate animated CRT scanline background lines.

        If *danger* is True, scatter red-shaded skulls and warning signs through the static.
        """
        t = time.time()
        th = self.state.theme
        if danger:
            pr, pg, pb = 180, 0, 0  # red tint for danger background
        else:
            pr, pg, pb = _hex_to_rgb(th.primary)
        bg_lines: list[Text] = []
        for row in range(term_h):
            line = Text()
            # Rolling scanline: a bright band that scrolls down the screen
            scanline_pos = (t * 8) % term_h
            scan_dist = min(abs(row - scanline_pos), term_h - abs(row - scanline_pos))
            scan_glow = max(0, 1.0 - scan_dist / 4.0) * 0.3

            # Base flicker per row
            flicker = 0.06 + 0.04 * math.sin(t * 6 + row * 0.5)
            # Horizontal interference bands that drift
            band_phase = math.sin(t * 1.2 + row * 0.12) * 0.5 + 0.5
            if band_phase > 0.8:
                flicker += 0.08
            flicker += scan_glow

            br = 255 * flicker
            base_c = _safe_hex(br * 0.4 + pr * flicker * 0.3, br * 0.4 + pg * flicker * 0.3, br * 0.4 + pb * flicker * 0.3)

            if danger:
                # Build line char-by-char, injecting red skulls/warnings at random
                for col in range(term_w):
                    noise = random.random()
                    threshold = 0.06 + scan_glow * 0.3
                    if noise < 0.012:
                        # Skull / warning glyph in a random shade of red
                        glyph = random.choice(self._SKULL_GLYPHS)
                        shade = random.randint(80, 255)
                        g_val = random.randint(0, shade // 4)
                        line.append(glyph, Style(color=_safe_hex(shade, g_val, 0)))
                    elif noise < threshold * 0.4:
                        line.append(random.choice("░▒▓"), Style(color=base_c))
                    elif noise < threshold:
                        line.append(random.choice("·.╌"), Style(color=base_c))
                    else:
                        line.append(" ", Style(color=base_c))
            else:
                # Build chars with more visible noise
                chars = []
                for col in range(term_w):
                    noise = random.random()
                    # More noise near scanline
                    threshold = 0.06 + scan_glow * 0.3
                    if noise < threshold * 0.4:
                        chars.append(random.choice("░▒▓"))
                    elif noise < threshold:
                        chars.append(random.choice("·.╌"))
                    else:
                        chars.append(" ")
                line.append("".join(chars), Style(color=base_c))
            bg_lines.append(line)
        return bg_lines

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

    def _get_menu_fade(self) -> float:
        """Return current menu opacity (0.0-1.0) based on fade state."""
        if self._menu_fading_in:
            elapsed = time.monotonic() - self._menu_fade_start
            progress = min(1.0, elapsed / self._menu_fade_duration)
            if progress >= 1.0:
                self._menu_fading_in = False
            return progress
        if self._menu_fading_out:
            elapsed = time.monotonic() - self._menu_fade_start
            progress = min(1.0, elapsed / self._menu_fade_duration)
            if progress >= 1.0:
                self._menu_fading_out = False
                if self._menu_fade_callback:
                    self._menu_fade_callback()
                    self._menu_fade_callback = None
                # Start fade-in if menu is still open
                if self._settings_open:
                    self._menu_fading_in = True
                    self._menu_fade_start = time.monotonic()
                return 0.0
            return 1.0 - progress
        return 1.0

    def _start_menu_fade_in(self) -> None:
        """Begin a menu fade-in animation."""
        self._menu_fading_in = True
        self._menu_fading_out = False
        self._menu_fade_start = time.monotonic()

    def _start_menu_fade_out(self, callback: Callable[[], None] | None = None) -> None:
        """Begin a menu fade-out, calling callback when complete."""
        self._menu_fading_out = True
        self._menu_fading_in = False
        self._menu_fade_start = time.monotonic()
        self._menu_fade_callback = callback

    def _compose_panel_on_bg(
        self, bg_lines: list[Text], panel_lines: list[Text],
        panel_w: int, term_w: int, term_h: int,
        dancer_lines: list[Text] | None = None,
    ) -> Group:
        """Overlay a centered panel (with optional dancers below) onto CRT background."""
        fade = self._get_menu_fade()
        total_content = len(panel_lines)
        if dancer_lines:
            total_content += 1 + len(dancer_lines)  # 1 blank + dancers
        panel_x = max(0, (term_w - panel_w - 2) // 2)
        panel_y = max(0, (term_h - total_content) // 2)
        bg_a = int(10 * fade)
        panel_bg_style = Style(bgcolor=_safe_hex(bg_a, bg_a, bg_a))

        # Scale panel vertical extent by fade (expand from center)
        if fade < 1.0:
            visible_half = max(0, int(total_content * 0.5 * fade))
            center = total_content // 2
            fade_min = center - visible_half
            fade_max = center + visible_half
        else:
            fade_min = 0
            fade_max = total_content

        result_lines: list[Text] = []
        for row in range(term_h):
            content_idx = row - panel_y
            # Which content line does this map to?
            content_line: Text | None = None
            in_range = fade_min <= content_idx <= fade_max
            if 0 <= content_idx < len(panel_lines) and in_range:
                content_line = panel_lines[content_idx]
            elif dancer_lines and in_range:
                dancer_idx = content_idx - len(panel_lines) - 1
                if dancer_idx == -1:
                    content_line = Text("")
                elif 0 <= dancer_idx < len(dancer_lines):
                    content_line = dancer_lines[dancer_idx]

            if content_line is not None:
                line = Text()
                bg_text = bg_lines[row].plain if row < len(bg_lines) else " " * term_w
                bg_br = max(10, int(34 * fade))
                line.append(bg_text[:panel_x], Style(color=_safe_hex(bg_br, bg_br, bg_br)))
                content_plain = content_line.plain
                pad_needed = panel_w - len(content_plain)
                line.append(" ", panel_bg_style)
                line.append_text(content_line)
                if pad_needed > 0:
                    line.append(" " * pad_needed, panel_bg_style)
                line.append(" ", panel_bg_style)
                right_start = panel_x + panel_w + 2
                right_bg = bg_text[right_start:term_w]
                if right_bg:
                    line.append(right_bg, Style(color=_safe_hex(bg_br, bg_br, bg_br)))
                result_lines.append(line)
            else:
                result_lines.append(bg_lines[row] if row < len(bg_lines) else Text(" " * term_w))

        return Group(*result_lines)

    def _render_menu_dancers(self, panel_w: int) -> list[Text]:
        """Render a small row of dancers for settings menu easter egg."""
        if not self._settings_dancers:
            return []
        t = time.time()
        bounce = int(t * 3) % 4
        # Pick 3-5 dancers spread across panel width
        n_dancers = random.Random(42).randint(3, 5)
        spacing = max(4, panel_w // (n_dancers + 1))
        dancer_rows: list[list[str]] = [[] for _ in range(3)]
        rng = random.Random(42)
        for i in range(n_dancers):
            dtype = rng.randint(0, len(self._MENU_DANCERS) - 1)
            frames = self._MENU_DANCERS[dtype]
            frame = frames[bounce]
            for r in range(3):
                pad = spacing - len(frame[r])
                dancer_rows[r].append(frame[r] + " " * max(0, pad))

        lines: list[Text] = []
        colors = ["#ff55ff", "#55ffff", "#ffff55", "#55ff55", "#ff8855"]
        for r in range(3):
            line = Text()
            full = "".join(dancer_rows[r])
            # Center within panel
            pad_l = max(0, (panel_w - len(full)) // 2)
            line.append(" " * pad_l)
            for ci, ch in enumerate(full):
                c = colors[ci % len(colors)]
                line.append(ch, Style(color=c))
            lines.append(line)
        return lines

    def _build_settings_layout(self) -> Group:
        """Render settings menu with animated CRT background."""
        if self._settings_sub == "advanced":
            return self._build_advanced_layout()
        if self._settings_sub == "color_picker":
            return self._build_color_picker_layout()

        self._term_width, self._term_height = self._get_terminal_size()
        term_w = self._term_width
        term_h = self._term_height
        th = self.state.theme
        cfg = self._config or Config()

        menu_items: list[tuple[str, str, object]] = [
            ("Auto Play on Connect", "auto_play", cfg.auto_play),
            ("Auto Volume on Connect", "auto_volume", cfg.auto_volume),
            ("FPS Limit", "fps_limit", cfg.fps_limit),
            ("Show Artwork (Normal)", "show_artwork", cfg.show_artwork),
            ("Album Art Colours", "use_art_colors", cfg.use_art_colors),
            ("Static Colour", "static_color", cfg.static_color),
        ]

        panel_w = 54
        bg_lines = self._build_crt_background(term_w, term_h)

        # Build panel content
        panel_lines: list[Text] = []

        title_line = Text(justify="center")
        title_line.append("━" * panel_w, Style(color=th.primary, bold=True))
        panel_lines.append(title_line)

        header = Text(justify="center")
        header.append(" ◈ SETTINGS ◈ ", Style(color=th.primary, bold=True))
        panel_lines.append(header)

        title_line2 = Text(justify="center")
        title_line2.append("━" * panel_w, Style(color=th.primary, bold=True))
        panel_lines.append(title_line2)

        panel_lines.append(Text(""))

        for i, (label, key, value) in enumerate(menu_items):
            item = Text()
            selected = i == self._settings_cursor

            if selected:
                item.append("  ▸ ", Style(color=th.accent, bold=True))
            else:
                item.append("    ", Style(color="#444444"))

            item.append(f"{label:<30}", Style(
                color=th.secondary if selected else "#888888",
                bold=selected,
            ))

            # Value column — all right-aligned to 8 chars wide
            if key == "auto_volume":
                if value == -1:
                    val_str = "OFF"
                    val_color = "#666666"
                else:
                    val_str = f"{value}%"
                    val_color = th.accent
                item.append(f"{val_str:>8}", Style(color=val_color, bold=selected))
                if selected:
                    item.append("  ◂▸", Style(color="#555555"))
            elif key == "fps_limit":
                item.append(f"{value:>8}", Style(color=th.accent, bold=selected))
                if selected:
                    item.append("  ◂▸", Style(color="#555555"))
            elif key == "static_color":
                if value:
                    item.append(f"{value:>8}", Style(color=value, bold=selected))
                    item.append(" ██", Style(color=value))
                else:
                    item.append(f"{'None':>8}", Style(color="#666666"))
                if selected:
                    item.append("  Enter▸", Style(color="#555555"))
            elif key in ("auto_play", "show_artwork", "use_art_colors"):
                val_str = "ON" if value else "OFF"
                val_color = th.accent if value else "#666666"
                item.append(f"{val_str:>8}", Style(color=val_color, bold=selected))

            panel_lines.append(item)
            panel_lines.append(Text(""))

        # ── Advanced section link ──
        panel_lines.append(Text(""))
        t = time.time()
        glow = 0.5 + 0.5 * math.sin(t * 3)
        r_val = int(180 + 75 * glow)
        g_val = int(30 * glow)
        adv_color = _safe_hex(r_val, g_val, 0)
        adv_line = Text()
        adv_line.append("    [A] ", Style(color=adv_color, bold=True))
        adv_line.append("Advanced", Style(color=adv_color, bold=True))
        panel_lines.append(adv_line)

        # Footer
        panel_lines.append(Text(""))
        footer = Text(justify="center")
        footer.append("━" * panel_w, Style(color=th.primary_dim))
        panel_lines.append(footer)

        hint = Text(justify="center")
        hint.append("[↑↓] Navigate  ", Style(color="#555555"))
        hint.append("[Enter/Space] Toggle  ", Style(color="#555555"))
        hint.append("[◂▸] Adjust  ", Style(color="#555555"))
        hint.append("[/] Close", Style(color="#555555"))
        panel_lines.append(hint)

        dancer_lines = self._render_menu_dancers(panel_w)
        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h, dancer_lines)

    def _build_advanced_layout(self) -> Group:
        """Render advanced settings (client name, UUID, reset) with danger CRT background."""
        self._term_width, self._term_height = self._get_terminal_size()
        term_w = self._term_width
        term_h = self._term_height
        t = time.time()
        cfg = self._config or Config()

        panel_w = 58
        bg_lines = self._build_crt_background(term_w, term_h, danger=True)

        # ── Reset confirmation overlay ──
        if self._advanced_confirm_reset:
            return self._build_reset_confirm_layout(bg_lines, panel_w, term_w, term_h, t)

        panel_lines: list[Text] = []

        # Glowing red title
        glow = 0.5 + 0.5 * math.sin(t * 3)
        r_val = int(180 + 75 * glow)
        title_c = _safe_hex(r_val, 0, 0)

        title_line = Text(justify="center")
        title_line.append("━" * panel_w, Style(color=title_c, bold=True))
        panel_lines.append(title_line)

        header = Text(justify="center")
        header.append(" ☠ ADVANCED ☠ ", Style(color=title_c, bold=True))
        panel_lines.append(header)

        title_line2 = Text(justify="center")
        title_line2.append("━" * panel_w, Style(color=title_c, bold=True))
        panel_lines.append(title_line2)

        panel_lines.append(Text(""))

        warn = Text(justify="center")
        warn.append("Changing these may break server recognition", Style(color="#aa4444"))
        panel_lines.append(warn)
        panel_lines.append(Text(""))

        adv_items: list[tuple[str, str, str]] = [
            ("Client Name", "client_name", cfg.client_name),
            ("Client UUID", "client_id", cfg.client_id),
            ("Reset Config", "reset_config", ""),
        ]

        for i, (label, key, value) in enumerate(adv_items):
            item = Text()
            selected = i == self._advanced_cursor

            if key == "reset_config":
                # Special red destructive action
                if selected:
                    item.append("  ▸ ", Style(color="#ff0000", bold=True))
                    item.append(f"☠ {label} ☠", Style(color="#ff2222", bold=True))
                else:
                    item.append("    ", Style(color="#444444"))
                    item.append(f"☠ {label} ☠", Style(color="#882222"))
                panel_lines.append(item)
                panel_lines.append(Text(""))
                continue

            if selected:
                item.append("  ▸ ", Style(color="#ff4444", bold=True))
            else:
                item.append("    ", Style(color="#444444"))

            item.append(f"{label:<14}", Style(
                color="#ff8888" if selected else "#886666",
                bold=selected,
            ))

            if self._advanced_editing == key:
                # Show text input with cursor
                display = self._advanced_edit_buf
                cursor_blink = int(t * 2) % 2 == 0
                item.append(f" {display}", Style(color="#ffffff", bold=True))
                if cursor_blink:
                    item.append("▌", Style(color="#ff4444"))
                else:
                    item.append(" ")
            else:
                display = value
                if len(display) > 34:
                    display = display[:31] + "..."
                item.append(f" {display}", Style(color="#cc8888" if selected else "#666666"))

            panel_lines.append(item)
            panel_lines.append(Text(""))

        # Footer
        footer = Text(justify="center")
        footer.append("━" * panel_w, Style(color="#661111"))
        panel_lines.append(footer)

        hint = Text(justify="center")
        if self._advanced_editing:
            hint.append("[Type] Edit  ", Style(color="#555555"))
            hint.append("[Enter] Save  ", Style(color="#555555"))
            hint.append("[Esc] Cancel", Style(color="#555555"))
        else:
            hint.append("[↑↓] Navigate  ", Style(color="#555555"))
            hint.append("[Enter] Edit  ", Style(color="#555555"))
            hint.append("[/] Back", Style(color="#555555"))
        panel_lines.append(hint)

        dancer_lines = self._render_menu_dancers(panel_w)
        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h, dancer_lines)

    def _build_reset_confirm_layout(
        self, bg_lines: list[Text], panel_w: int, term_w: int, term_h: int, t: float,
    ) -> Group:
        """Big ASCII art warning confirmation for config reset."""
        panel_lines: list[Text] = []
        glow = 0.5 + 0.5 * math.sin(t * 4)
        r_val = int(180 + 75 * glow)
        warn_c = _safe_hex(r_val, 0, 0)
        dim_c = _safe_hex(r_val // 2, 0, 0)

        ascii_warning = [
            r"    ___   ___   ___  ",
            r"   /   \ /   \ /   \ ",
            r"  / /!\ | /!\ | /!\ \\",
            r" / /_!_\ /_!_\ /_!_\ \\",
            r" \_____/ \_____/ \_____/",
        ]

        panel_lines.append(Text(""))
        for art_line in ascii_warning:
            tl = Text(justify="center")
            tl.append(art_line, Style(color=warn_c, bold=True))
            panel_lines.append(tl)

        panel_lines.append(Text(""))

        title = Text(justify="center")
        title.append("━" * panel_w, Style(color=warn_c, bold=True))
        panel_lines.append(title)

        msg = Text(justify="center")
        msg.append(" RESET ALL CONFIGURATION? ", Style(color=warn_c, bold=True))
        panel_lines.append(msg)

        title2 = Text(justify="center")
        title2.append("━" * panel_w, Style(color=warn_c, bold=True))
        panel_lines.append(title2)

        panel_lines.append(Text(""))

        detail1 = Text(justify="center")
        detail1.append("This will delete your config file", Style(color="#cc4444"))
        panel_lines.append(detail1)

        detail2 = Text(justify="center")
        detail2.append("and restart the application.", Style(color="#cc4444"))
        panel_lines.append(detail2)

        panel_lines.append(Text(""))

        detail3 = Text(justify="center")
        detail3.append("You will need to re-run setup.", Style(color="#aa3333"))
        panel_lines.append(detail3)

        panel_lines.append(Text(""))
        panel_lines.append(Text(""))

        yn = Text(justify="center")
        yn.append("[Y] ", Style(color="#ff0000", bold=True))
        yn.append("Yes, reset  ", Style(color="#cc4444"))
        yn.append("  [N] ", Style(color="#44ff44", bold=True))
        yn.append("No, go back", Style(color="#44aa44"))
        panel_lines.append(yn)

        panel_lines.append(Text(""))

        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h)

    def _build_color_picker_layout(self) -> Group:
        """Render color picker submenu with 16 presets + hex input."""
        self._term_width, self._term_height = self._get_terminal_size()
        term_w = self._term_width
        term_h = self._term_height
        t = time.time()
        th = self.state.theme
        cfg = self._config or Config()

        panel_w = 46
        bg_lines = self._build_crt_background(term_w, term_h)

        panel_lines: list[Text] = []

        title_line = Text(justify="center")
        title_line.append("━" * panel_w, Style(color=th.primary, bold=True))
        panel_lines.append(title_line)

        header = Text(justify="center")
        header.append(" ◈ STATIC COLOUR ◈ ", Style(color=th.primary, bold=True))
        panel_lines.append(header)

        title_line2 = Text(justify="center")
        title_line2.append("━" * panel_w, Style(color=th.primary, bold=True))
        panel_lines.append(title_line2)

        panel_lines.append(Text(""))

        # Current selection
        cur = Text(justify="center")
        if cfg.static_color:
            cur.append("Current: ", Style(color="#888888"))
            cur.append(f"████ {cfg.static_color}", Style(color=cfg.static_color, bold=True))
        else:
            cur.append("Current: None", Style(color="#666666"))
        panel_lines.append(cur)
        panel_lines.append(Text(""))

        # Color grid: 4 columns x 4 rows
        for row_idx in range(4):
            line = Text()
            line.append("  ")
            for col_idx in range(4):
                i = row_idx * 4 + col_idx
                name, hex_val = self._COLOR_PRESETS[i]
                selected = i == self._color_cursor

                if selected:
                    line.append("▸", Style(color=th.accent, bold=True))
                else:
                    line.append(" ")

                if hex_val:
                    line.append("██", Style(color=hex_val))
                    line.append(f" {name:<7}", Style(
                        color="#ffffff" if selected else "#888888",
                        bold=selected,
                    ))
                else:
                    # Custom hex entry
                    if self._color_hex_editing:
                        cursor_blink = int(t * 2) % 2 == 0
                        display = self._color_hex_buf or "#"
                        line.append(f"{display:<7}", Style(color="#ffffff", bold=True))
                        if cursor_blink:
                            line.append("▌", Style(color=th.accent))
                        else:
                            line.append(" ")
                        # Pad to match other entries
                        pad = 3 - max(0, len(display) - 7)
                        if pad > 0:
                            line.append(" " * pad)
                    else:
                        line.append("## ", Style(color="#666666"))
                        line.append(f"{'Hex':<7}", Style(
                            color="#ffffff" if selected else "#888888",
                            bold=selected,
                        ))

            panel_lines.append(line)

        panel_lines.append(Text(""))

        # Clear option
        clear_selected = self._color_cursor == 16
        clear_line = Text()
        if clear_selected:
            clear_line.append("  ▸ ", Style(color=th.accent, bold=True))
        else:
            clear_line.append("    ")
        clear_line.append("Clear (use default theme)", Style(
            color="#ffffff" if clear_selected else "#888888",
            bold=clear_selected,
        ))
        panel_lines.append(clear_line)

        # Footer
        panel_lines.append(Text(""))
        footer = Text(justify="center")
        footer.append("━" * panel_w, Style(color=th.primary_dim))
        panel_lines.append(footer)

        hint = Text(justify="center")
        if self._color_hex_editing:
            hint.append("[Type] Hex  ", Style(color="#555555"))
            hint.append("[Enter] Apply  ", Style(color="#555555"))
            hint.append("[Esc] Cancel", Style(color="#555555"))
        else:
            hint.append("[↑↓◂▸] Navigate  ", Style(color="#555555"))
            hint.append("[Enter] Select  ", Style(color="#555555"))
            hint.append("[/] Back", Style(color="#555555"))
        panel_lines.append(hint)

        dancer_lines = self._render_menu_dancers(panel_w)
        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h, dancer_lines)

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
            art_h = max(8, term_h - reserved_rows)
            art_w = max(20, flush_inner)
            art_lines = render_braille_art(
                self.state.artwork_data, art_w, art_h, theme=th, hq=True,
            )
            if art_lines:
                parts.append(Panel(
                    Group(*art_lines),
                    title=" \u2592 ALBUM ART \u2592 ",
                    title_align="center", border_style=th.border_now_playing, padding=(0, 0),
                ))
            else:
                placeholder = Text("No artwork available", style=Style(color="#555555", italic=True))
                parts.append(Panel(
                    placeholder,
                    title=" \u2592 ALBUM ART \u2592 ",
                    title_align="center", border_style=th.border_now_playing, padding=(0, 0),
                ))
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

        # ── Key hint at bottom ──
        hint = Text()
        hint.append(" [A]rt ", self._hint_style("a", active=self._art_mode))
        hint.append("[C]alm ", self._hint_style("c", active=self._art_calm))
        hint.append("[P]lay ", self._hint_style("p"))
        hint.append("[N]ext ", self._hint_style("n"))
        hint.append("[B]ack ", self._hint_style("b"))
        hint.append("[S]huf ", self._hint_style("s", active=self.state.shuffle))
        hint.append("[R]epeat ", self._hint_style("r", active=self.state.repeat_mode != "off"))
        hint.append("[↑↓]Vol ", self._hint_style("vol"))
        hint.append("[/]Settings ", self._hint_style("/"))
        hint.append("[Q]uit", self._hint_style("q"))
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
            # 'w' exits settings from any submenu (unless editing text)
            if k == "w" and not self._advanced_editing and not self._color_hex_editing and not self._advanced_confirm_reset:
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

    def _handle_settings_main_key(self, k: str) -> None:
        """Handle keys in the main settings menu."""
        if k == "/" or k == "escape":
            self._start_menu_fade_out(lambda: setattr(self, '_settings_open', False))
        elif k == "a":
            def _switch_to_advanced() -> None:
                self._settings_sub = "advanced"
                self._advanced_cursor = 0
                self._advanced_editing = ""
            self._start_menu_fade_out(_switch_to_advanced)
        elif k == "arrow_up":
            self._settings_cursor = (self._settings_cursor - 1) % len(self._settings_items)
        elif k == "arrow_down":
            self._settings_cursor = (self._settings_cursor + 1) % len(self._settings_items)
        elif k in (" ", "\r", "\n"):
            self._settings_toggle_current()
        elif k == "arrow_left":
            self._settings_adjust(-1)
        elif k == "arrow_right":
            self._settings_adjust(1)

    def _handle_advanced_key(self, k: str, raw_key: str) -> None:
        """Handle keys in the advanced settings submenu."""
        # ── Reset confirmation dialog ──
        if self._advanced_confirm_reset:
            if k == "y":
                from alfieprime_musiciser.config import CONFIG_FILE
                try:
                    CONFIG_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                # Restart the application
                os.execv(sys.executable, [sys.executable] + sys.argv)
            elif k in ("n", "escape", "/"):
                self._advanced_confirm_reset = False
            return

        if self._advanced_editing:
            # Text editing mode
            if k == "escape":
                self._advanced_editing = ""
                self._advanced_edit_buf = ""
            elif k in ("\r", "\n"):
                # Save the edit
                cfg = self._config
                if cfg and self._advanced_edit_buf:
                    if self._advanced_editing == "client_name":
                        cfg.client_name = self._advanced_edit_buf
                    elif self._advanced_editing == "client_id":
                        cfg.client_id = self._advanced_edit_buf
                    cfg.save()
                self._advanced_editing = ""
                self._advanced_edit_buf = ""
            elif k == "backspace" or raw_key == "\x7f":
                self._advanced_edit_buf = self._advanced_edit_buf[:-1]
            elif len(raw_key) == 1 and raw_key.isprintable():
                self._advanced_edit_buf += raw_key
            return

        if k == "/" or k == "escape":
            self._start_menu_fade_out(lambda: setattr(self, '_settings_sub', ''))
        elif k == "arrow_up":
            self._advanced_cursor = (self._advanced_cursor - 1) % len(self._advanced_items)
        elif k == "arrow_down":
            self._advanced_cursor = (self._advanced_cursor + 1) % len(self._advanced_items)
        elif k in (" ", "\r", "\n"):
            field = self._advanced_items[self._advanced_cursor]
            if field == "reset_config":
                self._advanced_confirm_reset = True
                return
            cfg = self._config or Config()
            self._advanced_editing = field
            if field == "client_name":
                self._advanced_edit_buf = cfg.client_name
            elif field == "client_id":
                self._advanced_edit_buf = cfg.client_id

    def _handle_color_picker_key(self, k: str, raw_key: str) -> None:
        """Handle keys in the color picker submenu."""
        if self._color_hex_editing:
            if k == "escape":
                self._color_hex_editing = False
                self._color_hex_buf = ""
            elif k in ("\r", "\n"):
                # Validate and apply hex
                buf = self._color_hex_buf.strip()
                if not buf.startswith("#"):
                    buf = "#" + buf
                if len(buf) == 7:
                    try:
                        int(buf[1:], 16)
                        if self._config:
                            self._config.static_color = buf
                            self._config.save()
                    except ValueError:
                        pass
                self._color_hex_editing = False
                self._color_hex_buf = ""
                self._start_menu_fade_out(lambda: setattr(self, '_settings_sub', ''))
            elif k == "backspace" or raw_key == "\x7f":
                self._color_hex_buf = self._color_hex_buf[:-1]
            elif len(raw_key) == 1 and raw_key in "0123456789abcdefABCDEF#":
                if len(self._color_hex_buf) < 7:
                    self._color_hex_buf += raw_key
            return

        total = 17  # 16 presets + 1 clear
        if k == "/" or k == "escape":
            self._start_menu_fade_out(lambda: setattr(self, '_settings_sub', ''))
        elif k == "arrow_up":
            if self._color_cursor == 16:
                self._color_cursor = 12  # jump to last row
            elif self._color_cursor >= 4:
                self._color_cursor -= 4
        elif k == "arrow_down":
            if self._color_cursor >= 12 and self._color_cursor < 16:
                self._color_cursor = 16
            elif self._color_cursor < 12:
                self._color_cursor += 4
        elif k == "arrow_left":
            if self._color_cursor < 16 and self._color_cursor % 4 > 0:
                self._color_cursor -= 1
        elif k == "arrow_right":
            if self._color_cursor < 16 and self._color_cursor % 4 < 3:
                self._color_cursor += 1
        elif k in (" ", "\r", "\n"):
            if self._color_cursor == 16:
                # Clear
                if self._config:
                    self._config.static_color = ""
                    self._config.save()
                self._settings_sub = ""
            elif self._color_cursor == 15:
                # Custom hex
                self._color_hex_editing = True
                self._color_hex_buf = "#"
            else:
                _, hex_val = self._COLOR_PRESETS[self._color_cursor]
                if self._config:
                    self._config.static_color = hex_val
                    self._config.save()
                self._settings_sub = ""

    def _settings_toggle_current(self) -> None:
        """Toggle the currently selected settings item."""
        cfg = self._config or Config()
        item = self._settings_items[self._settings_cursor]
        if item == "auto_play":
            cfg.auto_play = not cfg.auto_play
        elif item == "auto_volume":
            cfg.auto_volume = -1 if cfg.auto_volume >= 0 else 50
        elif item == "show_artwork":
            cfg.show_artwork = not cfg.show_artwork
        elif item == "use_art_colors":
            cfg.use_art_colors = not cfg.use_art_colors
        elif item == "static_color":
            # Open color picker submenu
            self._settings_sub = "color_picker"
            self._color_cursor = 0
            self._color_hex_editing = False
            return
        if self._config:
            self._config = cfg
            cfg.save()

    def _settings_adjust(self, direction: int) -> None:
        """Adjust a numeric setting left/right."""
        cfg = self._config or Config()
        item = self._settings_items[self._settings_cursor]
        if item == "auto_volume":
            if cfg.auto_volume < 0:
                cfg.auto_volume = 50
            else:
                cfg.auto_volume = max(0, min(100, cfg.auto_volume + direction * 5))
        elif item == "fps_limit":
            cfg.fps_limit = max(5, min(120, cfg.fps_limit + direction * 5))
        else:
            return
        if self._config:
            self._config = cfg
            cfg.save()

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
        loop = asyncio.get_event_loop()
        while self._running:
            has_key = await loop.run_in_executor(None, msvcrt.kbhit)
            if has_key:
                data = await loop.run_in_executor(None, msvcrt.getch)
                if data:
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

    # ── Standby Screensaver ─────────────────────────────────────────────

    def _check_standby(self) -> bool:
        """Return True if the standby screensaver should be active."""
        if self._settings_open:
            return False
        if self.state.is_playing:
            self._last_playing_time = time.monotonic()
            if self._standby_active:
                self._standby_active = False
            return False
        idle = time.monotonic() - self._last_playing_time
        if idle >= STANDBY_TIMEOUT and self.state.connected:
            if not self._standby_active:
                self._standby_active = True
                self._standby_phrase_idx = random.randint(0, len(_STANDBY_PHRASES) - 1)
                self._standby_phrase_time = time.monotonic()
            return True
        return False

    def _standby_segments(
        self, term_w: int, term_h: int,
    ) -> list[tuple[str, str | None, str | None, bool]]:
        """Render a gentle standby screensaver with rotating phrases."""
        segs: list[tuple[str, str | None, str | None, bool]] = []
        t = time.monotonic()

        # Rotate phrase every 8 seconds
        if t - self._standby_phrase_time > 8.0:
            self._standby_phrase_idx = (self._standby_phrase_idx + 1) % len(_STANDBY_PHRASES)
            self._standby_phrase_time = t

        phrase = _STANDBY_PHRASES[self._standby_phrase_idx]

        # Floating position — gentle drift across screen
        drift_x = int((math.sin(t * 0.15) * 0.3 + 0.5) * max(term_w - len(phrase) - 4, 0))
        drift_y = int((math.sin(t * 0.1 + 1.0) * 0.3 + 0.5) * max(term_h - 6, 0))

        # Title
        title = "A L F I E P R I M E"
        title_x = max(0, (term_w - len(title)) // 2)

        # Zzz animation
        zzz_frames = ["z", "zz", "zzz", "zz"]
        zzz = zzz_frames[int(t * 0.8) % len(zzz_frames)]

        for row in range(term_h):
            line_segs: list[tuple[str, str | None, str | None, bool]] = []

            if row == drift_y:
                # Phrase row with gentle color animation
                pad_l = drift_x
                line_segs.append((" " * pad_l, None, None, False))
                # Fade-in effect based on time since phrase changed
                age = min(1.0, (t - self._standby_phrase_time) / 1.5)
                for i, ch in enumerate(phrase):
                    hue = (t * 0.05 + i * 0.02) % 1.0
                    r, g, b = _hsv_to_rgb(hue, 0.4, 0.3 + 0.2 * age)
                    c = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
                    line_segs.append((ch, c, None, False))
                pad_r = term_w - pad_l - len(phrase)
                if pad_r > 0:
                    line_segs.append((" " * pad_r, None, None, False))
            elif row == drift_y - 2:
                # Zzz above the phrase
                zzz_x = drift_x + len(phrase) + 1
                if zzz_x + len(zzz) < term_w:
                    line_segs.append((" " * zzz_x, None, None, False))
                    pulse = 0.2 + 0.15 * math.sin(t * 2)
                    br = int(255 * pulse)
                    line_segs.append((zzz, f"#{br:02x}{br:02x}{min(255, br + 30):02x}", None, False))
                    pad_r = term_w - zzz_x - len(zzz)
                    if pad_r > 0:
                        line_segs.append((" " * pad_r, None, None, False))
                else:
                    line_segs.append((" " * term_w, None, None, False))
            elif row == 1:
                # Title at top center, very dim
                line_segs.append((" " * title_x, None, None, False))
                pulse = 0.08 + 0.04 * math.sin(t * 0.5)
                br = int(255 * pulse)
                c = f"#{br:02x}{br:02x}{br:02x}"
                line_segs.append((title, c, None, False))
                pad_r = term_w - title_x - len(title)
                if pad_r > 0:
                    line_segs.append((" " * pad_r, None, None, False))
            elif row == term_h - 2:
                # Subtle hint at the bottom
                hint = "press play to wake up"
                hint_x = (term_w - len(hint)) // 2
                blink = 0.06 + 0.04 * math.sin(t * 1.5)
                br = int(255 * blink)
                c = f"#{br:02x}{br:02x}{br:02x}"
                line_segs.append((" " * hint_x, None, None, False))
                line_segs.append((hint, c, None, False))
                pad_r = term_w - hint_x - len(hint)
                if pad_r > 0:
                    line_segs.append((" " * pad_r, None, None, False))
            else:
                # Mostly black with occasional dim floating particles
                if random.random() < 0.005:
                    star_x = random.randint(0, term_w - 1)
                    twinkle = 0.03 + 0.03 * math.sin(t * 3 + star_x)
                    br = int(255 * twinkle)
                    c = f"#{br:02x}{br:02x}{br:02x}"
                    if star_x > 0:
                        line_segs.append((" " * star_x, None, None, False))
                    line_segs.append(("·", c, None, False))
                    pad_r = term_w - star_x - 1
                    if pad_r > 0:
                        line_segs.append((" " * pad_r, None, None, False))
                else:
                    line_segs.append((" " * term_w, None, None, False))

            segs.extend(line_segs)
            if row < term_h - 1:
                segs.append(("\n", None, None, False))

        return segs

    # ── CRT Animation ──────────────────────────────────────────────────────

    def _crt_startup_segments(
        self, progress: float, term_w: int, term_h: int,
    ) -> list[tuple[str, str | None, str | None, bool]]:
        """Generate CRT power-on animation segments (boot phase only).

        progress: 0.0 → 1.0
          0.00-0.20  Black screen, faint hum glow in center
          0.20-0.50  Bright horizontal scanline appears at center
          0.50-1.00  Scanline expands vertically into full static
        """
        segs: list[tuple[str, str | None, str | None, bool]] = []
        mid_row = term_h // 2

        if progress < 0.20:
            # Black with a faint center dot
            dot_brightness = int(progress / 0.20 * 60)
            c = f"#{dot_brightness:02x}{dot_brightness:02x}{dot_brightness:02x}"
            for row in range(term_h):
                if row == mid_row:
                    pad = term_w // 2 - 1
                    segs.append((" " * pad, None, None, False))
                    segs.append(("··", c, None, False))
                    segs.append((" " * (term_w - pad - 2), None, None, False))
                else:
                    segs.append((" " * term_w, None, None, False))
                if row < term_h - 1:
                    segs.append(("\n", None, None, False))

        elif progress < 0.50:
            # Bright horizontal scanline at center
            p = (progress - 0.20) / 0.30
            line_brightness = int(80 + p * 175)
            glow_w = max(4, int(p * term_w))
            pad_l = (term_w - glow_w) // 2
            pad_r = term_w - pad_l - glow_w
            c_bright = f"#{line_brightness:02x}{line_brightness:02x}{min(255, line_brightness + 30):02x}"
            c_dim = f"#{max(10, line_brightness // 4):02x}{max(10, line_brightness // 6):02x}{max(10, line_brightness // 3):02x}"
            for row in range(term_h):
                dist = abs(row - mid_row)
                if dist == 0:
                    segs.append((" " * pad_l, None, None, False))
                    segs.append(("━" * glow_w, c_bright, None, True))
                    segs.append((" " * pad_r, None, None, False))
                elif dist == 1 and p > 0.5:
                    segs.append((" " * pad_l, None, None, False))
                    segs.append(("─" * glow_w, c_dim, None, False))
                    segs.append((" " * pad_r, None, None, False))
                else:
                    segs.append((" " * term_w, None, None, False))
                if row < term_h - 1:
                    segs.append(("\n", None, None, False))

        else:
            # Expanding vertically — phosphor bloom into full static
            p = (progress - 0.50) / 0.50
            visible_half = max(1, int(p * mid_row * 1.5))
            for row in range(term_h):
                dist = abs(row - mid_row)
                if dist <= visible_half:
                    edge_fade = 1.0 - (dist / max(visible_half, 1)) * 0.6
                    flicker = 0.85 + 0.15 * math.sin(time.monotonic() * 60 + row * 3)
                    br = int(min(255, 200 * edge_fade * flicker))
                    r = int(br * 0.8)
                    g = int(min(255, br * 1.0))
                    b = int(min(255, br * 0.9))
                    c = f"#{r:02x}{g:02x}{b:02x}"
                    noise_density = max(0.2, (1.0 - p) * 0.6)
                    line = "".join(
                        random.choice("░▒▓█▌▐─━╌╍")
                        if random.random() < noise_density
                        else random.choice("·. ")
                        for _ in range(term_w)
                    )
                    segs.append((line, c, None, False))
                else:
                    segs.append((" " * term_w, None, None, False))
                if row < term_h - 1:
                    segs.append(("\n", None, None, False))

        return segs

    def _crt_static_hold_segments(
        self, term_w: int, term_h: int,
    ) -> list[tuple[str, str | None, str | None, bool]]:
        """Generate idle static with animated ASCII connecting art."""
        segs: list[tuple[str, str | None, str | None, bool]] = []
        t = time.time()

        # ── Build static background into a row list ──
        # Pre-generate random characters in bulk for speed
        _heavy = "░▒▓█▌▐╌╍·."
        _light = "·. ·  "
        static_rows: list[str] = []
        for row in range(term_h):
            randoms = random.random  # local ref for speed
            choice = random.choice
            buf_chars: list[str] = []
            for _ in range(term_w):
                if randoms() < 0.3:
                    buf_chars.append(choice(_heavy))
                else:
                    buf_chars.append(choice(_light))
            static_rows.append("".join(buf_chars))

        # ── Animated ASCII art ──
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        spin_ch = spinner[int(t * 10) % len(spinner)]

        # Signal wave animation — show 1-3 ripples cycling
        wave_count = (int(t * 3) % 3) + 1

        w1 = ")" if wave_count >= 1 else " "
        w2 = ")" if wave_count >= 2 else " "
        w3 = ")" if wave_count >= 3 else " "
        dots = "." * ((int(t * 2) % 3) + 1)

        art = [
            f"              .─────.            ",
            f"             /       \\      {w3}    ",
            f"            │  ◉   ◉  │    {w2}     ",
            f"            │    ▽    │   {w1}      ",
            f"             \\       /           ",
            f"              '──┬──'            ",
            f"                 │               ",
            f"            ┌────┴────┐          ",
            f"            │ ═══════ │          ",
            f"            │ ═══════ │          ",
            f"            └─────────┘          ",
            f"           ──────┬──────         ",
            f"                 │               ",
            f"     {spin_ch} Connecting{dots:<3}           ",
        ]

        art_h = len(art)
        art_w = max(len(line) for line in art)
        mid_row = term_h // 2
        start_row = mid_row - art_h // 2

        # ── Compose: overlay art on static ──
        for row in range(term_h):
            art_row_idx = row - start_row
            if 0 <= art_row_idx < art_h:
                art_line = art[art_row_idx]
                pad_l = max(0, (term_w - art_w) // 2)
                # Build the row: static | art | static
                left_static = static_rows[row][:pad_l]
                right_start = pad_l + len(art_line)
                right_static = static_rows[row][right_start:term_w]
                # Pad art to art_w
                art_padded = art_line.ljust(art_w)[:art_w]

                # Static portions — dim
                flicker = 0.5 + 0.2 * math.sin(t * 8 + row * 0.7)
                br = int(min(120, 70 * flicker))
                static_c = f"#{br:02x}{br:02x}{br:02x}"
                segs.append((left_static, static_c, None, False))

                # Art portion — bright with pulse
                pulse = 0.6 + 0.4 * math.sin(t * 2 + art_row_idx * 0.3)
                art_br = int(120 + 135 * pulse)
                # Box chars in cyan-ish, text in white
                for ch in art_padded:
                    if ch in "╭╮╰╯│─┌┐└┘├┤┬┴┼":
                        c_br = int(art_br * 0.6)
                        segs.append((ch, f"#{c_br:02x}{min(255, int(c_br * 1.3)):02x}{min(255, int(c_br * 1.2)):02x}", None, False))
                    elif ch in "◉⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
                        segs.append((ch, f"#{art_br:02x}{min(255, art_br):02x}{max(0, art_br - 40):02x}", None, True))
                    elif ch == ")":
                        # Signal waves — animated brightness
                        wave_pulse = 0.4 + 0.6 * math.sin(t * 6)
                        w_br = int(art_br * wave_pulse)
                        segs.append((ch, f"#{max(20, w_br):02x}{min(255, int(w_br * 1.2)):02x}{min(255, int(w_br * 1.1)):02x}", None, True))
                    elif ch.isalpha() or ch in ".:!":
                        segs.append((ch, f"#{art_br:02x}{art_br:02x}{min(255, art_br + 20):02x}", None, True))
                    else:
                        segs.append((ch, None, None, False))

                segs.append((right_static, static_c, None, False))
            else:
                # Pure static row
                flicker = 0.5 + 0.3 * math.sin(t * 8 + row * 0.7)
                br = int(min(150, 90 * flicker))
                static_c = f"#{br:02x}{br:02x}{br:02x}"
                segs.append((static_rows[row][:term_w], static_c, None, False))

            if row < term_h - 1:
                segs.append(("\n", None, None, False))

        return segs

    def _crt_lights_on_segments(
        self, progress: float, term_w: int, term_h: int,
    ) -> list[tuple[str, str | None, str | None, bool]]:
        """Panels light up from top-left to bottom-right like venue lights.

        progress: 0.0 → 1.0
        Real content fades in with a diagonal sweep, replacing static.
        """
        segs: list[tuple[str, str | None, str | None, bool]] = []
        real_segs = self._render_frame_gui()

        # Build a 2D grid of real content chars with their styles
        # real_segs is a flat list; we need to walk it row by row
        real_rows: list[list[tuple[str, str | None, str | None, bool]]] = [[]]
        for text, fg, bg, bold in real_segs:
            if "\n" in text:
                real_rows.append([])
            else:
                for ch in text:
                    real_rows[-1].append((ch, fg, bg, bold))

        t = time.time()
        # Diagonal threshold: top-left (0,0) lights first, bottom-right last
        max_dist = term_h + term_w
        threshold = progress * max_dist * 1.3  # overshoot so trailing edge completes
        inv_ramp = 1.0 / max(max_dist * 0.15, 1)
        inv_edge = 1.0 / max(max_dist * 0.1, 1)

        # Cache faded colors to avoid redundant hex parse/format per character
        _fade_cache: dict[tuple[str, int], str] = {}
        _space_seg = (" ", None, None, False)
        _segs_append = segs.append
        _sin = math.sin
        _rand = random.random

        for row_idx in range(term_h):
            real_row = real_rows[row_idx] if row_idx < len(real_rows) else []
            real_row_len = len(real_row)
            row_base = row_idx  # pre-compute for dist calc
            for col_idx in range(term_w):
                # Distance from top-left corner (diagonal sweep)
                dist = row_base + col_idx * 0.7
                if dist < threshold:
                    # This cell is "lit" — show real content
                    if col_idx < real_row_len:
                        ch, fg, bg, bold = real_row[col_idx]
                        # Brightness ramp: recently lit cells glow brighter
                        lit_age = (threshold - dist) * inv_ramp
                        if lit_age < 1.0 and fg:
                            # Quantize fade to reduce unique colors (32 levels)
                            fade_q = int(min(1.0, lit_age) * 31)
                            cache_key = (fg, fade_q)
                            faded_fg = _fade_cache.get(cache_key)
                            if faded_fg is None:
                                fade = fade_q / 31.0
                                # Inline hex parse to avoid function call overhead
                                fr = int(int(fg[1:3], 16) * fade)
                                fg_c = int(int(fg[3:5], 16) * fade)
                                fb = int(int(fg[5:7], 16) * fade)
                                faded_fg = f"#{fr:02x}{fg_c:02x}{fb:02x}"
                                _fade_cache[cache_key] = faded_fg
                            _segs_append((ch, faded_fg, bg, bold))
                        else:
                            _segs_append((ch, fg, bg, bold))
                    else:
                        _segs_append(_space_seg)
                else:
                    # Still dark — show fading static
                    fade_to_edge = max(0.0, 1.0 - (dist - threshold) * inv_edge)
                    if _rand() < 0.15 * fade_to_edge:
                        flicker = 0.5 + 0.5 * _sin(t * 12 + row_idx + col_idx)
                        br = int(50 * flicker * fade_to_edge)
                        c = f"#{br:02x}{br:02x}{br:02x}"
                        _segs_append((random.choice("░▒·"), c, None, False))
                    else:
                        _segs_append(_space_seg)

            if row_idx < term_h - 1:
                segs.append(("\n", None, None, False))

        return segs

    def _crt_shutdown_segments(
        self, progress: float, term_w: int, term_h: int,
        last_frame: list[tuple[str, str | None, str | None, bool]] | None = None,
    ) -> list[tuple[str, str | None, str | None, bool]]:
        """Generate CRT power-off animation segments.

        progress: 0.0 → 1.0
          0.00-0.20  Content gets noisy, brightness drops
          0.20-0.55  Collapses vertically to a bright horizontal line
          0.55-0.80  Line shrinks horizontally to a bright dot
          0.80-1.00  Dot fades out with afterglow
        """
        segs: list[tuple[str, str | None, str | None, bool]] = []
        mid_row = term_h // 2
        mid_col = term_w // 2

        if progress < 0.20:
            # Content with increasing noise and brightness drop
            p = progress / 0.20
            noise_chance = p * 0.5
            brightness_mult = 1.0 - p * 0.4
            src = last_frame or []
            for text, fg, bg, bold in src:
                if "\n" in text or not text.strip():
                    segs.append((text, fg, bg, bold))
                    continue
                out = []
                for ch in text:
                    if random.random() < noise_chance and ch.strip():
                        out.append(random.choice("░▒▓"))
                    else:
                        out.append(ch)
                # Dim the color
                new_fg = fg
                if fg and fg.startswith("#") and len(fg) == 7:
                    r = int(int(fg[1:3], 16) * brightness_mult)
                    g = int(int(fg[3:5], 16) * brightness_mult)
                    b = int(int(fg[5:7], 16) * brightness_mult)
                    new_fg = f"#{r:02x}{g:02x}{b:02x}"
                segs.append(("".join(out), new_fg, bg, bold))

        elif progress < 0.55:
            # Collapse to horizontal line
            p = (progress - 0.20) / 0.35
            visible_half = max(0, int((1.0 - p) * mid_row))
            flicker = 0.9 + 0.1 * math.sin(time.monotonic() * 80)
            for row in range(term_h):
                dist = abs(row - mid_row)
                if dist <= visible_half:
                    edge_fade = 1.0 - (dist / max(visible_half, 1)) * 0.7
                    br = int(min(255, (120 + 135 * p) * edge_fade * flicker))
                    c = f"#{int(br * 0.85):02x}{br:02x}{int(br * 0.9):02x}"
                    if dist == 0:
                        segs.append(("━" * term_w, c, None, True))
                    else:
                        noise = "".join(
                            random.choice("░▒ ·")
                            for _ in range(term_w)
                        )
                        segs.append((noise, c, None, False))
                else:
                    segs.append((" " * term_w, None, None, False))
                if row < term_h - 1:
                    segs.append(("\n", None, None, False))

        elif progress < 0.80:
            # Line shrinks to dot
            p = (progress - 0.55) / 0.25
            line_half = max(0, int((1.0 - p) * (term_w // 2)))
            br = int(min(255, 220 + 35 * (1 - p)))
            c_bright = f"#{int(br * 0.9):02x}{br:02x}{br:02x}"
            for row in range(term_h):
                if row == mid_row:
                    if line_half > 0:
                        pad_l = mid_col - line_half
                        pad_r = term_w - mid_col - line_half
                        segs.append((" " * max(0, pad_l), None, None, False))
                        segs.append(("━" * (line_half * 2), c_bright, None, True))
                        segs.append((" " * max(0, pad_r), None, None, False))
                    else:
                        pad = mid_col - 1
                        segs.append((" " * pad, None, None, False))
                        segs.append(("●", c_bright, None, True))
                        segs.append((" " * (term_w - pad - 1), None, None, False))
                else:
                    segs.append((" " * term_w, None, None, False))
                if row < term_h - 1:
                    segs.append(("\n", None, None, False))

        else:
            # Dot fades out with afterglow
            p = (progress - 0.80) / 0.20
            br = int(max(0, 255 * (1.0 - p)))
            glow_r = max(1, int(3 * (1.0 - p)))
            c = f"#{int(br * 0.7):02x}{int(br * 0.9):02x}{br:02x}"
            for row in range(term_h):
                dist = abs(row - mid_row)
                if dist <= glow_r and br > 10:
                    fade = 1.0 - dist / max(glow_r, 1)
                    dbr = int(br * fade * 0.5)
                    dc = f"#{int(dbr * 0.7):02x}{int(dbr * 0.9):02x}{dbr:02x}"
                    if dist == 0:
                        pad = mid_col - 1
                        segs.append((" " * pad, None, None, False))
                        segs.append(("●", c, None, True))
                        segs.append((" " * (term_w - pad - 1), None, None, False))
                    else:
                        pad = mid_col - 1
                        segs.append((" " * pad, None, None, False))
                        segs.append(("·", dc, None, False))
                        segs.append((" " * (term_w - pad - 1), None, None, False))
                else:
                    segs.append((" " * term_w, None, None, False))
                if row < term_h - 1:
                    segs.append(("\n", None, None, False))

        return segs

    def _crt_to_ansi(
        self, segs: list[tuple[str, str | None, str | None, bool]], term_w: int, term_h: int,
    ) -> str:
        """Convert CRT animation segments to an ANSI string for terminal mode."""
        buf = _io.StringIO()
        if self._crt_console is None or self._crt_console_size != (term_w, term_h):
            self._crt_console = Console(
                file=buf, width=term_w, height=term_h,
                force_terminal=True, color_system="truecolor", no_color=False,
            )
            self._crt_console_size = (term_w, term_h)
        else:
            self._crt_console._file = buf  # type: ignore[attr-defined]
        text = Text()
        for s_text, fg, bg, bold in segs:
            try:
                style = Style(color=fg, bgcolor=bg, bold=bold if bold else None)
            except Exception:
                style = Style(bold=bold if bold else None)
            text.append(s_text, style)
        self._crt_console.print(text, end="")
        return buf.getvalue()

    # ── Run loops ────────────────────────────────────────────────────────

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
