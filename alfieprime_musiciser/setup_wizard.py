"""Animated interactive setup wizard for AlfiePRIME Musiciser.

Walks the user through every configurable setting grouped into logical
sections, with CRT-style transitions and ASCII art between each section.
"""
from __future__ import annotations

import math
import random
import shutil
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.style import Style
from rich.table import Table
from rich.text import Text

from alfieprime_musiciser.config import Config

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

_SECTION_DEFS = [
    ("CONNECTION",  _ART_CONNECTION, "#00ccff"),
    ("DISPLAY",     _ART_DISPLAY,   "#ff88ff"),
    ("PLAYBACK",    _ART_PLAYBACK,  "#88ff44"),
    ("PROTOCOL",    _ART_PROTOCOL,  "#ffaa00"),
    ("SPOTIFY",     _ART_SPOTIFY,   "#1db954"),
    ("DJ MODE",     _ART_DJ,        "#ff4488"),
]

_TITLE_BANNER = " A L F I E P R I M E   S E T U P "

# ── Helpers ──────────────────────────────────────────────────────────────────

def _hex(r: int | float, g: int | float, b: int | float) -> str:
    return f"#{max(0,min(255,int(r))):02x}{max(0,min(255,int(g))):02x}{max(0,min(255,int(b))):02x}"


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _dim(color: str, factor: float) -> str:
    r, g, b = _hex_to_rgb(color)
    return _hex(r * factor, g * factor, b * factor)


def _term_w() -> int:
    return shutil.get_terminal_size((80, 24)).columns


# ── Animation frames ─────────────────────────────────────────────────────────

def _build_scanline_frame(
    progress: float, term_w: int, term_h: int, color: str,
) -> Group:
    """CRT scanline wipe animation frame."""
    lines: list[Text] = []
    cr, cg, cb = _hex_to_rgb(color)

    center_y = term_h // 2
    # Phase 1 (0-0.4): scanline appears and glows
    # Phase 2 (0.4-1.0): content expands from scanline
    if progress < 0.4:
        p = progress / 0.4
        for row in range(term_h):
            line = Text()
            dist = abs(row - center_y)
            if dist == 0:
                brightness = 0.5 + 0.5 * p
                flicker = 1.0 + 0.1 * math.sin(time.time() * 30 + row)
                br = brightness * flicker
                c = _hex(cr * br, cg * br, cb * br)
                line.append("▓" * term_w, Style(color=c))
            elif dist <= 1 and p > 0.5:
                glow = (p - 0.5) * 2.0 * 0.3
                c = _hex(cr * glow, cg * glow, cb * glow)
                noise = "".join(random.choice("░·  ") for _ in range(term_w))
                line.append(noise, Style(color=c))
            else:
                line.append(" " * term_w)
            lines.append(line)
    else:
        p = (progress - 0.4) / 0.6
        visible_half = int(p * center_y)
        for row in range(term_h):
            line = Text()
            dist = abs(row - center_y)
            if dist <= visible_half:
                edge_fade = 1.0 - (dist / max(1, visible_half)) * 0.5
                flicker = 0.9 + 0.2 * math.sin(time.time() * 8 + row * 0.3)
                br = edge_fade * flicker * p
                c = _hex(cr * br * 0.15, cg * br * 0.15, cb * br * 0.15)
                noise = "".join(
                    random.choice("░▒ · " if random.random() < 0.08 else "   ")
                    for _ in range(term_w)
                )
                line.append(noise, Style(color=c))
            elif dist == visible_half + 1:
                br = 0.3 * (1.0 - p)
                c = _hex(cr * br, cg * br, cb * br)
                line.append("─" * term_w, Style(color=c))
            else:
                line.append(" " * term_w)
            lines.append(line)
    return Group(*lines)


def _build_section_intro_frame(
    progress: float, term_w: int, term_h: int,
    section_name: str, ascii_art: str, color: str,
) -> Group:
    """Section intro animation: ASCII art materialises with title."""
    lines: list[Text] = []
    art_lines = ascii_art.split("\n")
    art_h = len(art_lines)
    art_max_w = max(len(l) for l in art_lines) if art_lines else 0

    total_h = art_h + 4  # art + gap + title + underline
    start_y = max(0, (term_h - total_h) // 2)
    cr, cg, cb = _hex_to_rgb(color)

    # Phase 1 (0-0.3): static noise reveals
    # Phase 2 (0.3-0.7): art characters fade in
    # Phase 3 (0.7-1.0): title appears with glow

    for row in range(term_h):
        line = Text()
        rel_row = row - start_y

        if 0 <= rel_row < art_h:
            art_line = art_lines[rel_row]
            pad = max(0, (term_w - art_max_w) // 2)

            if progress < 0.3:
                # Noise with occasional revealed chars
                p = progress / 0.3
                result = []
                for i, ch in enumerate(art_line):
                    reveal = random.random() < p * 0.7
                    if ch == " ":
                        result.append(" ")
                    elif reveal:
                        result.append(ch)
                    else:
                        result.append(random.choice("░▒▓·"))
                txt = "".join(result)
                br = 0.3 + 0.4 * p
                c = _hex(cr * br, cg * br, cb * br)
                line.append(" " * pad)
                line.append(txt, Style(color=c))
            else:
                # Fully revealed with glow
                p2 = min(1.0, (progress - 0.3) / 0.4)
                glow = 0.7 + 0.3 * p2
                flicker = 1.0 + 0.03 * math.sin(time.time() * 6 + rel_row * 0.5)
                c = _hex(cr * glow * flicker, cg * glow * flicker, cb * glow * flicker)
                line.append(" " * pad)
                line.append(art_line, Style(color=c))

        elif rel_row == art_h + 1 and progress > 0.5:
            # Section title
            p3 = min(1.0, (progress - 0.5) / 0.3)
            title = f" ◈ {section_name} ◈ "
            pad = max(0, (term_w - len(title)) // 2)
            glow = 0.5 + 0.5 * p3
            flicker = 1.0 + 0.05 * math.sin(time.time() * 4)
            br = glow * flicker
            c = _hex(cr * br, cg * br, cb * br)
            line.append(" " * pad)
            line.append(title, Style(color=c, bold=True))

        elif rel_row == art_h + 2 and progress > 0.6:
            # Underline
            p4 = min(1.0, (progress - 0.6) / 0.3)
            bar_w = int(40 * p4)
            bar = "━" * bar_w
            pad = max(0, (term_w - 40) // 2)
            c = _dim(color, 0.5)
            line.append(" " * pad)
            line.append(bar, Style(color=c))
        else:
            line.append("")

        lines.append(line)

    return Group(*lines)


def _build_intro_frame(
    progress: float, term_w: int, term_h: int,
) -> Group:
    """Intro splash: boom box materialises with title banner."""
    lines: list[Text] = []
    art_lines = _ART_INTRO.split("\n")
    art_h = len(art_lines)
    art_max_w = max(len(l) for l in art_lines) if art_lines else 0

    total_h = art_h + 5
    start_y = max(0, (term_h - total_h) // 2)

    # Colors cycle through rainbow
    t = time.time()

    for row in range(term_h):
        line = Text()
        rel_row = row - start_y

        if 0 <= rel_row < art_h:
            art_line = art_lines[rel_row]
            pad = max(0, (term_w - art_max_w) // 2)

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
                line.append(" " * pad)
                line.append("".join(result), Style(color=_hex(r, g, b)))
            else:
                p2 = min(1.0, (progress - 0.4) / 0.3)
                hue = (t * 0.3 + rel_row * 0.05) % 1.0
                br = 0.7 + 0.3 * p2
                flicker = 1.0 + 0.02 * math.sin(t * 5 + rel_row)
                r, g, b = _hsv_to_rgb_simple(hue, 0.6, br * flicker)
                line.append(" " * pad)
                line.append(art_line, Style(color=_hex(r, g, b)))

        elif rel_row == art_h + 1 and progress > 0.5:
            # Title banner
            p3 = min(1.0, (progress - 0.5) / 0.3)
            pad = max(0, (term_w - len(_TITLE_BANNER)) // 2)
            chars_visible = int(len(_TITLE_BANNER) * p3)
            line.append(" " * pad)
            for i, ch in enumerate(_TITLE_BANNER):
                if i < chars_visible:
                    hue = (t * 0.2 + i * 0.03) % 1.0
                    r, g, b = _hsv_to_rgb_simple(hue, 0.8, 0.9)
                    line.append(ch, Style(color=_hex(r, g, b), bold=True))
                else:
                    line.append(" ")

        elif rel_row == art_h + 2 and progress > 0.7:
            p4 = min(1.0, (progress - 0.7) / 0.2)
            bar_w = int(40 * p4)
            bar = "━" * bar_w
            pad = max(0, (term_w - 40) // 2)
            hue = (t * 0.1) % 1.0
            r, g, b = _hsv_to_rgb_simple(hue, 0.5, 0.5)
            line.append(" " * pad)
            line.append(bar, Style(color=_hex(r, g, b)))

        elif rel_row == art_h + 4 and progress > 0.85:
            sub = "Interactive Setup Wizard"
            pad = max(0, (term_w - len(sub)) // 2)
            p5 = min(1.0, (progress - 0.85) / 0.15)
            line.append(" " * pad)
            line.append(sub, Style(color=_hex(120 * p5, 120 * p5, 120 * p5)))
        else:
            line.append("")

        lines.append(line)

    return Group(*lines)


def _hsv_to_rgb_simple(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Simple HSV→RGB (0-1 inputs, 0-255 outputs)."""
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


def _build_outro_frame(
    progress: float, term_w: int, term_h: int, color: str,
) -> Group:
    """Quick collapse-to-dot outro."""
    lines: list[Text] = []
    cr, cg, cb = _hex_to_rgb(color)
    center_y = term_h // 2

    if progress < 0.5:
        # Collapse to scanline
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
        # Scanline shrinks to dot
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


# ── Wizard ───────────────────────────────────────────────────────────────────

class SetupWizard:
    """Interactive animated setup wizard."""

    def __init__(self, console: Console, existing: Config | None = None) -> None:
        self.console = console
        self.config = existing or Config()
        # Track which fields changed for summary
        self._original = Config(**{
            k: v for k, v in existing.__dict__.items()
            if k in Config.__dataclass_fields__
        }) if existing else Config()
        self._skipped_sections: set[str] = set()

    # ── Animations ──

    def _play_animation(
        self, builder, duration: float = 1.5, **kwargs,
    ) -> None:
        """Play a timed animation using Rich Live."""
        tw = _term_w()
        th = shutil.get_terminal_size((80, 24)).lines
        fps = 24
        try:
            with Live(
                console=self.console,
                refresh_per_second=fps,
                transient=True,
                screen=True,
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
            pass  # Graceful fallback if terminal doesn't support Live

    def _play_intro(self) -> None:
        self._play_animation(_build_intro_frame, duration=2.5)

    def _play_section_intro(self, idx: int) -> None:
        name, art, color = _SECTION_DEFS[idx]
        self._play_animation(
            _build_section_intro_frame,
            duration=1.5,
            section_name=name,
            ascii_art=art,
            color=color,
        )

    def _play_outro(self, color: str = "#00ff88") -> None:
        self._play_animation(_build_outro_frame, duration=0.8, color=color)

    # ── Input helpers ──

    def _section_color(self, idx: int) -> str:
        return _SECTION_DEFS[idx][2]

    def _print_setting(
        self,
        label: str,
        description: str,
        current: str,
        color: str,
        hint: str = "",
    ) -> None:
        """Print a styled setting box."""
        self.console.print()
        inner = Text()
        inner.append(f"  {label}\n", Style(color=color, bold=True))
        inner.append(f"  {description}\n", Style(color="#888888"))
        inner.append(f"\n  Current: ", Style(color="#666666"))
        inner.append(f"{current}", Style(color=color, bold=True))
        if hint:
            inner.append(f"\n  {hint}", Style(color="#555555"))
        self.console.print(Panel(
            inner,
            border_style=Style(color=_dim(color, 0.5)),
            width=min(62, _term_w() - 4),
        ))

    def _ask_string(
        self, label: str, desc: str, current: str, color: str,
    ) -> str:
        self._print_setting(label, desc, current or "(empty)", color)
        return Prompt.ask(
            f"  [{_dim(color, 0.7)}]▸[/] Enter value",
            default=current,
            console=self.console,
        )

    def _ask_bool(
        self, label: str, desc: str, current: bool, color: str,
    ) -> bool:
        cur_str = "[bold green]ON[/]" if current else "[dim]OFF[/]"
        self._print_setting(label, desc, "ON" if current else "OFF", color)
        return Confirm.ask(
            f"  [{_dim(color, 0.7)}]▸[/] Enable?",
            default=current,
            console=self.console,
        )

    def _ask_int(
        self, label: str, desc: str, current: int, color: str,
        min_val: int, max_val: int,
    ) -> int:
        hint = f"Range: {min_val}–{max_val}"
        self._print_setting(label, desc, str(current), color, hint=hint)
        while True:
            raw = Prompt.ask(
                f"  [{_dim(color, 0.7)}]▸[/] Enter value",
                default=str(current),
                console=self.console,
            )
            try:
                val = int(raw)
                if min_val <= val <= max_val:
                    return val
                self.console.print(
                    f"  [red]Must be between {min_val} and {max_val}[/]"
                )
            except ValueError:
                self.console.print("  [red]Please enter a number[/]")

    def _ask_choice(
        self, label: str, desc: str, choices: list[tuple[str, str]],
        current: str, color: str,
    ) -> str:
        """choices = [(value, display_label), ...]"""
        choice_desc = "  ".join(
            f"[bold {color}]{v}[/] = {lbl}" if v == current
            else f"[dim]{v}[/] = {lbl}"
            for v, lbl in choices
        )
        self._print_setting(label, desc, current, color, hint=choice_desc)
        valid = [v for v, _ in choices]
        return Prompt.ask(
            f"  [{_dim(color, 0.7)}]▸[/] Choose",
            choices=valid,
            default=current,
            console=self.console,
        )

    def _ask_hex_color(
        self, label: str, desc: str, current: str, color: str,
    ) -> str:
        presets = [
            ("Red", "#ff0000"), ("Orange", "#ff8800"), ("Yellow", "#ffff00"),
            ("Lime", "#88ff00"), ("Green", "#00ff00"), ("Teal", "#00ff88"),
            ("Cyan", "#00ffff"), ("Sky", "#0088ff"), ("Blue", "#0000ff"),
            ("Purple", "#8800ff"), ("Magenta", "#ff00ff"), ("Pink", "#ff0088"),
            ("White", "#ffffff"),
        ]
        self._print_setting(label, desc, current or "(disabled)", color)
        # Show colour swatches
        swatch = Text("  ")
        for name, hex_c in presets:
            swatch.append("██", Style(color=hex_c))
            swatch.append(f" {name}  ", Style(color="#666666"))
            if len(swatch.plain) > _term_w() - 10:
                self.console.print(swatch)
                swatch = Text("  ")
        if swatch.plain.strip():
            self.console.print(swatch)
        self.console.print()
        raw = Prompt.ask(
            f"  [{_dim(color, 0.7)}]▸[/] Enter hex colour (e.g. #ff0000) or 'none' to disable",
            default=current or "none",
            console=self.console,
        )
        if raw.lower() in ("none", "off", "disable", ""):
            return ""
        if not raw.startswith("#"):
            raw = "#" + raw
        if len(raw) == 7:
            return raw
        self.console.print("  [red]Invalid hex colour, keeping current value[/]")
        return current

    def _ask_auto_volume(
        self, label: str, desc: str, current: int, color: str,
    ) -> int:
        hint = "-1 = disabled, 0–100 = set volume on connect"
        cur_str = "Disabled" if current == -1 else f"{current}%"
        self._print_setting(label, desc, cur_str, color, hint=hint)
        raw = Prompt.ask(
            f"  [{_dim(color, 0.7)}]▸[/] Enter volume (0-100) or -1 to disable",
            default=str(current),
            console=self.console,
        )
        try:
            val = int(raw)
            if val == -1 or 0 <= val <= 100:
                return val
            self.console.print("  [red]Must be -1 or 0–100[/]")
            return current
        except ValueError:
            self.console.print("  [red]Please enter a number[/]")
            return current

    def _section_skip_prompt(self, section_name: str, color: str) -> bool:
        """Ask if the user wants to configure this section or skip it."""
        self.console.print()
        result = Confirm.ask(
            f"  [{color}]▸[/] Configure [bold]{section_name}[/] settings?",
            default=True,
            console=self.console,
        )
        if not result:
            self.console.print(f"  [dim]Skipping {section_name} (keeping defaults)[/]")
        return not result

    # ── Section runners ──

    def _run_connection(self) -> None:
        c = self._section_color(0)

        self.config.client_name = self._ask_string(
            "Client Name",
            "How this player appears in Music Assistant / AirPlay.",
            self.config.client_name, c,
        )

        self.config.mode = self._ask_choice(
            "Connection Mode",
            "How to find and connect to the music server.",
            [("listen", "mDNS auto-discovery (recommended)"),
             ("connect", "Connect to a specific server URL")],
            self.config.mode, c,
        )

        if self.config.mode == "connect":
            url = self._ask_string(
                "Server URL",
                "WebSocket URL of the SendSpin/Music Assistant server.\n"
                "  Example: ws://192.168.1.100:8097/sendspin",
                self.config.server_url, c,
            )
            # Auto-prefix ws://
            if url and not url.startswith(("ws://", "wss://")):
                if ":" in url and "/" in url:
                    url = "ws://" + url
                else:
                    url = f"ws://{url}:8097/sendspin"
                self.console.print(f"  [dim]Using URL: {url}[/]")
            self.config.server_url = url
        else:
            self.config.listen_port = self._ask_int(
                "Listen Port",
                "TCP port to listen on for incoming connections.",
                self.config.listen_port, c,
                min_val=1024, max_val=65535,
            )

    def _run_display(self) -> None:
        c = self._section_color(1)

        self.config.fps_limit = self._ask_int(
            "FPS Limit",
            "Terminal rendering frame rate. Higher = smoother but uses more CPU.",
            self.config.fps_limit, c,
            min_val=5, max_val=120,
        )

        self.config.brightness = self._ask_int(
            "Brightness",
            "Terminal brightness percentage. Adjust if colours look washed out or too dim.",
            self.config.brightness, c,
            min_val=50, max_val=150,
        )

        self.config.show_artwork = self._ask_bool(
            "Show Artwork",
            "Display album art as braille art on the main screen.",
            self.config.show_artwork, c,
        )

        self.config.use_art_colors = self._ask_bool(
            "Album Art Colours",
            "Dynamically theme the UI from album artwork colours.",
            self.config.use_art_colors, c,
        )

        if not self.config.use_art_colors:
            self.config.static_color = self._ask_hex_color(
                "Static Colour",
                "Fixed UI colour when album art colours are disabled.",
                self.config.static_color, c,
            )

    def _run_playback(self) -> None:
        c = self._section_color(2)

        self.config.auto_play = self._ask_bool(
            "Auto Play on Connect",
            "Automatically start playback when a music server connects.",
            self.config.auto_play, c,
        )

        self.config.auto_volume = self._ask_auto_volume(
            "Auto Volume on Connect",
            "Automatically set volume when a server connects.",
            self.config.auto_volume, c,
        )

    def _run_protocol(self) -> None:
        c = self._section_color(3)

        self.config.sendspin_enabled = self._ask_bool(
            "SendSpin Receiver",
            "Enable the SendSpin/Music Assistant protocol receiver.",
            self.config.sendspin_enabled, c,
        )

        self.config.airplay_enabled = self._ask_bool(
            "AirPlay Receiver",
            "Enable the AirPlay 2 protocol receiver.",
            self.config.airplay_enabled, c,
        )

        self.config.swap_prompt = self._ask_bool(
            "Device Swap Prompt",
            "Show a Y/N prompt when a second device tries to connect.",
            self.config.swap_prompt, c,
        )

        if not self.config.swap_prompt:
            self.config.swap_auto_action = self._ask_choice(
                "Auto Swap Action",
                "What to do automatically when a new device connects\n"
                "  and the swap prompt is disabled.",
                [("accept", "Accept new device"),
                 ("deny", "Deny new device")],
                self.config.swap_auto_action, c,
            )

        self.config.forget_airplay_devices = self._ask_bool(
            "Forget AirPlay Devices on Exit",
            "Clear AirPlay pairing data on close so devices must re-pair.\n"
            "  Useful if you share this machine with others.",
            self.config.forget_airplay_devices, c,
        )

    def _run_spotify(self) -> None:
        c = self._section_color(4)

        self.config.spotify_enabled = self._ask_bool(
            "Spotify Connect",
            "Enable the Spotify Connect receiver (requires librespot).",
            self.config.spotify_enabled, c,
        )

        if self.config.spotify_enabled:
            self.config.spotify_client_id = self._ask_string(
                "Spotify Client ID",
                "Your Spotify Web API client ID for metadata and controls.\n"
                "  Create one at https://developer.spotify.com/dashboard",
                self.config.spotify_client_id, c,
            )

            self.config.spotify_username = self._ask_string(
                "Spotify Username",
                "Your Spotify username (for librespot authentication).\n"
                "  Leave empty to use zeroconf discovery.",
                self.config.spotify_username, c,
            )

            self.config.spotify_bitrate = self._ask_choice(
                "Bitrate",
                "Audio quality for Spotify playback.",
                [("160", "160 kbps (Normal)"),
                 ("320", "320 kbps (High)")],
                str(self.config.spotify_bitrate), c,
            )
            self.config.spotify_bitrate = int(self.config.spotify_bitrate)

            self.config.spotify_device_name = self._ask_string(
                "Device Name",
                "Name shown in Spotify app when casting.",
                self.config.spotify_device_name or self.config.client_name, c,
            )

    def _run_dj(self) -> None:
        c = self._section_color(5)

        self.config.dj_source_mode = self._ask_choice(
            "DJ Source Mode",
            "Which audio sources feed the two DJ mixer channels.",
            [("mixed", "Channel A = SendSpin, Channel B = AirPlay"),
             ("dual_sendspin", "Both channels from SendSpin receivers"),
             ("dual_airplay", "Both channels from AirPlay receivers"),
             ("spotify_sendspin", "Channel A = SendSpin, Channel B = Spotify"),
             ("spotify_airplay", "Channel A = AirPlay, Channel B = Spotify"),
             ("dual_spotify", "Both channels from Spotify")],
            self.config.dj_source_mode, c,
        )

    # ── Summary ──

    def _show_summary(self) -> str:
        """Show all settings with changes highlighted. Returns 'save'/'edit'/'cancel'."""
        self.console.print()

        table = Table(
            title="[bold bright_cyan]◈ SETUP SUMMARY ◈[/]",
            border_style="bright_cyan",
            show_header=True,
            header_style="bold",
            width=min(72, _term_w() - 4),
            padding=(0, 1),
        )
        table.add_column("Setting", style="bright_white", min_width=26)
        table.add_column("Value", min_width=20)
        table.add_column("", min_width=3)

        # Fields to display (label, field_name, formatter)
        display_fields: list[tuple[str, str]] = [
            ("Client Name", "client_name"),
            ("Connection Mode", "mode"),
            ("Server URL", "server_url"),
            ("Listen Port", "listen_port"),
            ("FPS Limit", "fps_limit"),
            ("Brightness", "brightness"),
            ("Show Artwork", "show_artwork"),
            ("Album Art Colours", "use_art_colors"),
            ("Static Colour", "static_color"),
            ("Auto Play", "auto_play"),
            ("Auto Volume", "auto_volume"),
            ("SendSpin Enabled", "sendspin_enabled"),
            ("AirPlay Enabled", "airplay_enabled"),
            ("Spotify Connect", "spotify_enabled"),
            ("Spotify Client ID", "spotify_client_id"),
            ("Spotify Username", "spotify_username"),
            ("Spotify Bitrate", "spotify_bitrate"),
            ("Spotify Device Name", "spotify_device_name"),
            ("Swap Prompt", "swap_prompt"),
            ("Swap Auto Action", "swap_auto_action"),
            ("Forget AirPlay", "forget_airplay_devices"),
            ("DJ Source Mode", "dj_source_mode"),
        ]

        for label, field in display_fields:
            new_val = getattr(self.config, field)
            old_val = getattr(self._original, field)
            changed = new_val != old_val

            # Format value
            if isinstance(new_val, bool):
                val_str = "ON" if new_val else "OFF"
            elif field == "auto_volume":
                val_str = "Disabled" if new_val == -1 else f"{new_val}%"
            elif field == "brightness":
                val_str = f"{new_val}%"
            else:
                val_str = str(new_val) if new_val else "(empty)"

            # Skip irrelevant fields
            if field == "server_url" and self.config.mode != "connect":
                continue
            if field == "listen_port" and self.config.mode != "listen":
                continue
            if field == "static_color" and self.config.use_art_colors:
                continue
            if field in ("spotify_client_id", "spotify_username", "spotify_bitrate", "spotify_device_name") and not self.config.spotify_enabled:
                continue
            if field == "swap_auto_action" and self.config.swap_prompt:
                continue

            val_style = "bold bright_green" if changed else "dim"
            marker = "[bright_green]◂[/]" if changed else ""
            table.add_row(label, f"[{val_style}]{val_str}[/]", marker)

        self.console.print(table)
        self.console.print()

        return Prompt.ask(
            "[bright_cyan]▸[/] What would you like to do?",
            choices=["save", "edit", "cancel"],
            default="save",
            console=self.console,
        )

    # ── Main flow ──

    def run(self) -> Config:
        """Run the full setup wizard. Returns the (possibly modified) Config."""
        sections = [
            ("CONNECTION", self._run_connection),
            ("DISPLAY",    self._run_display),
            ("PLAYBACK",   self._run_playback),
            ("PROTOCOL",   self._run_protocol),
            ("SPOTIFY",    self._run_spotify),
            ("DJ MODE",    self._run_dj),
        ]

        self._play_intro()

        for idx, (name, runner) in enumerate(sections):
            self._play_section_intro(idx)
            color = self._section_color(idx)
            if self._section_skip_prompt(name, color):
                continue
            runner()

        # Summary loop
        while True:
            action = self._show_summary()
            if action == "save":
                self.config.save()
                self._play_outro("#00ff88")
                self.console.print()
                from alfieprime_musiciser.config import CONFIG_FILE
                self.console.print(
                    f"[bold bright_green]✓[/] Config saved to [dim]{CONFIG_FILE}[/]"
                )
                self.console.print()
                return self.config
            elif action == "edit":
                self.console.print()
                # Let user pick which section to re-edit
                self.console.print("[bold]Sections:[/]")
                for i, (name, _) in enumerate(sections):
                    color = self._section_color(i)
                    self.console.print(
                        f"  [{color}]{i + 1}[/] - {name}"
                    )
                self.console.print()
                choice = Prompt.ask(
                    "[bright_cyan]▸[/] Which section?",
                    choices=[str(i + 1) for i in range(len(sections))],
                    console=self.console,
                )
                idx = int(choice) - 1
                self._play_section_intro(idx)
                sections[idx][1]()
            else:
                # Cancel — return original config
                self._play_outro("#ff4444")
                self.console.print()
                self.console.print("[dim]Setup cancelled. Using previous settings.[/]")
                self.console.print()
                return self._original


# ── Public API ───────────────────────────────────────────────────────────────

def run_setup_wizard(console: Console, existing: Config | None = None) -> Config:
    """Run the animated setup wizard. Returns the final Config."""
    wizard = SetupWizard(console, existing)
    return wizard.run()
