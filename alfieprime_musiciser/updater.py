"""Auto-update checker for AlfiePRIME Musiciser.

Compares the installed version against the latest version published on
GitHub.  Works regardless of install method (git clone, pipx, pip).
Features an animated TUI matching the setup wizard style.
"""
from __future__ import annotations

import io as _io
import logging
import math
import os
import re
import subprocess
import shutil
import sys
import time
import threading
from pathlib import Path

from rich.console import Console, Group
from rich.style import Style
from rich.text import Text

logger = logging.getLogger(__name__)

_REPO_URL = "https://github.com/AlfiePRIME/AlfiePRIME-Musiciser.git"
_VERSION_URL = (
    "https://api.github.com/repos/AlfiePRIME/AlfiePRIME-Musiciser"
    "/contents/alfieprime_musiciser/__init__.py?ref=main"
)

IS_WINDOWS = sys.platform == "win32"

# ── ASCII art ────────────────────────────────────────────────────────────────

_ART_UPDATE = r"""
     ╭──────────────────────╮
     │    ╭───╮             │
     │    │ ↑ │   UPDATE    │
     │    │ ↑ │             │
     │  ╭─┴───┴─╮           │
     │  │ ◈◈◈◈◈ │           │
     │  ╰───────╯           │
     ╰──────────────────────╯
""".strip("\n")

_ART_UP_TO_DATE = r"""
     ╭──────────────────────╮
     │    ╭───╮             │
     │    │ ✓ │   LATEST    │
     │    │   │             │
     │  ╭─┴───┴─╮           │
     │  │ ◈◈◈◈◈ │           │
     │  ╰───────╯           │
     ╰──────────────────────╯
""".strip("\n")

_ART_UPDATING = r"""
     ╭──────────────────────╮
     │    ╭───╮             │
     │    │ ⟳ │   INSTALL   │
     │    │   │             │
     │  ╭─┴───┴─╮           │
     │  │ ▓▓▓▓▓ │           │
     │  ╰───────╯           │
     ╰──────────────────────╯
""".strip("\n")

_UPDATE_COLOR = "#00ccff"
_UP_TO_DATE_COLOR = "#00ff88"
_UPDATING_COLOR = "#ffaa00"


# ── Helpers (same as setup_wizard) ───────────────────────────────────────────

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


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
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


def _build_crt_bg(term_w: int, term_h: int, color: str = "#888888") -> list[Text]:
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


# ── Version helpers ──────────────────────────────────────────────────────────

def _parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _get_local_version() -> str:
    try:
        from alfieprime_musiciser import __version__
        return __version__
    except (ImportError, AttributeError):
        return "0.0.0"


def _fetch_remote_version() -> str | None:
    try:
        from urllib.request import urlopen, Request
        req = Request(_VERSION_URL, headers={
            "User-Agent": "AlfiePRIME-Musiciser",
            "Accept": "application/vnd.github.v3.raw",
        })
        with urlopen(req, timeout=8) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _find_git_dir() -> Path | None:
    path = Path(__file__).resolve().parent
    for _ in range(5):
        if (path / ".git").is_dir():
            return path
        path = path.parent
    return None


# ── Update TUI ───────────────────────────────────────────────────────────────

class _UpdateTUI:
    """Animated TUI for the update process."""

    def __init__(self) -> None:
        self._cursor = 0  # 0 = Update Now, 1 = Skip
        self._phase = "checking"  # checking, available, up_to_date, updating, done, failed
        self._local_version = ""
        self._remote_version = ""
        self._running = True
        self._result = ""  # "update", "skip", "done"
        self._update_status = ""  # status text during update
        self._progress = 0.0  # progress bar during update
        self._console: Console | None = None
        self._console_size: tuple[int, int] = (0, 0)

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

    def _build_frame(self, term_w: int, term_h: int) -> Group:
        t = time.time()

        if self._phase == "checking":
            color = _UPDATE_COLOR
            art = _ART_UPDATE
        elif self._phase == "available":
            color = _UPDATE_COLOR
            art = _ART_UPDATE
        elif self._phase == "up_to_date":
            color = _UP_TO_DATE_COLOR
            art = _ART_UP_TO_DATE
        elif self._phase in ("updating", "done", "failed"):
            color = _UPDATING_COLOR
            art = _ART_UPDATING
        else:
            color = _UPDATE_COLOR
            art = _ART_UPDATE

        cr, cg, cb = _hex_to_rgb(color)
        bg = _build_crt_bg(term_w, term_h, color)
        panel_w = min(50, term_w - 6)
        panel_lines: list[Text] = []

        # Header
        header = Text()
        hc = _hex(min(255, cr + 40), min(255, cg + 40), min(255, cb + 40))
        header.append(_center("◈ UPDATE CHECK ◈", panel_w), Style(color=hc, bold=True))
        panel_lines.append(header)

        sep = Text()
        sep.append(_center("━" * (panel_w - 8), panel_w), Style(color=_dim(color, 0.4)))
        panel_lines.append(sep)
        panel_lines.append(Text(""))

        # ASCII art with animation
        for al in art.split("\n"):
            aline = Text()
            flicker = 0.5 + 0.1 * math.sin(t * 3 + len(panel_lines) * 0.4)
            ac = _hex(cr * flicker, cg * flicker, cb * flicker)
            aline.append(_center(al, panel_w), Style(color=ac))
            panel_lines.append(aline)
        panel_lines.append(Text(""))

        # Phase-specific content
        if self._phase == "checking":
            # Animated checking dots
            dots = "." * (int(t * 3) % 4)
            line = Text()
            line.append(_center(f"Checking for updates{dots}", panel_w),
                        Style(color="#aaaaaa"))
            panel_lines.append(line)
            panel_lines.append(Text(""))

            # Spinner
            spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            spinner = spinner_chars[int(t * 10) % len(spinner_chars)]
            sline = Text()
            sline.append(_center(f"{spinner}  v{self._local_version}", panel_w),
                         Style(color=color))
            panel_lines.append(sline)

        elif self._phase == "available":
            # Version comparison
            vline = Text()
            vline.append(_center(f"v{self._local_version}  →  v{self._remote_version}", panel_w),
                         Style(color="#ffffff", bold=True))
            panel_lines.append(vline)
            panel_lines.append(Text(""))

            uline = Text()
            uline.append(_center("A new version is available!", panel_w),
                         Style(color=color, bold=True))
            panel_lines.append(uline)
            panel_lines.append(Text(""))
            panel_lines.append(Text(""))

            # Buttons
            for i, label in enumerate(["  [ Update Now ]  ", "  [ Skip ]  "]):
                selected = i == self._cursor
                btn = Text()
                if i == 0:
                    btn_c = "#00ff88" if selected else "#555555"
                else:
                    btn_c = "#ff4444" if selected else "#555555"
                btn.append(_center(label, panel_w),
                           Style(color=btn_c, bold=selected))
                panel_lines.append(btn)

        elif self._phase == "up_to_date":
            vline = Text()
            vline.append(_center(f"v{self._local_version}", panel_w),
                         Style(color=color, bold=True))
            panel_lines.append(vline)
            panel_lines.append(Text(""))

            msg = Text()
            msg.append(_center("Already up to date!", panel_w),
                       Style(color="#aaaaaa"))
            panel_lines.append(msg)

        elif self._phase == "updating":
            # Progress bar
            bar_w = panel_w - 12
            filled = int(bar_w * self._progress)
            empty = bar_w - filled
            pline = Text()
            pline.append("      ", Style())
            pline.append("▓" * filled, Style(color=color))
            pline.append("░" * empty, Style(color="#333333"))
            pline.append(f" {int(self._progress * 100):>3}%", Style(color="#aaaaaa"))
            panel_lines.append(pline)
            panel_lines.append(Text(""))

            sline = Text()
            sline.append(_center(self._update_status, panel_w),
                         Style(color="#aaaaaa"))
            panel_lines.append(sline)

        elif self._phase == "done":
            vline = Text()
            vline.append(_center(f"Updated to v{self._remote_version}!", panel_w),
                         Style(color="#00ff88", bold=True))
            panel_lines.append(vline)
            panel_lines.append(Text(""))

            msg = Text()
            msg.append(_center("Restarting...", panel_w),
                       Style(color="#aaaaaa"))
            panel_lines.append(msg)

        elif self._phase == "failed":
            msg = Text()
            msg.append(_center("Update failed!", panel_w),
                       Style(color="#ff4444", bold=True))
            panel_lines.append(msg)
            panel_lines.append(Text(""))

            hint = Text()
            hint.append(_center("Try manually:", panel_w),
                        Style(color="#aaaaaa"))
            panel_lines.append(hint)

            cmd = Text()
            cmd_str = f"pipx install --force git+{_REPO_URL}"
            if len(cmd_str) > panel_w - 4:
                cmd_str = cmd_str[:panel_w - 7] + "..."
            cmd.append(_center(cmd_str, panel_w), Style(color="#ffaa00"))
            panel_lines.append(cmd)
            panel_lines.append(Text(""))

            btn = Text()
            btn.append(_center("  [ OK ]  ", panel_w),
                       Style(color=color, bold=True))
            panel_lines.append(btn)

        # Pad to fill panel
        while len(panel_lines) < 20:
            panel_lines.append(Text(""))

        # Hints
        if self._phase == "available":
            hints = Text()
            hints.append(_center("↑↓ Select  Enter Confirm  Esc Skip", panel_w),
                         Style(color=_dim(color, 0.5)))
            panel_lines.append(hints)
        elif self._phase == "failed":
            hints = Text()
            hints.append(_center("Press Enter or Esc to continue", panel_w),
                         Style(color=_dim(color, 0.5)))
            panel_lines.append(hints)

        return _compose_panel(bg, panel_lines, panel_w, term_w, term_h)

    def _handle_key(self, k: str) -> None:
        if self._phase == "available":
            if k == "arrow_up":
                self._cursor = 0
            elif k == "arrow_down":
                self._cursor = 1
            elif k in ("\r", "\n"):
                if self._cursor == 0:
                    self._result = "update"
                else:
                    self._result = "skip"
                self._running = False
            elif k in ("\x1b", "escape"):
                self._result = "skip"
                self._running = False
        elif self._phase in ("up_to_date", "failed"):
            if k in ("\r", "\n", "\x1b", "escape", " "):
                self._result = "done"
                self._running = False

    def _parse_input(self, data: bytes) -> None:
        i = 0
        while i < len(data):
            if data[i:i + 3] == b"\x1b[A":
                self._handle_key("arrow_up")
                i += 3
            elif data[i:i + 3] == b"\x1b[B":
                self._handle_key("arrow_down")
                i += 3
            elif data[i:i + 1] == b"\x1b":
                rest = data[i + 1:]
                if not rest or rest[0:1] not in (b"[", b"O"):
                    self._handle_key("escape")
                    i += 1
                else:
                    i += 1
                    while i < len(data) and not data[i:i + 1].isalpha() and data[i:i + 1] != b"~":
                        i += 1
                    i += 1
            elif data[i:i + 1] == b"\x03":
                self._result = "skip"
                self._running = False
                i += 1
            elif data[i:i + 1] in (b"\r", b"\n"):
                self._handle_key("\r")
                i += 1
            else:
                ch = data[i:i + 1].decode("ascii", errors="ignore")
                if ch:
                    self._handle_key(ch)
                i += 1

    def run_check(self) -> str:
        """Run the update check TUI. Returns 'update', 'skip', or 'done'."""
        self._local_version = _get_local_version()
        self._phase = "checking"
        self._running = True

        # Fetch remote version in background
        remote_result: list[str | None] = [None]
        fetch_done = threading.Event()

        def _fetch():
            remote_result[0] = _fetch_remote_version()
            fetch_done.set()

        fetch_thread = threading.Thread(target=_fetch, daemon=True)
        fetch_thread.start()

        if IS_WINDOWS:
            return self._run_check_windows(remote_result, fetch_done)
        else:
            return self._run_check_unix(remote_result, fetch_done)

    def _run_check_unix(self, remote_result: list, fetch_done: threading.Event) -> str:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()

            while self._running:
                # Check if fetch completed
                if self._phase == "checking" and fetch_done.is_set():
                    remote = remote_result[0]
                    if remote is None:
                        # Network error — exit silently
                        self._result = "done"
                        break
                    self._remote_version = remote
                    local_t = _parse_version(self._local_version)
                    remote_t = _parse_version(self._remote_version)
                    if remote_t > local_t:
                        self._phase = "available"
                    else:
                        self._phase = "up_to_date"
                        # Auto-dismiss after 1.5s
                        self._up_to_date_time = time.monotonic()

                # Auto-dismiss up_to_date after 1.5s
                if self._phase == "up_to_date":
                    if hasattr(self, '_up_to_date_time'):
                        if time.monotonic() - self._up_to_date_time > 1.5:
                            self._result = "done"
                            break

                tw, th = _term_size()
                frame = self._build_frame(tw, th)
                rendered = self._render_to_ansi(frame, tw, th)
                sys.stdout.write(f"\x1b[H{rendered}")
                sys.stdout.flush()

                ready = select.select([fd], [], [], 1.0 / 24)
                if ready[0]:
                    data = os.read(fd, 64)
                    if data:
                        self._parse_input(data)
        finally:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        return self._result

    def _run_check_windows(self, remote_result: list, fetch_done: threading.Event) -> str:
        import msvcrt
        _ARROW_MAP = {b"H": "arrow_up", b"P": "arrow_down"}

        os.system("cls")
        try:
            while self._running:
                if self._phase == "checking" and fetch_done.is_set():
                    remote = remote_result[0]
                    if remote is None:
                        self._result = "done"
                        break
                    self._remote_version = remote
                    local_t = _parse_version(self._local_version)
                    remote_t = _parse_version(self._remote_version)
                    if remote_t > local_t:
                        self._phase = "available"
                    else:
                        self._phase = "up_to_date"
                        self._up_to_date_time = time.monotonic()

                if self._phase == "up_to_date":
                    if hasattr(self, '_up_to_date_time'):
                        if time.monotonic() - self._up_to_date_time > 1.5:
                            self._result = "done"
                            break

                tw, th = _term_size()
                frame = self._build_frame(tw, th)
                rendered = self._render_to_ansi(frame, tw, th)
                sys.stdout.write(f"\x1b[H{rendered}")
                sys.stdout.flush()

                if msvcrt.kbhit():
                    data = msvcrt.getch()
                    if data in (b"\xe0", b"\x00"):
                        data2 = msvcrt.getch()
                        arrow = _ARROW_MAP.get(data2)
                        if arrow:
                            self._handle_key(arrow)
                    elif data == b"\x03":
                        self._result = "skip"
                        self._running = False
                    elif data == b"\r":
                        self._handle_key("\r")
                    elif data == b"\x1b":
                        self._handle_key("escape")
                else:
                    time.sleep(1.0 / 24)
        finally:
            os.system("cls")

        return self._result

    def run_update(self) -> bool:
        """Run the update process with animated progress. Returns True on success."""
        self._phase = "updating"
        self._running = True

        # Run update in background thread
        success_flag: list[bool] = [False]
        update_done = threading.Event()

        def _do_update():
            self._update_status = "Checking install method..."
            self._progress = 0.1
            git_dir = _find_git_dir()

            if git_dir is not None:
                self._update_status = "Pulling latest changes..."
                self._progress = 0.2
                try:
                    result = subprocess.run(
                        ["git", "pull", "--ff-only"],
                        cwd=str(git_dir),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if result.returncode == 0:
                        self._progress = 0.4
                    else:
                        git_dir_inner = None
                except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                    git_dir_inner = None
                else:
                    git_dir_inner = git_dir
            else:
                git_dir_inner = None

            self._update_status = "Reinstalling package..."
            self._progress = 0.5
            install_source = str(git_dir_inner) if git_dir_inner else f"git+{_REPO_URL}"

            # Try pipx first (use system Python, not venv Python)
            pipx_python = "python" if sys.platform == "win32" else sys.executable
            try:
                self._update_status = "Installing via pipx..."
                self._progress = 0.6
                result = subprocess.run(
                    [pipx_python, "-m", "pipx", "install", "--force", install_source],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if result.returncode == 0:
                    self._progress = 1.0
                    success_flag[0] = True
                    update_done.set()
                    return
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass

            # Fall back to pip — use --force-reinstall so new files
            # (like __main__.py) are written even if the version looks
            # the same to pip.
            try:
                self._update_status = "Installing via pip..."
                self._progress = 0.7
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "--force-reinstall", "--no-cache-dir", "--quiet",
                     install_source],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if result.returncode == 0:
                    self._progress = 1.0
                    success_flag[0] = True
                    update_done.set()
                    return
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass

            update_done.set()

        update_thread = threading.Thread(target=_do_update, daemon=True)
        update_thread.start()

        if IS_WINDOWS:
            self._run_update_loop_windows(update_done)
        else:
            self._run_update_loop_unix(update_done)

        return success_flag[0]

    def _run_update_loop_unix(self, update_done: threading.Event) -> None:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()

            while not update_done.is_set():
                tw, th = _term_size()
                frame = self._build_frame(tw, th)
                rendered = self._render_to_ansi(frame, tw, th)
                sys.stdout.write(f"\x1b[H{rendered}")
                sys.stdout.flush()
                time.sleep(1.0 / 24)

            # Show final frame briefly
            if self._progress >= 1.0:
                self._phase = "done"
            else:
                self._phase = "failed"
                self._running = True

            # Show result for a moment (done) or wait for dismiss (failed)
            if self._phase == "done":
                end = time.monotonic() + 1.5
                while time.monotonic() < end:
                    tw, th = _term_size()
                    frame = self._build_frame(tw, th)
                    rendered = self._render_to_ansi(frame, tw, th)
                    sys.stdout.write(f"\x1b[H{rendered}")
                    sys.stdout.flush()
                    time.sleep(1.0 / 24)
            else:
                # Failed — wait for key press
                import select
                while self._running:
                    tw, th = _term_size()
                    frame = self._build_frame(tw, th)
                    rendered = self._render_to_ansi(frame, tw, th)
                    sys.stdout.write(f"\x1b[H{rendered}")
                    sys.stdout.flush()
                    ready = select.select([fd], [], [], 1.0 / 24)
                    if ready[0]:
                        data = os.read(fd, 64)
                        if data:
                            self._parse_input(data)
        finally:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _run_update_loop_windows(self, update_done: threading.Event) -> None:
        import msvcrt

        os.system("cls")
        try:
            while not update_done.is_set():
                tw, th = _term_size()
                frame = self._build_frame(tw, th)
                rendered = self._render_to_ansi(frame, tw, th)
                sys.stdout.write(f"\x1b[H{rendered}")
                sys.stdout.flush()
                time.sleep(1.0 / 24)

            if self._progress >= 1.0:
                self._phase = "done"
            else:
                self._phase = "failed"
                self._running = True

            if self._phase == "done":
                end = time.monotonic() + 1.5
                while time.monotonic() < end:
                    tw, th = _term_size()
                    frame = self._build_frame(tw, th)
                    rendered = self._render_to_ansi(frame, tw, th)
                    sys.stdout.write(f"\x1b[H{rendered}")
                    sys.stdout.flush()
                    time.sleep(1.0 / 24)
            else:
                while self._running:
                    tw, th = _term_size()
                    frame = self._build_frame(tw, th)
                    rendered = self._render_to_ansi(frame, tw, th)
                    sys.stdout.write(f"\x1b[H{rendered}")
                    sys.stdout.flush()
                    if msvcrt.kbhit():
                        msvcrt.getch()
                        self._running = False
                    else:
                        time.sleep(1.0 / 24)
        finally:
            os.system("cls")


def _restart_after_update() -> None:
    """Re-launch the app after a successful update.

    Tries multiple strategies because the install method (pipx vs pip)
    and platform affect which approach works.
    """
    import subprocess as _sp
    import shutil

    if sys.platform == "win32":
        # Strategy 1: find the .exe entry point from sys.argv[0]
        exe = sys.argv[0]
        if not exe.lower().endswith(".exe"):
            exe += ".exe"
        if os.path.isfile(exe):
            _sp.Popen([exe] + sys.argv[1:])
            sys.exit(0)

        # Strategy 2: find it on PATH
        found = shutil.which("alfieprime-musiciser")
        if found:
            _sp.Popen([found] + sys.argv[1:])
            sys.exit(0)

        # Strategy 3: try -m (works if __main__.py was installed)
        try:
            _sp.Popen([sys.executable, "-m", "alfieprime_musiciser"] + sys.argv[1:])
            sys.exit(0)
        except OSError:
            pass

        # All strategies failed — just exit, user restarts manually
        print("\nUpdate installed. Please restart the app manually.")
        sys.exit(0)
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)


# ── Public API ───────────────────────────────────────────────────────────────

def check_for_updates(console: Console) -> None:
    """Check for updates; only show the TUI if an update is available."""
    local_version = _get_local_version()
    remote_version = _fetch_remote_version()

    if remote_version is None:
        return
    if _parse_version(remote_version) <= _parse_version(local_version):
        return

    # Update available — launch TUI
    tui = _UpdateTUI()
    tui._local_version = local_version
    tui._remote_version = remote_version
    tui._phase = "available"
    result = tui.run_check()

    if result == "update":
        success = tui.run_update()
        if success:
            _restart_after_update()
