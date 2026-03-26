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

from alfieprime_musiciser.colors import ColorTheme, _hex_to_rgb, _rgb_to_hex
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
)


# Standard ANSI 16 colours (0-15) mapped to hex for the GUI renderer.
_RICH_STANDARD_COLORS: dict[int, str] = {
    0: "#000000", 1: "#aa0000", 2: "#00aa00", 3: "#aa5500",
    4: "#0000aa", 5: "#aa00aa", 6: "#00aaaa", 7: "#aaaaaa",
    8: "#555555", 9: "#ff5555", 10: "#55ff55", 11: "#ffff55",
    12: "#5555ff", 13: "#ff55ff", 14: "#55ffff", 15: "#ffffff",
}


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


class BoomBoxTUI:
    """The party-themed boom box terminal UI."""

    def __init__(self, visualizer: AudioVisualizer, gui: bool = False) -> None:
        self._visualizer = visualizer
        self.state = PlayerState()
        self._running = False
        self._gui_mode = gui
        self._gui_window = None  # TerminalEmulator instance when in GUI mode
        self._command_callback: Callable[[str], None] | None = None
        # Track button positions for mouse clicks: {name: (col_start, col_end)}
        self._button_regions: dict[str, tuple[int, int]] = {}
        # Row (0-based from top of screen) where transport controls are rendered
        self._controls_row: int = 0
        # Cached terminal dimensions, updated each frame
        self._term_width: int = 120
        self._term_height: int = 50

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

    def _build_layout(self) -> Group:
        # Query real terminal size each frame
        self._term_width, self._term_height = self._get_terminal_size()
        term_w = self._term_width
        # Content width inside panels: border takes 2 chars, padding varies
        padded_inner = max(term_w - 4, 20)    # padding=(0,1) → 2 border + 2 padding
        flush_inner = max(term_w - 2, 20)     # padding=(0,0) → 2 border only
        bands, peaks, vu_left, vu_right = self._visualizer.get_spectrum()
        th = self.state.theme

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

        parts.append(Panel(
            Group(*np_lines, Text(""), controls_text),
            title=f" {reel_l} NOW PLAYING {reel_r} ",
            title_align="center", border_style=th.border_now_playing, padding=(0, 1),
        ))
        row += np_panel_content_lines + 2  # +2 for panel borders

        # ── Calculate available rows for variable-height sections ──
        # Fixed-height rows used by other panels:
        #   frame_top(1) + title(3) + now_playing(np_panel_content_lines+2)
        #   + VU(4) + party_lights(4) + status(3) + frame_bot(1)
        #   + spectrum borders(2) + dance_floor borders(2)
        np_rows = np_panel_content_lines + 2
        fixed_rows = 1 + 3 + np_rows + 4 + 4 + 4 + 1 + 2 + 2
        available = max(self._term_height - fixed_rows, 8)
        # Dance floor gets a fixed size; spectrum expands to fill all remaining space
        dance_height = max(5, min(8, available // 3))
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

        # Status bar — connection info + codec on row 1, system stats on row 2
        status_table = Table.grid(padding=0, expand=True)
        status_table.add_column(ratio=1)
        status_table.add_column(ratio=1)
        status_table.add_row(
            render_server_info(self.state.server_name, self.state.group_name, self.state.connected, theme=th),
            render_codec_info(self.state.codec, self.state.sample_rate, self.state.bit_depth, theme=th),
        )
        status_table.add_row(render_stats_info(theme=th), Text(""))
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
        if k == "p":
            self._fire_command("play_pause")
        elif k == "n":
            self._fire_command("next")
        elif k == "b":
            self._fire_command("previous")
        elif k == "s":
            self._fire_command("shuffle")
        elif k == "r":
            self._fire_command("repeat")
        elif k == "arrow_up":
            self._fire_command("volume_up")
        elif k == "arrow_down":
            self._fire_command("volume_down")

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
                if ch == b"q" or ch == b"Q":
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
        buf_console = Console(
            file=buf, width=term_w, height=term_h,
            force_terminal=True, color_system="truecolor",
            no_color=False,
        )
        layout = self._build_layout()
        buf_console.print(layout)

        rendered = buf.getvalue()

        lines = rendered.split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        if len(lines) > term_h:
            lines = lines[:term_h]
        elif len(lines) < term_h:
            lines.extend([""] * (term_h - len(lines)))

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
        console = Console(
            file=_io.StringIO(), width=term_w, height=term_h,
            force_terminal=True, color_system="truecolor",
        )

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
                    flicker = 0.85 + 0.15 * math.sin(time.time() * 60 + row * 3)
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
        static_rows: list[str] = []
        for row in range(term_h):
            line = "".join(
                random.choice("░▒▓█▌▐╌╍·.")
                if random.random() < 0.3
                else random.choice("·. ·  ")
                for _ in range(term_w)
            )
            static_rows.append(line)

        # ── Animated ASCII art ──
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        spin_ch = spinner[int(t * 10) % len(spinner)]

        # Signal wave animation — ripples outward from dish
        wave_frame = int(t * 3) % 4
        waves = [
            ["       ", "      )", "    ) )", "  ) ) )"],
            ["       ", "      )", "    ) )", "  ) ) )"],
            ["       ", "      )", "    ) )", "  ) ) )"],
            ["       ", "      )", "    ) )", "  ) ) )"],
        ]
        # Animate by showing 1-3 waves based on frame
        wave_count = (wave_frame % 3) + 1

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

        for row_idx in range(term_h):
            real_row = real_rows[row_idx] if row_idx < len(real_rows) else []
            for col_idx in range(term_w):
                # Distance from top-left corner (diagonal sweep)
                dist = row_idx + col_idx * 0.7
                if dist < threshold:
                    # This cell is "lit" — show real content
                    if col_idx < len(real_row):
                        ch, fg, bg, bold = real_row[col_idx]
                        # Brightness ramp: recently lit cells glow brighter
                        lit_age = (threshold - dist) / max(max_dist * 0.15, 1)
                        if lit_age < 1.0 and fg:
                            # Fade in: brighten towards full
                            fr, fg_c, fb = _hex_to_rgb(fg)
                            fade = min(1.0, lit_age)
                            fr = int(fr * fade)
                            fg_c = int(fg_c * fade)
                            fb = int(fb * fade)
                            segs.append((ch, _rgb_to_hex(fr, fg_c, fb), bg, bold))
                        else:
                            segs.append((ch, fg, bg, bold))
                    else:
                        segs.append((" ", None, None, False))
                else:
                    # Still dark — show fading static
                    fade_to_edge = max(0.0, 1.0 - (dist - threshold) / max(max_dist * 0.1, 1))
                    if random.random() < 0.15 * fade_to_edge:
                        flicker = 0.5 + 0.5 * math.sin(t * 12 + row_idx + col_idx)
                        br = int(50 * flicker * fade_to_edge)
                        c = f"#{br:02x}{br:02x}{br:02x}"
                        segs.append((random.choice("░▒·"), c, None, False))
                    else:
                        segs.append((" ", None, None, False))

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
            flicker = 0.9 + 0.1 * math.sin(time.time() * 80)
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
        c = Console(
            file=buf, width=term_w, height=term_h,
            force_terminal=True, color_system="truecolor", no_color=False,
        )
        text = Text()
        for s_text, fg, bg, bold in segs:
            style = Style(color=fg, bgcolor=bg, bold=bold if bold else None)
            text.append(s_text, style)
        c.print(text, end="")
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
                frame = self._render_frame()
                sys.stdout.write(f"\x1b[H{frame}")
                sys.stdout.flush()
                await asyncio.sleep(1 / 30)

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
            # Leave alternate screen and show cursor
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
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
                gui.process_events()
                segments = self._render_frame_gui()
                gui.send_segments(segments)
                await asyncio.sleep(1 / 30)

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
