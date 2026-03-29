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


# Tab definitions — (tab_key, display_label)
_TABS = [
    ("general", "General"),
    ("sendspin", "SendSpin"),
    ("airplay", "AirPlay"),
    ("spotify", "Spotify"),
    ("advanced", "Advanced"),
]


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
        base_r, base_g, base_b = _hex_to_rgb(th.primary_dim) if th.primary_dim else (30, 30, 30)
        bg_lines: list[Text] = []
        noise_chars = "░▒▓·.╌"
        if danger:
            noise_chars = self._SKULL_GLYPHS
        phase = t * 8
        for row in range(term_h):
            line = Text()
            # Scanline brightness varies by row position relative to animated phase
            scan = math.sin(phase + row * 0.6) * 0.5 + 0.5
            glow = int(scan * 18)
            r = max(0, min(255, base_r // 6 + glow))
            g = max(0, min(255, base_g // 6 + glow))
            b = max(0, min(255, base_b // 6 + glow))
            if danger:
                g = 0
                b = 0
                r = max(0, min(255, 12 + glow * 2))
            color = _safe_hex(r, g, b)
            row_str_parts: list[str] = []
            for col in range(term_w):
                # Sparse noise with occasional characters
                seed = (row * 1337 + col * 7919 + int(t * 2)) % 137
                if seed < 8:
                    row_str_parts.append(noise_chars[seed % len(noise_chars)])
                else:
                    row_str_parts.append(" ")
            row_str = "".join(row_str_parts)
            # Add flicker band — a horizontal bright band that slowly scrolls
            band_y = int((t * 3) % (term_h + 20)) - 10
            dist = abs(row - band_y)
            if dist < 3:
                flicker = max(0, 12 - dist * 4)
                r2 = min(255, r + flicker * 6)
                g2 = min(255, g + flicker * (0 if danger else 4))
                b2 = min(255, b + flicker * (0 if danger else 4))
                color = _safe_hex(r2, g2, b2)
            line.append(row_str, Style(color=color))
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
    ) -> Group:
        """Overlay a centered panel onto CRT background."""
        fade = self._get_menu_fade()
        total_content = len(panel_lines)
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

    def _scatter_dancers_on_bg(
        self, bg_lines: list[Text], term_w: int, term_h: int,
        panel_x: int, panel_y: int, panel_w: int, panel_h: int,
    ) -> None:
        """Scatter animated dancers onto the CRT background, avoiding the panel area."""
        if not self._settings_dancers:
            return
        t = time.time()
        bounce = int(t * 3) % 4
        rng = random.Random(42)
        n_dancers = rng.randint(10, 16)
        colors = ["#ff55ff", "#55ffff", "#ffff55", "#55ff55", "#ff8855", "#ff5555", "#55ff88"]
        px0 = panel_x - 1
        px1 = panel_x + panel_w + 2
        py0 = panel_y - 1
        py1 = panel_y + panel_h + 1
        for i in range(n_dancers):
            dtype = rng.randint(0, len(self._MENU_DANCERS) - 1)
            base_x = rng.randint(1, max(1, term_w - 5))
            base_y = rng.randint(1, max(1, term_h - 5))
            dx = int(3 * math.sin(t * 0.5 + i * 2.3))
            dy = int(2 * math.sin(t * 0.4 + i * 1.7 + 0.8))
            x = max(0, min(term_w - 4, base_x + dx))
            y = max(0, min(term_h - 4, base_y + dy))
            if px0 <= x <= px1 and py0 <= y <= py1 + 3:
                continue
            if px0 <= x + 3 <= px1 and py0 <= y <= py1 + 3:
                continue
            frames = self._MENU_DANCERS[dtype]
            frame = frames[bounce]
            c = colors[i % len(colors)]
            style = Style(color=c, bold=True)
            for r in range(3):
                row_idx = y + r
                if 0 <= row_idx < len(bg_lines):
                    row_str = frame[r]
                    line = bg_lines[row_idx]
                    plain = line.plain
                    if x + 3 <= len(plain):
                        new_line = Text()
                        if x > 0:
                            new_line.append_text(line[:x])
                        new_line.append(row_str, style)
                        if x + 3 < len(plain):
                            new_line.append_text(line[x + 3:])
                        bg_lines[row_idx] = new_line

    # ── Tab items ──────────────────────────────────────────────────────────

    def _get_tab_items(self, tab: str) -> list[tuple[str, str, object]]:
        """Return (label, config_key, value) list for the given tab."""
        cfg = self._config or Config()
        if tab == "general":
            return [
                ("Auto Play on Connect", "auto_play", cfg.auto_play),
                ("Auto Volume on Connect", "auto_volume", cfg.auto_volume),
                ("FPS Limit", "fps_limit", cfg.fps_limit),
                ("Brightness", "brightness", cfg.brightness),
                ("Show Artwork (Normal)", "show_artwork", cfg.show_artwork),
                ("Album Art Colours", "use_art_colors", cfg.use_art_colors),
                ("Static Colour", "static_color", cfg.static_color),
                ("DJ Source Mode", "dj_source_mode", cfg.dj_source_mode),
            ]
        elif tab == "sendspin":
            return [
                ("SendSpin Receiver", "sendspin_enabled", cfg.sendspin_enabled),
                ("Device Swap Prompt", "swap_prompt", cfg.swap_prompt),
                *([("Auto Action", "swap_auto_action", cfg.swap_auto_action)] if not cfg.swap_prompt else []),
            ]
        elif tab == "airplay":
            return [
                ("AirPlay Receiver", "airplay_enabled", cfg.airplay_enabled),
                ("Forget Devices on Exit", "forget_airplay_devices", cfg.forget_airplay_devices),
            ]
        elif tab == "spotify":
            return [
                ("Spotify Connect", "spotify_enabled", cfg.spotify_enabled),
                ("Bitrate (kbps)", "spotify_bitrate", cfg.spotify_bitrate),
                ("Device Name", "spotify_device_name", cfg.spotify_device_name),
                ("Username", "spotify_username", cfg.spotify_username),
                ("Web API Client ID", "spotify_client_id", cfg.spotify_client_id),
            ]
        elif tab == "advanced":
            return [
                ("Client Name", "client_name", cfg.client_name),
                ("Client UUID", "client_id", cfg.client_id),
                ("Reset Config", "reset_config", ""),
            ]
        return []

    # ── Main layout ────────────────────────────────────────────────────────

    def _build_settings_layout(self) -> Group:
        """Render tab-based settings menu with animated CRT background."""
        if self._settings_sub == "color_picker":
            return self._build_color_picker_layout()
        self._term_width, self._term_height = self._get_terminal_size()
        term_w = self._term_width
        term_h = self._term_height
        th = self.state.theme
        cfg = self._config or Config()
        t = time.time()

        is_advanced = _TABS[self._settings_tab][0] == "advanced"
        danger = is_advanced

        if danger and self._advanced_confirm_reset:
            bg_lines = self._build_crt_background(term_w, term_h, danger=True)
            return self._build_reset_confirm_layout(bg_lines, 58, term_w, term_h, t)

        panel_w = 58
        bg_lines = self._build_crt_background(term_w, term_h, danger=danger)

        tab_key = _TABS[self._settings_tab][0]
        items = self._get_tab_items(tab_key)

        # Compute colors
        if danger:
            glow = 0.5 + 0.5 * math.sin(t * 3)
            r_val = int(180 + 75 * glow)
            title_c = _safe_hex(r_val, 0, 0)
            border_c = title_c
            item_sel_c = "#ff8888"
            item_c = "#886666"
            cursor_c = "#ff4444"
        else:
            title_c = th.primary
            border_c = th.primary
            item_sel_c = th.secondary
            item_c = "#888888"
            cursor_c = th.accent

        def _center(text: str) -> str:
            pad = max(0, (panel_w - len(text)) // 2)
            return " " * pad + text

        panel_lines: list[Text] = []

        # ── Tab bar ──
        tab_line = Text()
        tab_line.append(" ", Style())
        for i, (tkey, tlabel) in enumerate(_TABS):
            is_sel = i == self._settings_tab
            if is_sel:
                tab_line.append(f" {tlabel} ", Style(
                    color="black" if not danger else "#000000",
                    bgcolor=cursor_c,
                    bold=True,
                ))
            else:
                tab_line.append(f" {tlabel} ", Style(color="#666666"))
            if i < len(_TABS) - 1:
                tab_line.append("│", Style(color="#444444"))
        panel_lines.append(tab_line)

        # Title border
        title_line = Text()
        title_line.append("━" * panel_w, Style(color=border_c, bold=True))
        panel_lines.append(title_line)
        panel_lines.append(Text(""))

        # ── Warning for advanced tab ──
        if danger:
            warn = Text()
            warn.append(_center("Changing these may break server recognition"), Style(color="#aa4444"))
            panel_lines.append(warn)
            panel_lines.append(Text(""))

        # ── Note for protocol-affecting settings ──
        if tab_key in ("sendspin", "airplay", "spotify"):
            note = Text()
            note.append(_center("Protocol changes apply on next restart"), Style(color="#666666"))
            panel_lines.append(note)
            panel_lines.append(Text(""))

        # ── Menu items ──
        for i, (label, key, value) in enumerate(items):
            item = Text()
            selected = i == self._settings_cursor

            # Reset config gets special styling
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
                item.append("  ▸ ", Style(color=cursor_c, bold=True))
            else:
                item.append("    ", Style(color="#444444"))

            # Text-editable fields (advanced or spotify text fields)
            editable_keys = ("client_name", "client_id", "spotify_device_name",
                             "spotify_username", "spotify_client_id")
            if key in editable_keys:
                item.append(f"{label:<24}", Style(
                    color=item_sel_c if selected else item_c,
                    bold=selected,
                ))
                if self._advanced_editing == key:
                    display = self._advanced_edit_buf
                    cursor_blink = int(t * 2) % 2 == 0
                    item.append(f" {display}", Style(color="#ffffff", bold=True))
                    if cursor_blink:
                        item.append("▌", Style(color=cursor_c))
                    else:
                        item.append(" ")
                else:
                    display = str(value) if value else ""
                    if not display and key == "spotify_device_name":
                        import socket
                        display = f"Musiciser@{socket.gethostname()}"
                    if len(display) > 24:
                        display = display[:21] + "..."
                    item.append(f" {display}", Style(
                        color=(item_sel_c if selected else "#666666"),
                    ))
                panel_lines.append(item)
                panel_lines.append(Text(""))
                continue

            item.append(f"{label:<30}", Style(
                color=item_sel_c if selected else item_c,
                bold=selected,
            ))

            # Value display based on type
            if key == "auto_volume":
                if value == -1:
                    val_str, val_color = "OFF", "#666666"
                else:
                    val_str, val_color = f"{value}%", cursor_c
                item.append(f"{val_str:>8}", Style(color=val_color, bold=selected))
                if selected:
                    item.append("  ◂▸", Style(color="#555555"))
            elif key == "fps_limit":
                item.append(f"{value:>8}", Style(color=cursor_c, bold=selected))
                if selected:
                    item.append("  ◂▸", Style(color="#555555"))
            elif key == "brightness":
                item.append(f"{value}%".rjust(8), Style(color=cursor_c, bold=selected))
                if selected:
                    item.append("  ◂▸", Style(color="#555555"))
            elif key == "spotify_bitrate":
                item.append(f"{value}".rjust(8), Style(color=cursor_c, bold=selected))
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
            elif key == "swap_auto_action":
                val_str = str(value).upper()
                val_color = "#44ff44" if value == "accept" else "#ff4444"
                item.append(f"{val_str:>8}", Style(color=val_color, bold=selected))
                if selected:
                    item.append("  ◂▸", Style(color="#555555"))
            elif key == "dj_source_mode":
                _mode_labels = {
                    "mixed": "MIXED", "dual_sendspin": "DUAL SS", "dual_airplay": "DUAL AP",
                    "spotify_sendspin": "SS+SP", "spotify_airplay": "AP+SP", "dual_spotify": "DUAL SP",
                }
                val_str = _mode_labels.get(str(value), str(value).upper())
                item.append(f"{val_str:>8}", Style(color=cursor_c, bold=selected))
                if selected:
                    item.append("  ◂▸", Style(color="#555555"))
            elif key in ("auto_play", "show_artwork", "use_art_colors",
                         "airplay_enabled", "sendspin_enabled", "spotify_enabled",
                         "swap_prompt", "forget_airplay_devices"):
                val_str = "ON" if value else "OFF"
                val_color = cursor_c if value else "#666666"
                item.append(f"{val_str:>8}", Style(color=val_color, bold=selected))

            panel_lines.append(item)
            panel_lines.append(Text(""))

        # Pad to consistent height
        while len(panel_lines) < 14:
            panel_lines.append(Text(""))

        # Footer
        footer = Text()
        footer.append("━" * panel_w, Style(color=border_c if not danger else "#661111"))
        panel_lines.append(footer)

        # Hint line
        hint = Text()
        if self._advanced_editing:
            hint_text = "[Type] Edit  [Enter] Save  [Esc] Cancel"
            hint.append(" " * max(0, (panel_w - len(hint_text)) // 2))
            hint.append("[Type] Edit  ", Style(color="#555555"))
            hint.append("[Enter] Save  ", Style(color="#555555"))
            hint.append("[Esc] Cancel", Style(color="#555555"))
        else:
            hint_text = "[◂▸]Tab [↑↓]Nav [Enter]Edit [C]Close"
            hint.append(" " * max(0, (panel_w - len(hint_text)) // 2))
            hint.append("[◂▸]Tab ", Style(color="#555555"))
            hint.append("[↑↓]Nav ", Style(color="#555555"))
            hint.append("[Enter]Edit ", Style(color="#555555"))
            hint.append("[C]Close", Style(color="#555555"))
        panel_lines.append(hint)

        panel_x = max(0, (term_w - panel_w - 2) // 2)
        panel_y = max(0, (term_h - len(panel_lines)) // 2)
        self._scatter_dancers_on_bg(bg_lines, term_w, term_h, panel_x, panel_y, panel_w, len(panel_lines))
        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h)

    # ── Reset confirm (preserved from original) ───────────────────────────

    def _build_reset_confirm_layout(
        self, bg_lines: list[Text], panel_w: int, term_w: int, term_h: int, t: float,
    ) -> Group:
        """Big ASCII art warning confirmation for config reset."""
        panel_lines: list[Text] = []
        glow = 0.5 + 0.5 * math.sin(t * 4)
        r_val = int(50 + 200 * glow)
        warn_c = _safe_hex(r_val, 0, 0)
        deep_c = _safe_hex(max(20, r_val * 0.4), 0, 0)
        skull_pulse = 0.5 + 0.5 * math.sin(t * 6)
        skull_r = int(40 + 180 * skull_pulse)
        skull_c = _safe_hex(skull_r, 0, 0)

        def _center(text: str) -> str:
            pad = max(0, (panel_w - len(text)) // 2)
            return " " * pad + text

        skull_unit = "☠  "
        n_skulls = panel_w // len(skull_unit)
        skull_str = (skull_unit * n_skulls).rstrip()
        skull_str = _center(skull_str)

        skull_row = Text()
        skull_row.append(skull_str, Style(color=skull_c, bold=True))
        panel_lines.append(skull_row)
        panel_lines.append(Text(""))
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
        skull_row3 = Text()
        skull_row3.append(skull_str, Style(color=deep_c, bold=True))
        panel_lines.append(skull_row3)
        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h)

    # ── Color picker (preserved from original) ────────────────────────────

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

        # Current color preview
        if cfg.static_color:
            preview = Text()
            preview.append(_center(f"Current: {cfg.static_color}  "), Style(color=cfg.static_color))
            preview.append("████", Style(color=cfg.static_color))
            panel_lines.append(preview)
        else:
            preview = Text()
            preview.append(_center("Current: None (dynamic)"), Style(color="#666666"))
            panel_lines.append(preview)
        panel_lines.append(Text(""))

        # 4x4 grid of color presets
        for row in range(4):
            line = Text()
            line.append("  ", Style())
            for col in range(4):
                idx = row * 4 + col
                name, hex_val = self._COLOR_PRESETS[idx]
                selected = idx == self._color_cursor
                if idx == 15:  # Custom Hex
                    if self._color_hex_editing:
                        display = self._color_hex_buf
                        cursor_blink = int(t * 2) % 2 == 0
                        if selected:
                            line.append(f"[{display}", Style(color="#ffffff", bold=True))
                            if cursor_blink:
                                line.append("▌", Style(color=th.accent))
                            else:
                                line.append(" ")
                            pad = max(0, 8 - len(display) - 2)
                            line.append(" " * pad + "]", Style(color="#ffffff"))
                        else:
                            line.append(f" {name:<9}", Style(color="#888888"))
                    elif selected:
                        line.append(f"▸{name:<9}", Style(color=th.accent, bold=True))
                    else:
                        line.append(f" {name:<9}", Style(color="#888888"))
                elif selected:
                    line.append(f" ▸██ ", Style(color=hex_val, bold=True))
                    line.append(f"{name:<5}", Style(color=th.secondary, bold=True))
                else:
                    line.append(f"  ██ ", Style(color=hex_val))
                    line.append(f"{name:<5}", Style(color="#666666"))
            panel_lines.append(line)
            panel_lines.append(Text(""))

        # Clear button
        clear_line = Text()
        selected = self._color_cursor == 16
        if selected:
            clear_line.append(_center("▸ Clear (use dynamic) ◂"), Style(color=th.accent, bold=True))
        else:
            clear_line.append(_center("  Clear (use dynamic)  "), Style(color="#666666"))
        panel_lines.append(clear_line)
        panel_lines.append(Text(""))

        footer = Text()
        footer.append("━" * panel_w, Style(color=th.primary_dim))
        panel_lines.append(footer)
        hint = Text()
        hint_text = "[↑↓◂▸]Nav [Enter]Select [B]Back"
        hint.append(" " * max(0, (panel_w - len(hint_text)) // 2))
        hint.append("[↑↓◂▸]Nav ", Style(color="#555555"))
        hint.append("[Enter]Select ", Style(color="#555555"))
        hint.append("[B]Back", Style(color="#555555"))
        panel_lines.append(hint)

        panel_x = max(0, (term_w - panel_w - 2) // 2)
        panel_y = max(0, (term_h - len(panel_lines)) // 2)
        self._scatter_dancers_on_bg(bg_lines, term_w, term_h, panel_x, panel_y, panel_w, len(panel_lines))
        return self._compose_panel_on_bg(bg_lines, panel_lines, panel_w, term_w, term_h)

    # ── Key handling ───────────────────────────────────────────────────────

    def _handle_settings_main_key(self, k: str, raw_key: str = "") -> None:
        """Handle all settings key input (tab navigation + item interaction)."""
        if k == "escape":
            self._start_menu_fade_out(lambda: setattr(self, '_settings_open', False))
            return

        # ── Reset confirm overlay ──
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

        # ── Text editing mode ──
        if self._advanced_editing:
            if k == "escape":
                self._advanced_editing = ""
                self._advanced_edit_buf = ""
            elif k in ("\r", "\n"):
                cfg = self._config
                if cfg and self._advanced_edit_buf is not None:
                    key = self._advanced_editing
                    val = self._advanced_edit_buf
                    if key == "client_name":
                        cfg.client_name = val
                    elif key == "client_id":
                        cfg.client_id = val
                    elif key == "spotify_device_name":
                        cfg.spotify_device_name = val
                    elif key == "spotify_username":
                        cfg.spotify_username = val
                    elif key == "spotify_client_id":
                        cfg.spotify_client_id = val
                    cfg.save()
                self._advanced_editing = ""
                self._advanced_edit_buf = ""
            elif k == "backspace" or raw_key == "\x7f":
                self._advanced_edit_buf = self._advanced_edit_buf[:-1]
            elif len(raw_key) == 1 and raw_key.isprintable():
                self._advanced_edit_buf += raw_key
            return

        tab_key = _TABS[self._settings_tab][0]
        items = self._get_tab_items(tab_key)

        # ── Tab navigation ──
        if k == "arrow_left" and not items:
            self._settings_tab = (self._settings_tab - 1) % len(_TABS)
            self._settings_cursor = 0
            return
        if k == "arrow_right" and not items:
            self._settings_tab = (self._settings_tab + 1) % len(_TABS)
            self._settings_cursor = 0
            return

        # Tab switching: Tab key or number keys
        if k in ("1", "2", "3", "4", "5"):
            idx = int(k) - 1
            if idx < len(_TABS):
                self._settings_tab = idx
                self._settings_cursor = 0
            return
        if k == "tab" or k == "\t":
            self._settings_tab = (self._settings_tab + 1) % len(_TABS)
            self._settings_cursor = 0
            return

        if not items:
            return

        # ── Item navigation ──
        if k == "arrow_up":
            self._settings_cursor = (self._settings_cursor - 1) % len(items)
        elif k == "arrow_down":
            self._settings_cursor = (self._settings_cursor + 1) % len(items)
        elif k in (" ", "\r", "\n"):
            self._settings_toggle_current(tab_key, items)
        elif k in ("arrow_left", "arrow_right"):
            # Check if current item is adjustable
            if items and self._settings_cursor < len(items):
                key = items[self._settings_cursor][1]
                adjustable = ("auto_volume", "fps_limit", "brightness", "spotify_bitrate",
                              "swap_auto_action", "dj_source_mode")
                if key in adjustable:
                    direction = 1 if k == "arrow_right" else -1
                    self._settings_adjust_item(key, direction)
                else:
                    # Switch tab
                    direction = 1 if k == "arrow_right" else -1
                    self._settings_tab = (self._settings_tab + direction) % len(_TABS)
                    self._settings_cursor = 0

    def _settings_toggle_current(self, tab_key: str, items: list) -> None:
        """Toggle the currently selected settings item."""
        if self._settings_cursor >= len(items):
            return
        cfg = self._config or Config()
        label, key, value = items[self._settings_cursor]

        # Boolean toggles
        bool_keys = ("auto_play", "show_artwork", "use_art_colors",
                     "airplay_enabled", "sendspin_enabled", "spotify_enabled",
                     "swap_prompt", "forget_airplay_devices")
        if key in bool_keys:
            setattr(cfg, key, not getattr(cfg, key))
            cfg.save()
            if self._config:
                self._config = cfg
            return

        # Auto volume toggle (off <-> 50%)
        if key == "auto_volume":
            cfg.auto_volume = -1 if cfg.auto_volume >= 0 else 50
            cfg.save()
            if self._config:
                self._config = cfg
            return

        # Static color → open color picker
        if key == "static_color":
            def _open_color_picker() -> None:
                self._settings_sub = "color_picker"
                self._color_cursor = 0
                self._color_hex_editing = False
            self._start_menu_fade_out(_open_color_picker)
            return

        # Reset config
        if key == "reset_config":
            self._start_menu_fade_out(lambda: setattr(self, '_advanced_confirm_reset', True))
            return

        # Text-editable fields
        editable_keys = ("client_name", "client_id", "spotify_device_name",
                         "spotify_username", "spotify_client_id")
        if key in editable_keys:
            self._advanced_editing = key
            self._advanced_edit_buf = str(getattr(cfg, key, ""))
            return

        # Enum cycling (DJ source mode, auto action)
        if key == "dj_source_mode":
            self._settings_adjust_item(key, 1)
            return
        if key == "swap_auto_action":
            self._settings_adjust_item(key, 1)
            return

    def _settings_adjust_item(self, key: str, direction: int) -> None:
        """Adjust a numeric or enum setting."""
        cfg = self._config or Config()
        if key == "auto_volume":
            if cfg.auto_volume < 0:
                cfg.auto_volume = 50
            else:
                cfg.auto_volume = max(0, min(100, cfg.auto_volume + direction * 5))
        elif key == "fps_limit":
            cfg.fps_limit = max(5, min(120, cfg.fps_limit + direction * 5))
        elif key == "brightness":
            cfg.brightness = max(50, min(150, cfg.brightness + direction * 10))
        elif key == "spotify_bitrate":
            bitrates = [96, 160, 320]
            try:
                i = bitrates.index(cfg.spotify_bitrate)
            except ValueError:
                i = 2
            cfg.spotify_bitrate = bitrates[(i + direction) % len(bitrates)]
        elif key == "swap_auto_action":
            cfg.swap_auto_action = "accept" if cfg.swap_auto_action == "deny" else "deny"
        elif key == "dj_source_mode":
            _modes = ["mixed", "dual_sendspin", "dual_airplay",
                       "spotify_sendspin", "spotify_airplay", "dual_spotify"]
            _i = _modes.index(cfg.dj_source_mode) if cfg.dj_source_mode in _modes else 0
            cfg.dj_source_mode = _modes[(_i + direction) % len(_modes)]
        else:
            return
        if self._config:
            self._config = cfg
            cfg.save()

    # ── Color picker key handling (preserved) ─────────────────────────────

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
