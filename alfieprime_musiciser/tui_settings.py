from __future__ import annotations

import math
import os
import random
import sys
import time

from collections.abc import Callable

from rich.console import Group
from rich.style import Style
from rich.text import Text

from alfieprime_musiciser.colors import _hex_to_rgb
from alfieprime_musiciser.config import Config


def _safe_hex(r: int | float, g: int | float, b: int | float) -> str:
    return f"#{max(0,min(255,int(r))):02x}{max(0,min(255,int(g))):02x}{max(0,min(255,int(b))):02x}"


class SettingsMixin:
    _MENU_DANCERS = [
        [[" o/", "/| ", "/ \\"], ["\\o ", " |\\", "/ \\"], ["\\o/", " | ", "/ \\"], [" o ", "/|\\", "/ \\"]],
        [["\\o/", " | ", "/ \\"], ["_o_", " | ", "| |"], ["\\o/", " | ", "/ \\"], [" o ", "-|-", "/ \\"]],
        [["*o*", "/|\\", "/ \\"], ["°o°", "\\|/", "\\ /"], ["*o*", "/|\\", ">< "], ["°o°", "\\|/", "/ \\"]],
        [["[o]", "/|\\", "| |"], ["[o]", "\\|/", "/ \\"], ["[o]", "-|-", "| |"], ["[o]", "_|_", "\\ /"]],
    ]

    _COLOR_PRESETS: list[tuple[str, str]] = [
        ("Red", "#ff0000"), ("Orange", "#ff8800"), ("Yellow", "#ffff00"), ("Lime", "#88ff00"),
        ("Green", "#00ff00"), ("Teal", "#00ff88"), ("Cyan", "#00ffff"), ("Sky", "#0088ff"),
        ("Blue", "#0000ff"), ("Purple", "#8800ff"), ("Magenta", "#ff00ff"), ("Pink", "#ff0088"),
        ("White", "#ffffff"), ("Silver", "#aaaaaa"), ("Grey", "#555555"), ("Custom Hex", ""),
    ]

    _SKULL_GLYPHS = "☠⚠☢☣✖✕"  # single-width only — no emoji

    def _build_crt_background(self, term_w: int, term_h: int, danger: bool = False) -> list[Text]:
        """Generate animated CRT scanline background lines."""
        t = time.time()
        th = self.state.theme
        if danger:
            pr, pg, pb = 140, 0, 0
        else:
            pr, pg, pb = _hex_to_rgb(th.primary)
        bg_lines: list[Text] = []
        for row in range(term_h):
            line = Text()
            scanline_pos = (t * 8) % term_h
            scan_dist = min(abs(row - scanline_pos), term_h - abs(row - scanline_pos))
            scan_glow = max(0, 1.0 - scan_dist / 4.0) * 0.3
            flicker = 0.06 + 0.04 * math.sin(t * 6 + row * 0.5)
            band_phase = math.sin(t * 1.2 + row * 0.12) * 0.5 + 0.5
            if band_phase > 0.8:
                flicker += 0.08
            flicker += scan_glow
            br = 255 * flicker
            base_c = _safe_hex(br * 0.4 + pr * flicker * 0.3, br * 0.4 + pg * flicker * 0.3, br * 0.4 + pb * flicker * 0.3)
            if danger:
                # Pre-build row as (char, category) pairs, then batch by category
                row_chars: list[tuple[str, int]] = []  # (char, 0=bg 1=noise 2=skull)
                for col in range(term_w):
                    noise = random.random()
                    threshold = 0.06 + scan_glow * 0.3
                    if noise < 0.025:
                        row_chars.append((random.choice(self._SKULL_GLYPHS), 2))
                    elif noise < threshold * 0.4:
                        row_chars.append((random.choice("░▒▓"), 1))
                    elif noise < threshold:
                        row_chars.append((random.choice("·.╌"), 1))
                    else:
                        row_chars.append((" ", 0))
                # Batch consecutive same-category chars into single appends
                # Skulls get individual random red shades per group
                buf: list[str] = []
                cur_cat = -1
                for ch, cat in row_chars:
                    if cat != cur_cat and buf:
                        if cur_cat == 2:
                            shade = random.randint(60, 200)
                            line.append("".join(buf), Style(color=_safe_hex(shade, shade // 8, 0)))
                        else:
                            line.append("".join(buf), Style(color=base_c))
                        buf = []
                    cur_cat = cat
                    buf.append(ch)
                if buf:
                    if cur_cat == 2:
                        shade = random.randint(60, 200)
                        line.append("".join(buf), Style(color=_safe_hex(shade, shade // 8, 0)))
                    else:
                        line.append("".join(buf), Style(color=base_c))
            else:
                chars = []
                for col in range(term_w):
                    noise = random.random()
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
            total_content += 1 + len(dancer_lines)
        panel_x = max(0, (term_w - panel_w - 2) // 2)
        panel_y = max(0, (term_h - total_content) // 2)
        bg_a = int(10 * fade)
        panel_bg_style = Style(bgcolor=_safe_hex(bg_a, bg_a, bg_a))
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
        def _center(text: str) -> str:
            pad = max(0, (panel_w - len(text)) // 2)
            return " " * pad + text

        panel_lines: list[Text] = []
        title_line = Text()
        title_line.append("━" * panel_w, Style(color=th.primary, bold=True))
        panel_lines.append(title_line)
        header = Text()
        header.append(_center(" ◈ SETTINGS ◈ "), Style(color=th.primary, bold=True))
        panel_lines.append(header)
        title_line2 = Text()
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
        panel_lines.append(Text(""))
        footer = Text()
        footer.append("━" * panel_w, Style(color=th.primary_dim))
        panel_lines.append(footer)
        hint_text = "[↑↓] Navigate  [Enter/Space] Toggle  [◂▸] Adjust  [C] Close"
        hint = Text()
        hint.append(" " * max(0, (panel_w - len(hint_text)) // 2))
        hint.append("[↑↓] Navigate  ", Style(color="#555555"))
        hint.append("[Enter/Space] Toggle  ", Style(color="#555555"))
        hint.append("[◂▸] Adjust  ", Style(color="#555555"))
        hint.append("[C] Close", Style(color="#555555"))
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
        if self._advanced_confirm_reset:
            return self._build_reset_confirm_layout(bg_lines, panel_w, term_w, term_h, t)
        def _center(text: str) -> str:
            pad = max(0, (panel_w - len(text)) // 2)
            return " " * pad + text

        panel_lines: list[Text] = []
        glow = 0.5 + 0.5 * math.sin(t * 3)
        r_val = int(180 + 75 * glow)
        title_c = _safe_hex(r_val, 0, 0)
        title_line = Text()
        title_line.append("━" * panel_w, Style(color=title_c, bold=True))
        panel_lines.append(title_line)
        header = Text()
        header.append(_center(" ☠ ADVANCED ☠ "), Style(color=title_c, bold=True))
        panel_lines.append(header)
        title_line2 = Text()
        title_line2.append("━" * panel_w, Style(color=title_c, bold=True))
        panel_lines.append(title_line2)
        panel_lines.append(Text(""))
        warn = Text()
        warn.append(_center("Changing these may break server recognition"), Style(color="#aa4444"))
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
        footer = Text()
        footer.append("━" * panel_w, Style(color="#661111"))
        panel_lines.append(footer)
        hint = Text()
        if self._advanced_editing:
            hint_text = "[Type] Edit  [Enter] Save  [Esc] Cancel"
            hint.append(" " * max(0, (panel_w - len(hint_text)) // 2))
            hint.append("[Type] Edit  ", Style(color="#555555"))
            hint.append("[Enter] Save  ", Style(color="#555555"))
            hint.append("[Esc] Cancel", Style(color="#555555"))
        else:
            hint_text = "[↑↓] Navigate  [Enter] Edit  [B] Back"
            hint.append(" " * max(0, (panel_w - len(hint_text)) // 2))
            hint.append("[↑↓] Navigate  ", Style(color="#555555"))
            hint.append("[Enter] Edit  ", Style(color="#555555"))
            hint.append("[B] Back", Style(color="#555555"))
        panel_lines.append(hint)
        dancer_lines = self._render_menu_dancers(panel_w)
        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h, dancer_lines)

    def _build_reset_confirm_layout(
        self, bg_lines: list[Text], panel_w: int, term_w: int, term_h: int, t: float,
    ) -> Group:
        """Big ASCII art warning confirmation for config reset."""
        panel_lines: list[Text] = []
        # Dark-to-bright red glow
        glow = 0.5 + 0.5 * math.sin(t * 4)
        r_val = int(50 + 200 * glow)
        warn_c = _safe_hex(r_val, 0, 0)
        deep_c = _safe_hex(max(20, r_val * 0.4), 0, 0)
        skull_pulse = 0.5 + 0.5 * math.sin(t * 6)
        skull_r = int(40 + 180 * skull_pulse)
        skull_c = _safe_hex(skull_r, 0, 0)

        def _center(text: str) -> str:
            """Pad text with leading spaces to center within panel_w."""
            pad = max(0, (panel_w - len(text)) // 2)
            return " " * pad + text

        # Skull border — double-spaced, pre-calculated to fit panel_w
        skull_unit = "☠  "  # 3 chars per unit
        n_skulls = panel_w // len(skull_unit)
        skull_str = (skull_unit * n_skulls).rstrip()
        skull_str = _center(skull_str)

        skull_row = Text()
        skull_row.append(skull_str, Style(color=skull_c, bold=True))
        panel_lines.append(skull_row)
        panel_lines.append(Text(""))
        # Warning triangles — all lines exactly 24 chars
        ascii_warning = [
            "   /\\      /\\      /\\   ",
            "  /!!\\    /!!\\    /!!\\  ",
            " / !! \\  / !! \\  / !! \\ ",
            "/______\\/______\\/______\\",
        ]
        for art_line in ascii_warning:
            tl = Text()
            tl.append(_center(art_line), Style(color=warn_c, bold=True))
            panel_lines.append(tl)
        panel_lines.append(Text(""))
        # Second skull row
        skull_row2 = Text()
        skull_row2.append(skull_str, Style(color=skull_c, bold=True))
        panel_lines.append(skull_row2)
        title = Text()
        title.append("━" * panel_w, Style(color=warn_c, bold=True))
        panel_lines.append(title)
        msg = Text()
        msg.append(_center("☠ RESET ALL CONFIGURATION? ☠"), Style(color=warn_c, bold=True))
        panel_lines.append(msg)
        title2 = Text()
        title2.append("━" * panel_w, Style(color=warn_c, bold=True))
        panel_lines.append(title2)
        panel_lines.append(Text(""))
        detail1 = Text()
        detail1.append(_center("This will delete your config file"), Style(color="#aa2222"))
        panel_lines.append(detail1)
        detail2 = Text()
        detail2.append(_center("and restart the application."), Style(color="#aa2222"))
        panel_lines.append(detail2)
        panel_lines.append(Text(""))
        detail3 = Text()
        detail3.append(_center("☢ You will need to re-run setup ☢"), Style(color="#881111"))
        panel_lines.append(detail3)
        panel_lines.append(Text(""))
        panel_lines.append(Text(""))
        yn_text = "[Y] Yes, reset    [N] No, go back"
        yn = Text()
        yn.append(" " * max(0, (panel_w - len(yn_text)) // 2))
        yn.append("[Y] ", Style(color="#cc0000", bold=True))
        yn.append("Yes, reset    ", Style(color="#aa2222"))
        yn.append("[N] ", Style(color="#44ff44", bold=True))
        yn.append("No, go back", Style(color="#44aa44"))
        panel_lines.append(yn)
        panel_lines.append(Text(""))
        # Bottom skull row
        skull_row3 = Text()
        skull_row3.append(skull_str, Style(color=deep_c, bold=True))
        panel_lines.append(skull_row3)
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
        def _center(text: str) -> str:
            pad = max(0, (panel_w - len(text)) // 2)
            return " " * pad + text

        panel_lines: list[Text] = []
        title_line = Text()
        title_line.append("━" * panel_w, Style(color=th.primary, bold=True))
        panel_lines.append(title_line)
        header = Text()
        header.append(_center(" ◈ STATIC COLOUR ◈ "), Style(color=th.primary, bold=True))
        panel_lines.append(header)
        title_line2 = Text()
        title_line2.append("━" * panel_w, Style(color=th.primary, bold=True))
        panel_lines.append(title_line2)
        panel_lines.append(Text(""))
        cur = Text()
        if cfg.static_color:
            cur_text = f"Current: ████ {cfg.static_color}"
            cur.append(" " * max(0, (panel_w - len(cur_text)) // 2))
            cur.append("Current: ", Style(color="#888888"))
            cur.append(f"████ {cfg.static_color}", Style(color=cfg.static_color, bold=True))
        else:
            cur.append(_center("Current: None"), Style(color="#666666"))
        panel_lines.append(cur)
        panel_lines.append(Text(""))
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
                    if self._color_hex_editing:
                        cursor_blink = int(t * 2) % 2 == 0
                        display = self._color_hex_buf or "#"
                        line.append(f"{display:<7}", Style(color="#ffffff", bold=True))
                        if cursor_blink:
                            line.append("▌", Style(color=th.accent))
                        else:
                            line.append(" ")
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
        panel_lines.append(Text(""))
        footer = Text()
        footer.append("━" * panel_w, Style(color=th.primary_dim))
        panel_lines.append(footer)
        hint = Text()
        if self._color_hex_editing:
            hint_text = "[Type] Hex  [Enter] Apply  [Esc] Cancel"
            hint.append(" " * max(0, (panel_w - len(hint_text)) // 2))
            hint.append("[Type] Hex  ", Style(color="#555555"))
            hint.append("[Enter] Apply  ", Style(color="#555555"))
            hint.append("[Esc] Cancel", Style(color="#555555"))
        else:
            hint_text = "[↑↓◂▸] Navigate  [Enter] Select  [B] Back"
            hint.append(" " * max(0, (panel_w - len(hint_text)) // 2))
            hint.append("[↑↓◂▸] Navigate  ", Style(color="#555555"))
            hint.append("[Enter] Select  ", Style(color="#555555"))
            hint.append("[B] Back", Style(color="#555555"))
        panel_lines.append(hint)
        dancer_lines = self._render_menu_dancers(panel_w)
        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h, dancer_lines)

    def _handle_settings_main_key(self, k: str) -> None:
        """Handle keys in the main settings menu."""
        if k == "escape":
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
        if self._advanced_confirm_reset:
            if k == "y":
                from alfieprime_musiciser.config import CONFIG_FILE
                try:
                    CONFIG_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                os.execv(sys.executable, [sys.executable] + sys.argv)
            elif k in ("n", "escape", "/"):
                self._start_menu_fade_out(lambda: setattr(self, '_advanced_confirm_reset', False))
            return
        if self._advanced_editing:
            if k == "escape":
                self._advanced_editing = ""
                self._advanced_edit_buf = ""
            elif k in ("\r", "\n"):
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
        if k == "b" or k == "escape":
            self._start_menu_fade_out(lambda: setattr(self, '_settings_sub', ''))
        elif k == "arrow_up":
            self._advanced_cursor = (self._advanced_cursor - 1) % len(self._advanced_items)
        elif k == "arrow_down":
            self._advanced_cursor = (self._advanced_cursor + 1) % len(self._advanced_items)
        elif k in (" ", "\r", "\n"):
            field = self._advanced_items[self._advanced_cursor]
            if field == "reset_config":
                self._start_menu_fade_out(lambda: setattr(self, '_advanced_confirm_reset', True))
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
        total = 17
        if k == "b" or k == "escape":
            self._start_menu_fade_out(lambda: setattr(self, '_settings_sub', ''))
        elif k == "arrow_up":
            if self._color_cursor == 16:
                self._color_cursor = 12
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
                if self._config:
                    self._config.static_color = ""
                    self._config.save()
                self._settings_sub = ""
            elif self._color_cursor == 15:
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
