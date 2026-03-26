"""Standalone GUI window that renders Rich output in a separate process.

The tkinter rendering runs in its own OS process so it never competes with
the asyncio event loop or audio pipeline for CPU time.  Communication is
via a multiprocessing.Connection (pipe) that sends pre-processed segments.

Architecture:
    Main process  ──(pipe)──>  GUI process (tkinter)
    - Renders Rich layout to segments
    - Sends list[(text, fg, bg, bold)] via pipe
    - Receives (cols, rows) size and key events back
"""

from __future__ import annotations

import multiprocessing
import platform
import time
from multiprocessing.connection import Connection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


# ── Messages ────────────────────────────────────────────────────────────────

# Main → GUI
MSG_SEGMENTS = 1   # (MSG_SEGMENTS, segments_list)
MSG_QUIT = 2       # (MSG_QUIT,)

# GUI → Main
MSG_SIZE = 3       # (MSG_SIZE, cols, rows)
MSG_KEY = 4        # (MSG_KEY, char)
MSG_CLOSED = 5     # (MSG_CLOSED,)


# ── GUI Process ─────────────────────────────────────────────────────────────


def _pick_monospace_font() -> str:
    """Return the best available monospace font family."""
    from tkinter import font as tkfont

    system = platform.system()
    if system == "Windows":
        candidates = ["Cascadia Mono", "Consolas", "Courier New"]
    elif system == "Darwin":
        candidates = ["Menlo", "Monaco", "Courier New"]
    else:
        candidates = ["DejaVu Sans Mono", "Liberation Mono", "Noto Mono", "Monospace"]

    available = set(tkfont.families())
    for name in candidates:
        if name in available:
            return name
    return "TkFixedFont"


def _gui_process_main(conn: Connection, title: str) -> None:
    """Entry point for the GUI child process.  Runs the tkinter main loop."""
    import tkinter as tk
    from tkinter import font as tkfont

    BG_COLOR = "#0a0a0a"
    FG_COLOR = "#cccccc"

    root = tk.Tk()
    root.title(title)
    root.configure(bg=BG_COLOR)

    family = _pick_monospace_font()
    font = tkfont.Font(family=family, size=11)
    bold_font = tkfont.Font(family=family, size=11, weight="bold")
    char_width = font.measure("M")
    char_height = font.metrics("linespace")

    win_w = char_width * 120 + 16
    win_h = char_height * 50 + 16
    root.geometry(f"{win_w}x{win_h}")
    root.minsize(char_width * 60 + 16, char_height * 20 + 16)

    text_widget = tk.Text(
        root,
        bg=BG_COLOR, fg=FG_COLOR,
        font=font, wrap="none", state="disabled",
        insertwidth=0, borderwidth=0, highlightthickness=0,
        padx=4, pady=4, cursor="arrow",
    )
    text_widget.pack(fill="both", expand=True)

    # Tag cache
    tags: dict[tuple[str | None, str | None, bool], str] = {}
    tag_counter = [0]

    def get_tag(fg: str | None, bg: str | None, bold: bool) -> str:
        key = (fg, bg, bold)
        tag = tags.get(key)
        if tag is not None:
            return tag
        name = f"t{tag_counter[0]}"
        tag_counter[0] += 1
        kwargs: dict = {}
        if fg:
            kwargs["foreground"] = fg
        if bg:
            kwargs["background"] = bg
        if bold:
            kwargs["font"] = bold_font
        text_widget.tag_configure(name, **kwargs)
        tags[key] = name
        return name

    alive = [True]

    def on_close() -> None:
        alive[0] = False
        try:
            conn.send((MSG_CLOSED,))
        except (BrokenPipeError, OSError):
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    _arrow_keysyms = {"Up": "arrow_up", "Down": "arrow_down"}

    def on_key(event: tk.Event) -> None:
        # Map arrow keys to named strings
        mapped = _arrow_keysyms.get(event.keysym)
        key = mapped or event.char
        if key:
            try:
                conn.send((MSG_KEY, key))
            except (BrokenPipeError, OSError):
                pass

    root.bind("<Key>", on_key)

    def get_size() -> tuple[int, int]:
        w = text_widget.winfo_width() - 8
        h = text_widget.winfo_height() - 8
        cols = max(60, w // max(char_width, 1))
        # Subtract 1 because tkinter's Text widget always has an implicit
        # trailing newline that occupies one visible row.
        rows = max(20, h // max(char_height, 1) - 1)
        return cols, rows

    def apply_segments(segments: list[tuple[str, str | None, str | None, bool]]) -> None:
        """Render segments into the text widget — single insert + batch tag_add."""
        parts: list[str] = []
        ranges: list[tuple[int, int, str]] = []
        offset = 0
        prev_key: tuple[str | None, str | None, bool] | None = None
        run_start = 0

        for seg_text, fg, bg, bold in segments:
            if not seg_text:
                continue
            key = (fg, bg, bold)
            if key == prev_key:
                parts.append(seg_text)
                offset += len(seg_text)
            else:
                if prev_key is not None and offset > run_start:
                    ranges.append((run_start, offset, get_tag(*prev_key)))
                run_start = offset
                prev_key = key
                parts.append(seg_text)
                offset += len(seg_text)

        if prev_key is not None and offset > run_start:
            ranges.append((run_start, offset, get_tag(*prev_key)))

        full_text = "".join(parts)

        text_widget.configure(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", full_text)
        for start, end, tag in ranges:
            text_widget.tag_add(tag, f"1.0+{start}c", f"1.0+{end}c")
        text_widget.configure(state="disabled")

    # Send initial size
    root.update_idletasks()
    try:
        conn.send((MSG_SIZE, *get_size()))
    except (BrokenPipeError, OSError):
        pass

    last_size = get_size()
    last_size_time = 0.0

    def poll_pipe() -> None:
        nonlocal last_size, last_size_time
        if not alive[0]:
            return

        # Process all pending messages from the main process
        try:
            while conn.poll():
                msg = conn.recv()
                if msg[0] == MSG_SEGMENTS:
                    apply_segments(msg[1])
                elif msg[0] == MSG_QUIT:
                    alive[0] = False
                    root.destroy()
                    return
        except (EOFError, BrokenPipeError, OSError):
            alive[0] = False
            root.destroy()
            return

        # Send size updates (throttled to every 100ms)
        now = time.monotonic()
        if now - last_size_time > 0.1:
            cur_size = get_size()
            if cur_size != last_size:
                last_size = cur_size
                try:
                    conn.send((MSG_SIZE, *cur_size))
                except (BrokenPipeError, OSError):
                    pass
            last_size_time = now

        # Schedule next poll — 8ms gives ~120fps input responsiveness
        root.after(8, poll_pipe)

    root.after(8, poll_pipe)

    try:
        root.mainloop()
    except Exception:
        pass
    finally:
        alive[0] = False
        conn.close()


# ── Main Process Handle ─────────────────────────────────────────────────────


class GUIProcess:
    """Handle for communicating with the GUI child process from the main process.

    Usage:
        gui = GUIProcess(title="My App", on_key=handle_key, on_close=handle_close)
        gui.start()

        # In render loop:
        gui.send_segments(segments)
        gui.process_events()  # dispatches key/close/resize callbacks
        cols, rows = gui.get_size()
    """

    def __init__(
        self,
        title: str = "AlfiePRIME Musiciser",
        on_key: Callable[[str], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._title = title
        self._on_key = on_key
        self._on_close = on_close
        self._conn: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._cols = 120
        self._rows = 50
        self.alive = False

    def start(self) -> None:
        """Spawn the GUI process."""
        # Use 'spawn' context so the child starts clean — avoids fork issues
        # with tkinter and inherited asyncio state on Linux/macOS.
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe()
        self._conn = parent_conn
        self._process = ctx.Process(
            target=_gui_process_main,
            args=(child_conn, self._title),
            daemon=True,
        )
        self._process.start()
        child_conn.close()  # only the child uses this end
        self.alive = True

    def send_segments(
        self, segments: list[tuple[str, str | None, str | None, bool]],
    ) -> None:
        """Send rendered segments to the GUI process for display."""
        if not self.alive or self._conn is None:
            return
        try:
            self._conn.send((MSG_SEGMENTS, segments))
        except (BrokenPipeError, OSError):
            self.alive = False

    def process_events(self) -> None:
        """Process any pending messages from the GUI process (key presses, resize, close)."""
        if not self.alive or self._conn is None:
            return
        try:
            while self._conn.poll():
                msg = self._conn.recv()
                if msg[0] == MSG_SIZE:
                    self._cols = msg[1]
                    self._rows = msg[2]
                elif msg[0] == MSG_KEY:
                    if self._on_key:
                        self._on_key(msg[1])
                elif msg[0] == MSG_CLOSED:
                    self.alive = False
                    if self._on_close:
                        self._on_close()
                    return
        except (EOFError, BrokenPipeError, OSError):
            self.alive = False
            if self._on_close:
                self._on_close()

    def get_size(self) -> tuple[int, int]:
        """Return current (cols, rows) as reported by the GUI process."""
        return self._cols, self._rows

    def stop(self) -> None:
        """Tell the GUI process to quit and clean up."""
        if self._conn is not None:
            try:
                self._conn.send((MSG_QUIT,))
            except (BrokenPipeError, OSError):
                pass
            self._conn.close()
            self._conn = None
        if self._process is not None:
            self._process.join(timeout=2)
            if self._process.is_alive():
                self._process.terminate()
            self._process = None
        self.alive = False
