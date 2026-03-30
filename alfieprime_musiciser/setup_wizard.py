"""Animated interactive setup wizard for AlfiePRIME Musiciser.

Walks the user through every configurable setting grouped into logical
sections.  Each section is rendered as a full TUI screen with CRT-style
background, cursor navigation, and inline editing — matching the look and
feel of the in-app settings menu.
"""
from __future__ import annotations

import io as _io
import math
import os
import random
import shutil
import sys
import time

from rich.console import Console, Group
from rich.style import Style
from rich.text import Text

from alfieprime_musiciser.config import Config
from alfieprime_musiciser.tui_settings import _HELP_TEXT

IS_WINDOWS = sys.platform == "win32"

# ── ASCII art per section ────────────────────────────────────────────────────

_ART_INTRO = r"""
       ╭──────────────────────────────────╮
       │  ┌─┐  ╔═══════════════╗  ┌─┐    │
       │  │◉│  ║               ║  │◉│    │
       │  │ │  ║   M U S I C   ║  │ │    │
       │  │ │  ║   I S E R     ║  │ │    │
       │  │◉│  ║               ║  │◉│    │
       │  └─┘  ╚═══════════════╝  └─┘    │
       │   ◯◯◯    [▓▓▓▓▓▓▓▓▓]    ◯◯◯    │
       ╰──────────────────────────────────╯
""".strip("\n")

_ART_CONNECTION = r"""
        ╱╲
       ╱  ╲        ·    ·
      ╱    ╲    ·    ·
     ╱  /\  ╲       ·    ·
    ╱  /  \  ╲
   ╱  /    \  ╲    ))
   ╰──┤    ├──╯   ))
      │ ▓▓ │
   ───┴────┴───
""".strip("\n")

_ART_DISPLAY = r"""
    ╔══════════════════════╗
    ║  ▓░▓░▓░▓░▓░▓░▓░▓░  ║
    ║  ░▓░▓░▓░▓░▓░▓░▓░▓  ║
    ║  ▓░▓░▓░▓░▓░▓░▓░▓░  ║
    ║  ░▓░▓░▓░▓░▓░▓░▓░▓  ║
    ╚══════════════════════╝
         ╱══════════╲
        ╱────────────╲
""".strip("\n")

_ART_PLAYBACK = r"""
         ╭───╮
        ╱ ╭─╮ ╲
       │ ╱   ╲ │
       │ │ ● │ │
       │ ╲   ╱ │
        ╲ ╰─╯ ╱
         ╰───╯
       ♪  ♫  ♪  ♫
""".strip("\n")

_ART_PROTOCOL = r"""
          │
         ╱│╲
        ╱ │ ╲        ·  ·
       ╱  │  ╲    ·  ·
      ╱   │   ╲      ·  ·
          │         ))
       ───┼───     ))
          │
""".strip("\n")

_ART_DJ = r"""
     ╭─────╮   ╭─────╮
     │ ╭─╮ │   │ ╭─╮ │
     │ │●│ │   │ │●│ │
     │ ╰─╯ │   │ ╰─╯ │
     ╰─────╯   ╰─────╯
       ╰────┬┬┬────╯
            │││
""".strip("\n")

_ART_SPOTIFY = r"""
        ╭───────────╮
       ╱  ═══════    ╲
      │   ═══════     │
      │    ═══════    │
       ╲    ═══════  ╱
        ╰───────────╯
          ♪  ♫  ♪
""".strip("\n")

_ART_SUMMARY = r"""
    ╔═══════════════════════╗
    ║   ┌─┐  ✓  DONE  ┌─┐  ║
    ║   └─┘           └─┘  ║
    ╚═══════════════════════╝
""".strip("\n")

_SECTION_DEFS = [
    ("CONNECTION", _ART_CONNECTION, "#00ccff"),
    ("DISPLAY", _ART_DISPLAY, "#ff88ff"),
    ("PLAYBACK", _ART_PLAYBACK, "#88ff44"),
    ("PROTOCOL", _ART_PROTOCOL, "#ffaa00"),
    ("SPOTIFY", _ART_SPOTIFY, "#1db954"),
    ("DJ MODE", _ART_DJ, "#ff4488"),
    ("SUMMARY", _ART_SUMMARY, "#00ff88"),
]

_TITLE_BANNER_SETUP = " M U S I C I S E R   S E T U P "


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hex(r: int | float, g: int | float, b: int | float) -> str:
    return f"#{max(0,min(255,int(r))):02x}{max(0,min(255,int(g))):02x}{max(0,min(255,int(b))):02x}"


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _dim(color: str, factor: float) -> str:
    r, g, b = _hex_to_rgb(color)
    return _hex(r * factor, g * factor, b * factor)


def _center(text: str, width: int = 50) -> str:
    return text.center(width)


def _term_size() -> tuple[int, int]:
    sz = shutil.get_terminal_size((80, 24))
    return sz.columns, sz.lines


def _hsv_to_rgb_simple(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Simple HSV->RGB (0-1 inputs, 0-255 outputs)."""
    if s <= 0:
        c = int(v * 255)
        return c, c, c
    h6 = h * 6.0
    i = int(h6) % 6
    f = h6 - int(h6)
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return int(r * 255), int(g * 255), int(b * 255)


# ── Animation frame builders ────────────────────────────────────────────────

def _build_intro_frame(
    progress: float, term_w: int, term_h: int,
    subtitle: str = "Interactive Setup Wizard",
    title_banner: str = "",
) -> Group:
    """Intro splash: boom box materialises with title banner."""
    if not title_banner:
        title_banner = _TITLE_BANNER_NORMAL
    lines: list[Text] = []
    art_lines = _ART_INTRO.split("\n")
    art_h = len(art_lines)
    art_max_w = max(len(l) for l in art_lines) if art_lines else 0
    total_h = art_h + 5
    start_y = max(0, (term_h - total_h) // 2)
    t = time.time()

    # All elements center relative to the art block width
    block_w = art_max_w
    block_pad = max(0, (term_w - block_w) // 2)

    def _center_in_block(text_len: int) -> int:
        """Return left padding to center text within the art block."""
        return block_pad + max(0, (block_w - text_len) // 2)

    for row in range(term_h):
        line = Text()
        rel_row = row - start_y

        if 0 <= rel_row < art_h:
            art_line = art_lines[rel_row]
            if progress < 0.4:
                p = progress / 0.4
                result = []
                for ch in art_line:
                    if ch == " ":
                        result.append(" ")
                    elif random.random() < p * 0.8:
                        result.append(ch)
                    else:
                        result.append(random.choice("░▒▓·"))
                hue = (t * 0.3 + rel_row * 0.05) % 1.0
                r, g, b = _hsv_to_rgb_simple(hue, 0.7, 0.4 + 0.4 * p)
                line.append(" " * block_pad)
                line.append("".join(result), Style(color=_hex(r, g, b)))
            else:
                p2 = min(1.0, (progress - 0.4) / 0.3)
                hue = (t * 0.3 + rel_row * 0.05) % 1.0
                br = 0.7 + 0.3 * p2
                flicker = 1.0 + 0.02 * math.sin(t * 5 + rel_row)
                r, g, b = _hsv_to_rgb_simple(hue, 0.6, br * flicker)
                line.append(" " * block_pad)
                line.append(art_line, Style(color=_hex(r, g, b)))

        elif rel_row == art_h + 1 and progress > 0.5:
            p3 = min(1.0, (progress - 0.5) / 0.3)
            pad = _center_in_block(len(title_banner))
            chars_visible = int(len(title_banner) * p3)
            line.append(" " * pad)
            for i, ch in enumerate(title_banner):
                if i < chars_visible:
                    hue = (t * 0.2 + i * 0.03) % 1.0
                    r, g, b = _hsv_to_rgb_simple(hue, 0.8, 0.9)
                    line.append(ch, Style(color=_hex(r, g, b), bold=True))
                else:
                    line.append(" ")

        elif rel_row == art_h + 2 and progress > 0.7:
            p4 = min(1.0, (progress - 0.7) / 0.2)
            bar_w = min(block_w, int(block_w * p4))
            bar = "━" * bar_w
            pad = _center_in_block(block_w)
            hue = (t * 0.1) % 1.0
            r, g, b = _hsv_to_rgb_simple(hue, 0.5, 0.5)
            line.append(" " * pad)
            line.append(bar, Style(color=_hex(r, g, b)))

        elif rel_row == art_h + 4 and progress > 0.85 and subtitle:
            pad = _center_in_block(len(subtitle))
            p5 = min(1.0, (progress - 0.85) / 0.15)
            line.append(" " * pad)
            line.append(subtitle, Style(color=_hex(120 * p5, 120 * p5, 120 * p5)))
        else:
            line.append("")

        lines.append(line)

    return Group(*lines)


def _build_outro_frame(
    progress: float, term_w: int, term_h: int, color: str,
) -> Group:
    """Quick collapse-to-dot outro."""
    lines: list[Text] = []
    cr, cg, cb = _hex_to_rgb(color)
    center_y = term_h // 2

    if progress < 0.5:
        p = progress / 0.5
        visible_half = int((1.0 - p) * center_y)
        for row in range(term_h):
            dist = abs(row - center_y)
            line = Text()
            if dist == 0:
                br = 0.8 + 0.2 * (1.0 - p)
                c = _hex(cr * br, cg * br, cb * br)
                line.append("▓" * term_w, Style(color=c))
            elif dist <= visible_half:
                br = 0.1 * (1.0 - p)
                c = _hex(cr * br, cg * br, cb * br)
                noise = "".join(random.choice("░· ") for _ in range(term_w))
                line.append(noise, Style(color=c))
            else:
                line.append(" " * term_w)
            lines.append(line)
    else:
        p = (progress - 0.5) / 0.5
        dot_w = max(1, int((1.0 - p) * term_w))
        pad = (term_w - dot_w) // 2
        for row in range(term_h):
            line = Text()
            if row == center_y:
                br = 1.0 - p * 0.7
                c = _hex(cr * br, cg * br, cb * br)
                line.append(" " * pad)
                line.append("▓" * dot_w, Style(color=c))
            else:
                line.append(" " * term_w)
            lines.append(line)

    return Group(*lines)


# ── CRT background (standalone, matches settings menu style) ────────────────

def _build_crt_bg(term_w: int, term_h: int, color: str = "#888888") -> list[Text]:
    """Generate animated CRT scanline background."""
    t = time.time()
    cr, cg, cb = _hex_to_rgb(color)
    bg_lines: list[Text] = []
    noise_chars = "░▒▓·.╌"
    phase = t * 8
    for row in range(term_h):
        line = Text()
        scan = math.sin(phase + row * 0.6) * 0.5 + 0.5
        glow = int(scan * 18)
        r = max(0, min(255, cr // 8 + glow))
        g = max(0, min(255, cg // 8 + glow))
        b = max(0, min(255, cb // 8 + glow))
        fc = _hex(r, g, b)
        parts: list[str] = []
        for col in range(term_w):
            seed = (row * 1337 + col * 7919 + int(t * 2)) % 137
            parts.append(noise_chars[seed % len(noise_chars)] if seed < 8 else " ")
        row_str = "".join(parts)
        band_y = int((t * 3) % (term_h + 20)) - 10
        dist = abs(row - band_y)
        if dist < 3:
            flicker = max(0, 12 - dist * 4)
            fc = _hex(min(255, r + flicker * 5), min(255, g + flicker * 4), min(255, b + flicker * 4))
        line.append(row_str, Style(color=fc))
        bg_lines.append(line)
    return bg_lines


def _compose_panel(
    bg_lines: list[Text], panel_lines: list[Text],
    panel_w: int, term_w: int, term_h: int,
) -> Group:
    """Overlay a centered panel onto CRT background."""
    total_content = len(panel_lines)
    panel_x = max(0, (term_w - panel_w - 2) // 2)
    panel_y = max(0, (term_h - total_content) // 2)
    bg_a = 10
    panel_bg_style = Style(bgcolor=_hex(bg_a, bg_a, bg_a))

    result_lines: list[Text] = []
    for row in range(term_h):
        content_idx = row - panel_y
        if 0 <= content_idx < total_content:
            content_line = panel_lines[content_idx]
            line = Text()
            bg_text = bg_lines[row].plain if row < len(bg_lines) else " " * term_w
            line.append(bg_text[:panel_x], Style(color="#222222"))
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
                line.append(right_bg, Style(color="#222222"))
            result_lines.append(line)
        else:
            result_lines.append(bg_lines[row] if row < len(bg_lines) else Text(" " * term_w))
    return Group(*result_lines)


# ── Item definitions ─────────────────────────────────────────────────────────

def _get_section_items(section: str, cfg: Config) -> list[tuple[str, str, str, dict]]:
    """Return (label, config_key, type, extra) items for a setup section."""
    import socket
    hostname = socket.gethostname()

    if section == "CONNECTION":
        items: list[tuple[str, str, str, dict]] = [
            ("Client Name", "client_name", "string", {"placeholder": hostname}),
            ("Connection Mode", "mode", "choice", {
                "choices": [("listen", "LISTEN"), ("connect", "CONNECT")],
            }),
        ]
        if cfg.mode == "connect":
            items.append(("Server URL", "server_url", "string", {
                "placeholder": "ws://192.168.1.100:8097/sendspin",
            }))
        else:
            items.append(("Listen Port", "listen_port", "int", {
                "min": 1024, "max": 65535, "step": 1,
            }))
        items.append(("Continue", "", "continue", {}))
        return items

    elif section == "DISPLAY":
        items = [
            ("FPS Limit", "fps_limit", "int", {"min": 5, "max": 120, "step": 5}),
            ("Brightness", "brightness", "int", {"min": 50, "max": 150, "step": 10}),
            ("Show Artwork", "show_artwork", "bool", {}),
            ("Album Art Colours", "use_art_colors", "bool", {}),
        ]
        if not cfg.use_art_colors:
            items.append(("Static Colour", "static_color", "string", {
                "placeholder": "#ff0088",
            }))
        items.append(("Continue", "", "continue", {}))
        return items

    elif section == "PLAYBACK":
        return [
            ("Auto Play", "auto_play", "bool", {}),
            ("Auto Volume", "auto_volume", "int", {
                "min": -1, "max": 100, "step": 5,
                "format": lambda v: "OFF" if v == -1 else f"{v}%",
            }),
            ("Continue", "", "continue", {}),
        ]

    elif section == "PROTOCOL":
        items = [
            ("SendSpin Receiver", "sendspin_enabled", "bool", {}),
            ("AirPlay Receiver", "airplay_enabled", "bool", {}),
            ("Swap Prompt", "swap_prompt", "bool", {}),
        ]
        if not cfg.swap_prompt:
            items.append(("Auto Action", "swap_auto_action", "choice", {
                "choices": [("accept", "ACCEPT"), ("deny", "DENY")],
            }))
        items += [
            ("Remember Devices", "remember_airplay_devices", "bool", {}),
            ("Continue", "", "continue", {}),
        ]
        return items

    elif section == "SPOTIFY":
        items = [
            ("Spotify Connect", "spotify_enabled", "bool", {}),
        ]
        if cfg.spotify_enabled:
            items += [
                ("Bitrate", "spotify_bitrate", "choice", {
                    "choices": [(160, "160 kbps"), (320, "320 kbps")],
                }),
                ("Device Name", "spotify_device_name", "string", {
                    "placeholder": f"Musiciser@{hostname}",
                }),
                ("Remember Devices", "remember_spotify_devices", "bool", {}),
                ("Username", "spotify_username", "string", {"placeholder": "(zeroconf)"}),
                ("Web API Client ID", "spotify_client_id", "string", {"placeholder": "(optional)"}),
            ]
        items.append(("Continue", "", "continue", {}))
        return items

    elif section == "DJ MODE":
        modes: list[tuple[str, str]] = [
            ("mixed", "MIXED"), ("dual_sendspin", "DUAL SS"), ("dual_airplay", "DUAL AP"),
        ]
        if not IS_WINDOWS:
            modes += [("spotify_sendspin", "SS+SP"), ("spotify_airplay", "AP+SP"), ("dual_spotify", "DUAL SP")]
        return [
            ("DJ Source Mode", "dj_source_mode", "choice", {"choices": modes}),
            ("Open DJ on Start", "dj_default", "bool", {}),
            ("Album Art Colours", "dj_use_art_colors", "bool", {}),
            ("Continue", "", "continue", {}),
        ]

    elif section == "SUMMARY":
        return [
            ("Save & Launch", "", "action_save", {}),
            ("Cancel", "", "action_cancel", {}),
        ]

    return []


# ── Wizard ───────────────────────────────────────────────────────────────────

class SetupWizard:
    """Interactive animated setup wizard with full TUI rendering."""

    def __init__(self, existing: Config | None = None) -> None:
        self.config = existing or Config()
        self._original = Config(**{
            k: v for k, v in existing.__dict__.items()
            if k in Config.__dataclass_fields__
        }) if existing else Config()
        self._running = True
        self._cursor = 0
        self._editing = ""
        self._edit_buf = ""
        self._section_done = False
        self._result: str = ""
        self._help_key: str = ""
        self._console: Console | None = None
        self._console_size: tuple[int, int] = (0, 0)

    # ── Animations ──

    def _play_animation(self, builder, duration: float = 1.5, **kwargs) -> None:
        from rich.live import Live
        tw, th = _term_size()
        fps = 24
        try:
            with Live(
                console=Console(), refresh_per_second=fps,
                transient=True, screen=True,
            ) as live:
                start = time.monotonic()
                while True:
                    elapsed = time.monotonic() - start
                    if elapsed >= duration:
                        break
                    progress = min(1.0, elapsed / duration)
                    frame = builder(progress, tw, th, **kwargs)
                    live.update(frame)
                    time.sleep(1.0 / fps)
        except Exception:
            pass

    def _play_intro(self) -> None:
        self._play_animation(
            _build_intro_frame, duration=2.5,
            subtitle="Interactive Setup Wizard",
            title_banner=_TITLE_BANNER_SETUP,
        )

    def _play_outro(self, color: str = "#00ff88") -> None:
        self._play_animation(_build_outro_frame, duration=0.8, color=color)

    # ── Rendering ──

    def _render_to_ansi(self, group: Group, term_w: int, term_h: int) -> str:
        buf = _io.StringIO()
        if self._console is None or self._console_size != (term_w, term_h):
            self._console = Console(
                file=buf, width=term_w, height=term_h,
                force_terminal=True, color_system="truecolor", no_color=False,
            )
            self._console_size = (term_w, term_h)
        else:
            self._console._file = buf  # type: ignore[attr-defined]
        self._console.print(group)
        rendered = buf.getvalue()
        lines = rendered.split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        n = len(lines)
        if n > term_h:
            lines = lines[:term_h]
        elif n < term_h:
            lines.extend([" " * term_w] * (term_h - n))
        return "\n".join(lines)

    def _build_section_frame(
        self, section_idx: int, term_w: int, term_h: int,
    ) -> Group:
        name, art, color = _SECTION_DEFS[section_idx]
        cr, cg, cb = _hex_to_rgb(color)
        t = time.time()

        bg = _build_crt_bg(term_w, term_h, color)
        panel_w = min(56, term_w - 6)
        panel_lines: list[Text] = []

        # Section header
        header = Text()
        hc = _hex(min(255, cr + 40), min(255, cg + 40), min(255, cb + 40))
        header.append(_center(f"◈ {name} ◈", panel_w), Style(color=hc, bold=True))
        panel_lines.append(header)

        sep = Text()
        sep.append(_center("━" * (panel_w - 8), panel_w), Style(color=_dim(color, 0.4)))
        panel_lines.append(sep)
        panel_lines.append(Text(""))

        # ASCII art
        for al in art.split("\n"):
            aline = Text()
            flicker = 0.5 + 0.1 * math.sin(t * 3 + len(panel_lines) * 0.4)
            ac = _hex(cr * flicker, cg * flicker, cb * flicker)
            aline.append(_center(al, panel_w), Style(color=ac))
            panel_lines.append(aline)
        panel_lines.append(Text(""))

        # Items
        items = _get_section_items(name, self.config)
        for i, (label, key, itype, extra) in enumerate(items):
            selected = i == self._cursor
            line = Text()

            if itype == "continue":
                panel_lines.append(Text(""))
                line = Text()
                btn_text = "  [ Continue ▸ ]  "
                line.append(_center(btn_text, panel_w),
                            Style(color=color if selected else "#555555", bold=selected))
                panel_lines.append(line)
                continue

            if itype in ("action_save", "action_cancel"):
                is_save = itype == "action_save"
                btn_c = "#00ff88" if is_save else "#ff4444"
                btn_text = f"  [ {label} ]  "
                line.append(_center(btn_text, panel_w),
                            Style(color=btn_c if selected else "#555555", bold=selected))
                panel_lines.append(line)
                panel_lines.append(Text(""))
                continue

            cursor_c = color if selected else "#333333"
            line.append("  ", Style())
            line.append("▸ " if selected else "  ", Style(color=cursor_c, bold=selected))

            label_c = "#ffffff" if selected else "#888888"
            line.append(f"{label:<22}", Style(color=label_c, bold=selected))

            if key and self._editing == key:
                display = self._edit_buf
                cursor_blink = int(t * 2) % 2 == 0
                line.append(f" {display}", Style(color="#ffffff", bold=True))
                line.append("▌" if cursor_blink else " ", Style(color=color))
            elif itype == "bool":
                val = getattr(self.config, key, False)
                if val:
                    line.append("    ON", Style(color="#44ff44" if selected else "#338833", bold=selected))
                else:
                    line.append("   OFF", Style(color="#ff4444" if selected else "#883333", bold=selected))
                if selected:
                    line.append("  ◂▸", Style(color="#555555"))
            elif itype == "int":
                val = getattr(self.config, key, 0)
                fmt = extra.get("format")
                val_str = fmt(val) if fmt else str(val)
                line.append(f"{val_str:>6}", Style(
                    color=color if selected else "#666666", bold=selected))
                if selected:
                    line.append("  ◂▸", Style(color="#555555"))
            elif itype == "choice":
                val = getattr(self.config, key, "")
                choices = extra.get("choices", [])
                display = str(val).upper()
                for cv, cl in choices:
                    if cv == val:
                        display = cl
                        break
                line.append(f"{display:>12}", Style(
                    color=color if selected else "#666666", bold=selected))
                if selected:
                    line.append("  ◂▸", Style(color="#555555"))
            elif itype == "string":
                val = getattr(self.config, key, "")
                placeholder = extra.get("placeholder", "")
                display = val or placeholder
                if len(display) > 20:
                    display = display[:17] + "..."
                display_c = (color if selected else "#666666") if val else "#444444"
                line.append(f" {display}", Style(color=display_c, italic=not val))
                if selected:
                    line.append("  Enter▸", Style(color="#555555"))

            panel_lines.append(line)

        while len(panel_lines) < 22:
            panel_lines.append(Text(""))

        # Section progress indicator
        sections = self._get_section_list()
        total = len(sections)
        try:
            current = sections.index(section_idx) + 1
        except ValueError:
            current = 0
        progress_line = Text()
        progress_line.append(_center(f"Step {current} of {total}", panel_w),
                             Style(color=_dim(color, 0.4)))
        panel_lines.append(progress_line)

        # Key hints
        hints = Text()
        hint_c = _dim(color, 0.5)
        hints.append(_center("↑↓ Nav  ←→ Adjust  Enter Edit  ? Help  Esc Skip", panel_w),
                      Style(color=hint_c))
        panel_lines.append(hints)

        return _compose_panel(bg, panel_lines, panel_w, term_w, term_h)

    def _build_summary_frame(self, term_w: int, term_h: int) -> Group:
        name, art, color = _SECTION_DEFS[-1]
        bg = _build_crt_bg(term_w, term_h, color)
        panel_w = min(56, term_w - 6)
        panel_lines: list[Text] = []

        header = Text()
        header.append(_center("◈ SETUP SUMMARY ◈", panel_w), Style(color=color, bold=True))
        panel_lines.append(header)
        sep = Text()
        sep.append(_center("━" * (panel_w - 8), panel_w), Style(color=_dim(color, 0.4)))
        panel_lines.append(sep)
        panel_lines.append(Text(""))

        fields = [
            ("Client Name", "client_name"),
            ("Mode", "mode"),
            ("Server URL", "server_url"),
            ("Listen Port", "listen_port"),
            ("FPS", "fps_limit"),
            ("Brightness", "brightness"),
            ("Artwork", "show_artwork"),
            ("Art Colours", "use_art_colors"),
            ("Auto Play", "auto_play"),
            ("Auto Volume", "auto_volume"),
            ("SendSpin", "sendspin_enabled"),
            ("AirPlay", "airplay_enabled"),
            ("Spotify", "spotify_enabled"),
            ("Spotify Bitrate", "spotify_bitrate"),
            ("DJ Mode", "dj_source_mode"),
            ("DJ on Start", "dj_default"),
            ("DJ Art Colours", "dj_use_art_colors"),
        ]
        for label, field in fields:
            val = getattr(self.config, field)
            old = getattr(self._original, field)
            if field == "server_url" and self.config.mode != "connect":
                continue
            if field == "listen_port" and self.config.mode != "listen":
                continue
            if field in ("spotify_enabled", "spotify_bitrate") and IS_WINDOWS:
                continue
            changed = val != old

            if isinstance(val, bool):
                val_str = "ON" if val else "OFF"
            elif field == "auto_volume":
                val_str = "OFF" if val == -1 else f"{val}%"
            elif field == "brightness":
                val_str = f"{val}%"
            else:
                val_str = str(val) if val else "(default)"

            line = Text()
            line.append(f"  {label:<20}", Style(color="#aaaaaa"))
            vc = "#44ff88" if changed else "#666666"
            line.append(f"{val_str:<16}", Style(color=vc, bold=changed))
            if changed:
                line.append("◂", Style(color="#44ff88"))
            panel_lines.append(line)

        while len(panel_lines) < 22:
            panel_lines.append(Text(""))

        items = _get_section_items("SUMMARY", self.config)
        for i, (label, _, itype, _) in enumerate(items):
            selected = i == self._cursor
            line = Text()
            is_save = itype == "action_save"
            btn_c = "#00ff88" if is_save else "#ff4444"
            btn_text = f"  [ {label} ]  "
            line.append(_center(btn_text, panel_w),
                        Style(color=btn_c if selected else "#555555", bold=selected))
            panel_lines.append(line)

        panel_lines.append(Text(""))
        hints = Text()
        hints.append(_center("↑↓ Select  Enter Confirm", panel_w), Style(color=_dim(color, 0.5)))
        panel_lines.append(hints)

        return _compose_panel(bg, panel_lines, panel_w, term_w, term_h)

    # ── Help dialog ──

    def _build_help_frame(self, section_idx: int, term_w: int, term_h: int) -> Group:
        """Render a help dialog overlay for the selected setting."""
        _, _, color = _SECTION_DEFS[section_idx]
        bg = _build_crt_bg(term_w, term_h, color)
        panel_w = min(48, term_w - 6)

        help_text = _HELP_TEXT.get(self._help_key, "No help available for this setting.")
        # Find label
        name = _SECTION_DEFS[section_idx][0]
        items = _get_section_items(name, self.config)
        label = self._help_key
        for lbl, key, _, _ in items:
            if key == self._help_key:
                label = lbl
                break

        panel_lines: list[Text] = []

        sep = Text()
        sep.append(_center("━" * (panel_w - 4), panel_w), Style(color=color, bold=True))
        panel_lines.append(sep)

        header = Text()
        header.append(_center(f" ? {label} ? ", panel_w), Style(color=color, bold=True))
        panel_lines.append(header)

        sep2 = Text()
        sep2.append(_center("━" * (panel_w - 4), panel_w), Style(color=color, bold=True))
        panel_lines.append(sep2)
        panel_lines.append(Text(""))

        for line_str in help_text.split("\n"):
            line = Text()
            line.append(f"  {line_str}", Style(color="#cccccc"))
            panel_lines.append(line)

        panel_lines.append(Text(""))
        panel_lines.append(Text(""))

        footer_sep = Text()
        footer_sep.append(_center("━" * (panel_w - 4), panel_w), Style(color=_dim(color, 0.4)))
        panel_lines.append(footer_sep)

        hint = Text()
        hint.append(_center("Press any key to close", panel_w), Style(color="#555555"))
        panel_lines.append(hint)

        return _compose_panel(bg, panel_lines, panel_w, term_w, term_h)

    # ── Key handling ──

    def _handle_key(self, k: str, section: str) -> None:
        items = _get_section_items(section, self.config)
        n_items = len(items)

        # Help dialog — any key closes it
        if self._help_key:
            self._help_key = ""
            return

        if self._editing:
            if k in ("\r", "\n"):
                key = self._editing
                val = self._edit_buf
                # Type coercion for int fields
                field_type = type(getattr(self.config, key, ""))
                if field_type == int:
                    try:
                        val = int(val)
                    except ValueError:
                        val = getattr(self.config, key)
                setattr(self.config, key, val)
                self._editing = ""
                self._edit_buf = ""
            elif k in ("\x1b", "escape"):
                self._editing = ""
                self._edit_buf = ""
            elif k in ("\x7f", "backspace"):
                self._edit_buf = self._edit_buf[:-1]
            elif len(k) == 1 and k.isprintable():
                self._edit_buf += k
            return

        if k == "arrow_up":
            self._cursor = (self._cursor - 1) % n_items
        elif k == "arrow_down":
            self._cursor = (self._cursor + 1) % n_items
        elif k in ("\x1b", "escape"):
            for i, (_, _, itype, _) in enumerate(items):
                if itype == "continue":
                    self._cursor = i
                    break

        elif k == "?" and self._cursor < n_items:
            config_key = items[self._cursor][1]
            if config_key and config_key in _HELP_TEXT:
                self._help_key = config_key

        elif k in ("\r", "\n", " "):
            if self._cursor >= n_items:
                return
            label, key, itype, extra = items[self._cursor]

            if itype == "continue":
                self._section_done = True
            elif itype == "action_save":
                self._result = "save"
                self._section_done = True
            elif itype == "action_cancel":
                self._result = "cancel"
                self._section_done = True
            elif itype == "bool":
                val = getattr(self.config, key, False)
                setattr(self.config, key, not val)
            elif itype == "string":
                self._editing = key
                self._edit_buf = str(getattr(self.config, key, ""))
            elif itype == "choice":
                self._cycle_choice(key, extra, 1)
            elif itype == "int":
                self._editing = key
                self._edit_buf = str(getattr(self.config, key, 0))

        elif k in ("arrow_left", "arrow_right"):
            if self._cursor >= n_items:
                return
            _, key, itype, extra = items[self._cursor]
            direction = 1 if k == "arrow_right" else -1

            if itype == "bool":
                val = getattr(self.config, key, False)
                setattr(self.config, key, not val)
            elif itype == "int":
                val = getattr(self.config, key, 0)
                step = extra.get("step", 1)
                mn, mx = extra.get("min", 0), extra.get("max", 100)
                setattr(self.config, key, max(mn, min(mx, val + step * direction)))
            elif itype == "choice":
                self._cycle_choice(key, extra, direction)

    def _cycle_choice(self, key: str, extra: dict, direction: int) -> None:
        val = getattr(self.config, key, "")
        choices = extra.get("choices", [])
        vals = [cv for cv, _ in choices]
        try:
            idx = vals.index(val)
        except ValueError:
            idx = 0
        setattr(self.config, key, vals[(idx + direction) % len(vals)])

    # ── Input parsing ──

    def _parse_input(self, data: bytes, section: str) -> None:
        i = 0
        while i < len(data):
            if data[i:i + 3] == b"\x1b[A":
                self._handle_key("arrow_up", section)
                i += 3
            elif data[i:i + 3] == b"\x1b[B":
                self._handle_key("arrow_down", section)
                i += 3
            elif data[i:i + 3] == b"\x1b[C":
                self._handle_key("arrow_right", section)
                i += 3
            elif data[i:i + 3] == b"\x1b[D":
                self._handle_key("arrow_left", section)
                i += 3
            elif data[i:i + 1] == b"\x1b":
                rest = data[i + 1:]
                if not rest or rest[0:1] not in (b"[", b"O"):
                    self._handle_key("escape", section)
                    i += 1
                else:
                    i += 1
                    while i < len(data) and not data[i:i + 1].isalpha() and data[i:i + 1] != b"~":
                        i += 1
                    i += 1
            elif data[i:i + 1] == b"\x03":
                self._result = "cancel"
                self._section_done = True
                self._running = False
                i += 1
            elif data[i:i + 1] == b"\x7f":
                self._handle_key("backspace", section)
                i += 1
            elif data[i:i + 1] in (b"\r", b"\n"):
                self._handle_key("\r", section)
                i += 1
            elif data[i:i + 1] == b"\t":
                self._handle_key("arrow_down", section)
                i += 1
            else:
                ch = data[i:i + 1].decode("ascii", errors="ignore")
                if ch:
                    self._handle_key(ch, section)
                i += 1

    # ── Section TUI loop ──

    def _get_section_list(self) -> list[int]:
        sections = [0, 1, 2, 3]  # CONNECTION, DISPLAY, PLAYBACK, PROTOCOL
        if not IS_WINDOWS:
            sections.append(4)  # SPOTIFY
        sections.append(5)  # DJ MODE
        return sections

    def _run_section(self, section_idx: int) -> None:
        name = _SECTION_DEFS[section_idx][0]
        self._cursor = 0
        self._editing = ""
        self._section_done = False
        is_summary = name == "SUMMARY"

        if IS_WINDOWS:
            self._run_section_windows(section_idx, name, is_summary)
        else:
            self._run_section_unix(section_idx, name, is_summary)

    def _run_section_unix(self, section_idx: int, name: str, is_summary: bool) -> None:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()

            while not self._section_done and self._running:
                tw, th = _term_size()
                if self._help_key:
                    frame = self._build_help_frame(section_idx, tw, th)
                elif is_summary:
                    frame = self._build_summary_frame(tw, th)
                else:
                    frame = self._build_section_frame(section_idx, tw, th)
                rendered = self._render_to_ansi(frame, tw, th)
                sys.stdout.write(f"\x1b[H{rendered}")
                sys.stdout.flush()

                ready = select.select([fd], [], [], 1.0 / 24)
                if ready[0]:
                    data = os.read(fd, 64)
                    if data:
                        self._parse_input(data, name)
        finally:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _run_section_windows(self, section_idx: int, name: str, is_summary: bool) -> None:
        import msvcrt
        _ARROW_MAP = {b"H": "arrow_up", b"P": "arrow_down", b"M": "arrow_right", b"K": "arrow_left"}

        os.system("cls")
        try:
            while not self._section_done and self._running:
                tw, th = _term_size()
                if self._help_key:
                    frame = self._build_help_frame(section_idx, tw, th)
                elif is_summary:
                    frame = self._build_summary_frame(tw, th)
                else:
                    frame = self._build_section_frame(section_idx, tw, th)
                rendered = self._render_to_ansi(frame, tw, th)
                sys.stdout.write(f"\x1b[H{rendered}")
                sys.stdout.flush()

                if msvcrt.kbhit():
                    data = msvcrt.getch()
                    if data in (b"\xe0", b"\x00"):
                        data2 = msvcrt.getch()
                        arrow = _ARROW_MAP.get(data2)
                        if arrow:
                            self._handle_key(arrow, name)
                    elif data == b"\x03":
                        self._result = "cancel"
                        self._section_done = True
                        self._running = False
                    elif data == b"\r":
                        self._handle_key("\r", name)
                    elif data == b"\x1b":
                        self._handle_key("escape", name)
                    elif data == b"\x08":
                        self._handle_key("backspace", name)
                    elif data == b"\t":
                        self._handle_key("arrow_down", name)
                    else:
                        ch = data.decode("ascii", errors="ignore")
                        if ch:
                            self._handle_key(ch, name)
                else:
                    time.sleep(1.0 / 24)
        finally:
            os.system("cls")

    # ── Main flow ──

    def run(self) -> Config:
        """Run the full setup wizard. Returns the (possibly modified) Config."""
        sections = self._get_section_list()

        self._play_intro()

        for idx in sections:
            self._run_section(idx)
            if not self._running:
                break

        if self._running:
            self._cursor = 0
            self._section_done = False
            summary_idx = len(_SECTION_DEFS) - 1
            self._run_section(summary_idx)

        if self._result == "save":
            self.config.save()
            self._play_outro("#00ff88")
            return self.config
        else:
            self._play_outro("#ff4444")
            return self._original


# ── Public API ───────────────────────────────────────────────────────────────

def play_intro_animation() -> None:
    """Play the intro splash animation with a random quote as the title.

    Uses manual alternate-screen management so the main terminal stays
    clear when the animation ends — preventing a flash of prior output
    before the TUI enters its own alternate screen.
    """
    from alfieprime_musiciser.tui_animations import _STANDBY_PHRASES
    from alfieprime_musiciser import __version__
    quote = random.choice(_STANDBY_PHRASES)

    tw, th = _term_size()
    fps = 24
    duration = 2.5

    # Render helper (same pattern as SetupWizard._render_to_ansi)
    _console: Console | None = None

    def _render(group: Group) -> str:
        nonlocal _console
        buf = _io.StringIO()
        if _console is None:
            _console = Console(
                file=buf, width=tw, height=th,
                force_terminal=True, color_system="truecolor", no_color=False,
            )
        else:
            _console._file = buf  # type: ignore[attr-defined]
        _console.print(group)
        rendered = buf.getvalue()
        lines = rendered.split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        n = len(lines)
        if n > th:
            lines = lines[:th]
        elif n < th:
            lines.extend([" " * tw] * (th - n))
        return "\n".join(lines)

    try:
        # Enter alternate screen + hide cursor
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()

        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                break
            progress = min(1.0, elapsed / duration)
            frame = _build_intro_frame(
                progress, tw, th,
                subtitle=f"v{__version__}",
                title_banner=quote,
            )
            rendered = _render(frame)
            sys.stdout.write(f"\x1b[H{rendered}")
            sys.stdout.flush()
            time.sleep(1.0 / fps)

        # Stay in alternate screen — the TUI will take over seamlessly.
        # Just clear it and show cursor briefly.
        sys.stdout.write("\x1b[2J\x1b[H\x1b[?25h")
        sys.stdout.flush()
    except Exception:
        # Restore terminal state on any error
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


def run_setup_wizard(console: Console | None = None, existing: Config | None = None) -> Config:
    """Run the animated setup wizard. Returns the final Config."""
    wizard = SetupWizard(existing)
    return wizard.run()
