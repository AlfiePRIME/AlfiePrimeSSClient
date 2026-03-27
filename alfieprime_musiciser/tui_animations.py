from __future__ import annotations

import io as _io
import math
import random
import time

from rich.console import Console, Group
from rich.style import Style
from rich.text import Text

from alfieprime_musiciser.colors import _hex_to_rgb, _hsv_to_rgb


def _safe_hex(r: int | float, g: int | float, b: int | float) -> str:
    return f"#{max(0,min(255,int(r))):02x}{max(0,min(255,int(g))):02x}{max(0,min(255,int(b))):02x}"


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


class AnimationsMixin:

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

        # Box dimensions: phrase padded with 2 chars each side + border
        box_w = len(phrase) + 6  # "│  phrase  │"
        box_h = 5  # top border, blank, phrase, blank, bottom border

        # Floating position — gentle drift across screen
        max_dx = max(0, term_w - box_w)
        max_dy = max(0, term_h - box_h - 4)
        drift_x = int((math.sin(t * 0.15) * 0.3 + 0.5) * max_dx)
        drift_y = int((math.sin(t * 0.1 + 1.0) * 0.3 + 0.5) * max_dy) + 2

        # Box row indices
        box_top = drift_y
        box_bottom = drift_y + box_h - 1
        phrase_row = drift_y + 2  # middle of box

        # Title
        title = "A L F I E P R I M E"
        title_x = max(0, (term_w - len(title)) // 2)

        # Zzz animation
        zzz_frames = ["z", "zz", "zzz", "zz"]
        zzz = zzz_frames[int(t * 0.8) % len(zzz_frames)]

        # Border colour — gentle pulse
        border_pulse = 0.12 + 0.08 * math.sin(t * 0.8)
        border_br = int(255 * border_pulse)
        border_c = _safe_hex(border_br, border_br, border_br + 20)

        for row in range(term_h):
            line_segs: list[tuple[str, str | None, str | None, bool]] = []

            if row == box_top:
                # Top border: ╭───╮
                line_segs.append((" " * drift_x, None, None, False))
                line_segs.append(("╭" + "─" * (box_w - 2) + "╮", border_c, None, False))
                pad_r = term_w - drift_x - box_w
                if pad_r > 0:
                    line_segs.append((" " * pad_r, None, None, False))
            elif row == box_bottom:
                # Bottom border: ╰───╯
                line_segs.append((" " * drift_x, None, None, False))
                line_segs.append(("╰" + "─" * (box_w - 2) + "╯", border_c, None, False))
                pad_r = term_w - drift_x - box_w
                if pad_r > 0:
                    line_segs.append((" " * pad_r, None, None, False))
            elif box_top < row < box_bottom and row == phrase_row:
                # Phrase row: │  phrase  │
                line_segs.append((" " * drift_x, None, None, False))
                line_segs.append(("│  ", border_c, None, False))
                age = min(1.0, (t - self._standby_phrase_time) / 1.5)
                for i, ch in enumerate(phrase):
                    hue = (t * 0.05 + i * 0.02) % 1.0
                    r, g, b = _hsv_to_rgb(hue, 0.4, 0.3 + 0.2 * age)
                    c = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
                    line_segs.append((ch, c, None, False))
                line_segs.append(("  │", border_c, None, False))
                pad_r = term_w - drift_x - box_w
                if pad_r > 0:
                    line_segs.append((" " * pad_r, None, None, False))
            elif box_top < row < box_bottom:
                # Empty rows inside box: │          │
                line_segs.append((" " * drift_x, None, None, False))
                line_segs.append(("│" + " " * (box_w - 2) + "│", border_c, None, False))
                pad_r = term_w - drift_x - box_w
                if pad_r > 0:
                    line_segs.append((" " * pad_r, None, None, False))
            elif row == box_top - 2:
                # Zzz above the box
                zzz_x = drift_x + box_w + 1
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
        _heavy = "░▒▓█▌▐"
        _light = "·.  · "
        static_rows: list[str] = []
        for row in range(term_h):
            randoms = random.random
            choice = random.choice
            buf_chars: list[str] = []
            for _ in range(term_w):
                if randoms() < 0.3:
                    buf_chars.append(choice(_heavy))
                else:
                    buf_chars.append(choice(_light))
            static_rows.append("".join(buf_chars))

        # ── Animated ASCII art — antenna with radio waves ──
        # Use only ASCII/single-width chars for reliable rendering
        phase = (t * 2.5) % 4.0
        def _w(ring: int) -> str:
            return "." if (phase - ring) % 4.0 < 1.0 else " "
        w1, w2, w3, w4 = _w(0), _w(1), _w(2), _w(3)

        spinner = "|/-\\"
        spin_ch = spinner[int(t * 8) % len(spinner)]
        dots = "." * ((int(t * 2) % 3) + 1) + " " * (3 - (int(t * 2) % 3) - 1)

        # Fixed-width 33-char art lines — all exactly the same length
        AW = 33
        art = [
            f"        {w4}   {w3}  {w2} {w1}              ",
            f"      {w4}  {w3}  {w2} {w1}   {w1}            ",
            f"    {w4}  {w3}  {w2} {w1}  /    {w1}           ",
            f"  {w4}  {w3}  {w2} {w1} //      {w1} {w2}        ",
            f"    {w3}  {w2} {w1} ///        {w2} {w3}      ",
            f"      {w2}  ////          {w3} {w4}    ",
            f"       {w1}/////                  ",
            f"        ////   *                 ",
            f"         |                       ",
            f"         |                       ",
            f"        /|\\                      ",
            f"       / | \\                     ",
            f"      /  |  \\                    ",
            f"    {w1}  ------- {w1}                  ",
            f"   {w2}           {w2}                  ",
            f"  {w3}             {w3}                  ",
            f" {w4}               {w4}                 ",
            f"                                 ",
            f"{(spin_ch + ' Connecting' + dots):^33}",
        ]

        # After 2 minutes, show a hint asking if the server is running
        elapsed = time.monotonic() - self._connect_wait_start if self._connect_wait_start else 0
        if elapsed >= 120:
            art.append(f"                                 ")
            hint1 = "Is your server running?"
            hint2 = "Still listening for a connection..."
            art.append(f"  {hint1:^29}    ")
            art.append(f"  {hint2:^29}    ")

            # Pulsing minutes counter
            mins = int(elapsed // 60)
            wait_msg = f"Waiting for {mins}m {int(elapsed % 60):02d}s"
            art.append(f"  {wait_msg:^29}    ")

        # Ensure all lines are exactly AW chars
        art = [(line + " " * AW)[:AW] for line in art]

        art_h = len(art)
        mid_row = term_h // 2
        start_row = mid_row - art_h // 2

        # ── Compose: overlay art on static ──
        for row in range(term_h):
            art_row_idx = row - start_row
            if 0 <= art_row_idx < art_h:
                art_line = art[art_row_idx]
                pad_l = max(0, (term_w - AW) // 2)

                # Left static
                left_static = static_rows[row][:pad_l]
                flicker = 0.5 + 0.2 * math.sin(t * 8 + row * 0.7)
                br = int(min(120, 70 * flicker))
                static_c = f"#{br:02x}{br:02x}{br:02x}"
                segs.append((left_static, static_c, None, False))

                # Art portion — batch consecutive chars of same category
                pulse = 0.6 + 0.4 * math.sin(t * 2 + art_row_idx * 0.3)
                art_br = int(120 + 135 * pulse)
                # Precompute colours for this row
                c_br = int(art_br * 0.7)
                metal_c = f"#{min(255, int(c_br * 1.1)):02x}{min(255, int(c_br * 0.8)):02x}{max(0, int(c_br * 0.4)):02x}"
                tip_pulse = 0.5 + 0.5 * math.sin(t * 5)
                tip_br = int(180 + 75 * tip_pulse)
                tip_c = f"#{min(255, tip_br):02x}{min(255, int(tip_br * 0.9)):02x}{max(0, int(tip_br * 0.3)):02x}"
                wave_pulse = 0.3 + 0.7 * math.sin(t * 4 + art_row_idx * 0.5)
                w_br = int(art_br * wave_pulse)
                wave_c = f"#{max(20, int(w_br * 0.3)):02x}{min(255, int(w_br * 1.1)):02x}{min(255, int(w_br * 1.2)):02x}"
                text_c = f"#{art_br:02x}{art_br:02x}{min(255, art_br + 20):02x}"

                # Categorise and batch chars
                def _cat(ch: str) -> int:
                    if ch in "/\\|-":
                        return 1  # metal
                    if ch == "*":
                        return 2  # tip
                    if ch == ".":
                        return 3  # wave dot
                    if ch.isalpha() or ch in ".:!":
                        return 4  # text
                    return 0  # space/other

                cat_colors = {0: None, 1: metal_c, 2: tip_c, 3: wave_c, 4: text_c}
                cat_bold = {0: False, 1: False, 2: True, 3: True, 4: True}

                buf: list[str] = []
                cur_cat = -1
                for ch in art_line:
                    c = _cat(ch)
                    if c != cur_cat and buf:
                        segs.append(("".join(buf), cat_colors.get(cur_cat), None, cat_bold.get(cur_cat, False)))
                        buf = []
                    cur_cat = c
                    buf.append(ch)
                if buf:
                    segs.append(("".join(buf), cat_colors.get(cur_cat), None, cat_bold.get(cur_cat, False)))

                # Right static — fill to term_w
                right_start = pad_l + AW
                right_w = term_w - right_start
                if right_w > 0:
                    right_static = static_rows[row][right_start:term_w]
                    # Pad if static row is shorter
                    if len(right_static) < right_w:
                        right_static += " " * (right_w - len(right_static))
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
