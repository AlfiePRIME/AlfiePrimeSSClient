from __future__ import annotations

import math
import time

from rich.style import Style
from rich.text import Text

try:
    import psutil
    _process = psutil.Process()
except ImportError:
    psutil = None  # type: ignore[assignment]
    _process = None  # type: ignore[assignment]

try:
    from PIL import Image as _PILImage
    import io as _io
except ImportError:
    _PILImage = None  # type: ignore[assignment,misc]
    _io = None  # type: ignore[assignment]

from alfieprime_musiciser.colors import (
    ColorTheme, DEFAULT_SPECTRUM_COLORS, _default_theme,
    _hex_to_rgb, _lerp_color, _rgb_to_hex, _hsv_to_rgb,
)

# ─── Renderer ────────────────────────────────────────────────────────────────

LIGHT_CHARS = ["◉", "◈", "✦", "✧", "◆", "◇", "⬥", "⬦"]


def _rainbow_color(pos: float) -> str:
    h = pos % 1.0
    r, g, b = _hsv_to_rgb(h, 1.0, 1.0)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def _theme_color(pos: float, theme: ColorTheme | None) -> str:
    """Animated color: lerp through the theme's spectrum if album art is present,
    otherwise fall back to full rainbow."""
    if theme is None or theme is _default_theme:
        return _rainbow_color(pos)
    colors = theme.spectrum_colors
    if not colors:
        return _rainbow_color(pos)
    # Map pos (0-1 looping) into the spectrum list with smooth interpolation
    p = pos % 1.0
    idx_f = p * (len(colors) - 1)
    lo = int(idx_f)
    hi = min(lo + 1, len(colors) - 1)
    frac = idx_f - lo
    return _lerp_color(colors[lo], colors[hi], frac)


def render_title_banner(width: int, theme: ColorTheme | None = None) -> Text:
    t = time.time()
    title = " A L F I E P R I M E   M U S I C I Z E R "
    text = Text()

    for i in range(4):
        char = LIGHT_CHARS[int((t * 3 + i) % len(LIGHT_CHARS))]
        color = _theme_color((t * 0.5 + i * 0.1) % 1.0, theme)
        text.append(f" {char}", Style(color=color, bold=True))

    for i, ch in enumerate(title):
        color = _theme_color((t * 0.3 + i * 0.04) % 1.0, theme)
        text.append(ch, Style(color=color, bold=True))

    for i in range(4):
        char = LIGHT_CHARS[int((t * 3 + i + 4) % len(LIGHT_CHARS))]
        color = _theme_color((t * 0.5 + (i + 4) * 0.1) % 1.0, theme)
        text.append(f"{char} ", Style(color=color, bold=True))

    return text


def render_transport_controls(
    is_playing: bool, shuffle: bool = False, repeat_mode: str = "off",
    supported_commands: list[str] | None = None,
    theme: ColorTheme | None = None,
) -> tuple[Text, dict[str, tuple[int, int]]]:
    """Render transport controls, returning (text, {button_name: (col_start, col_end)})."""
    th = theme or _default_theme
    cmds = set(supported_commands or [])
    text = Text()
    buttons: dict[str, tuple[int, int]] = {}

    def _add_button(name: str, label: str, color: str, dim: bool = False) -> None:
        start = text.cell_len
        style = Style(color=color, bold=not dim, dim=dim)
        text.append(f" {label} ", style)
        buttons[name] = (start, text.cell_len)

    # Shuffle
    shuf_color = th.accent if shuffle else th.primary_dim
    _add_button("shuffle", "\u21c4", shuf_color, dim="shuffle" not in cmds and "unshuffle" not in cmds)
    text.append(" ", Style())

    # Previous
    _add_button("previous", "\u23ee", "#aaaaaa", dim="previous" not in cmds)
    text.append(" ", Style())

    # Play / Pause
    if is_playing:
        _add_button("play_pause", "\u23f8", th.accent, dim="pause" not in cmds)
    else:
        _add_button("play_pause", "\u25b6", th.accent, dim="play" not in cmds)
    text.append(" ", Style())

    # Next
    _add_button("next", "\u23ed", "#aaaaaa", dim="next" not in cmds)
    text.append(" ", Style())

    # Repeat
    if repeat_mode == "one":
        rep_label, rep_color = "\u21bb\u00b9", th.accent
    elif repeat_mode == "all":
        rep_label, rep_color = "\u21bb", th.accent
    else:
        rep_label, rep_color = "\u21bb", th.primary_dim
    _add_button("repeat", rep_label, rep_color, dim="repeat_off" not in cmds)

    # Key hints
    text.append("   ", Style())
    text.append("[S]huf ", Style(color="#444444"))
    text.append("[B]ack ", Style(color="#444444"))
    text.append("[P]lay ", Style(color="#444444"))
    text.append("[N]ext ", Style(color="#444444"))
    text.append("[R]epeat ", Style(color="#444444"))
    text.append("[↑↓]Vol", Style(color="#444444"))

    return text, buttons


def render_now_playing(
    title: str, artist: str, album: str,
    progress_ms: int, duration_ms: int, width: int,
    theme: ColorTheme | None = None,
) -> list[Text]:
    th = theme or _default_theme
    t = time.time()
    lines: list[Text] = []

    track_text = title or "No Track"

    line = Text()
    line.append("  \u266b ", Style(color=th.primary, bold=True))
    for i, ch in enumerate(track_text):
        color = _theme_color((t * 0.2 + i * 0.05) % 1.0, theme)
        line.append(ch, Style(color=color, bold=True))
    lines.append(line)

    if artist:
        line = Text()
        line.append("    ", Style())
        line.append(artist, Style(color=th.secondary, bold=True))
        if album:
            line.append(" \u2014 ", Style(color="#666666"))
            line.append(album, Style(color="#888888", italic=True))
        lines.append(line)

    prog_width = max(width - 20, 20)
    ratio = min(progress_ms / duration_ms, 1.0) if duration_ms > 0 else 0.0
    filled = int(ratio * prog_width)
    empty = prog_width - filled

    line = Text()
    line.append("  [", Style(color="#555555"))
    line.append("=" * max(0, filled - 1), Style(color=th.accent))
    if filled > 0:
        line.append(">", Style(color="#ffffff", bold=True))
    line.append("\u2500" * empty, Style(color="#333333"))
    line.append("] ", Style(color="#555555"))

    cur_min, cur_sec = divmod(progress_ms // 1000, 60)
    tot_min, tot_sec = divmod(duration_ms // 1000, 60)
    line.append(f"{cur_min}:{cur_sec:02d}", Style(color=th.accent))
    line.append("/", Style(color="#555555"))
    line.append(f"{tot_min}:{tot_sec:02d}", Style(color="#888888"))
    lines.append(line)

    return lines


def render_spectrum(
    bands: list[float], peaks: list[float], width: int, height: int = 12,
    theme: ColorTheme | None = None,
) -> list[Text]:
    th = theme or _default_theme
    spec_colors = th.spectrum_colors or DEFAULT_SPECTRUM_COLORS
    num_bands = len(bands) if bands else 32
    bar_w = max(1, (width - 4) // num_bands)
    total_bar_width = bar_w * num_bands
    pad_left = max(0, (width - total_bar_width) // 2)
    lines: list[Text] = []
    n_spec = len(spec_colors) - 1
    bg_style = Style(color=th.bg_subtle)
    peak_style = Style(color="#ffffff", bold=True)
    # Precompute per-row: threshold, color, bar/peak strings
    bar_str = "\u2588" * bar_w
    peak_str = "\u2594" * bar_w
    dot_str = "\u00b7" * bar_w
    row_colors = [
        Style(color=spec_colors[min(int((height - 1 - row) / height * n_spec), n_spec)], bold=True)
        for row in range(height)
    ]

    for row in range(height):
        line = Text()
        if pad_left > 0:
            line.append(" " * pad_left)
        threshold = 1.0 - (row + 1) / height
        color_style = row_colors[row]
        peak_threshold = 1.0 - row / height
        inv_height = 1.0 / height

        for b in range(num_bands):
            level = bands[b] if b < len(bands) else 0.0
            peak = peaks[b] if b < len(peaks) else 0.0

            if level > threshold:
                line.append(bar_str, color_style)
            elif abs(peak - peak_threshold) < inv_height:
                line.append(peak_str, peak_style)
            else:
                line.append(dot_str, bg_style)

        lines.append(line)

    return lines


# Per-channel VU peak hold state (survives across frames)
_vu_peaks: dict[str, tuple[float, float]] = {}  # label → (peak_level, peak_time)


def reset_vu_peaks() -> None:
    """Clear VU peak hold state (call on stream stop / reconnect)."""
    _vu_peaks.clear()


def render_vu_meter(
    level: float, width: int, label: str, color: str,
    theme: ColorTheme | None = None,
) -> Text:
    th = theme or _default_theme
    t = time.time()
    meter_width = max(width - 6, 10)

    # ── Apply non-linear scaling so quiet audio still shows movement ──
    # Square root scaling compresses loud signals, expands quiet ones
    display_level = math.sqrt(min(max(level, 0.0), 1.0))
    filled = min(int(display_level * meter_width), meter_width)

    # ── Peak hold: sticky marker that slowly falls ──
    peak_key = label
    prev_peak, prev_time = _vu_peaks.get(peak_key, (0.0, 0.0))
    if display_level >= prev_peak:
        peak_val, peak_time = display_level, t
    else:
        # Hold for 0.6s then fall
        hold = 0.6
        elapsed = t - prev_time
        if elapsed < hold:
            peak_val, peak_time = prev_peak, prev_time
        else:
            fall_rate = 1.5  # units/sec
            peak_val = max(display_level, prev_peak - (elapsed - hold) * fall_rate)
            peak_time = prev_time
    _vu_peaks[peak_key] = (peak_val, peak_time)
    peak_pos = min(int(peak_val * meter_width), meter_width - 1)

    text = Text()
    text.append(f" {label} ", Style(color=color, bold=True))
    text.append("\u2590", Style(color="#444444"))

    for i in range(meter_width):
        ratio = i / max(meter_width - 1, 1)
        if i == peak_pos and peak_val > 0.01:
            # Peak marker — bright white/yellow flash
            flash = 0.7 + 0.3 * math.sin(t * 8)
            br = int(255 * flash)
            text.append("\u2588", Style(color=f"#{br:02x}{br:02x}{min(255, br):02x}", bold=True))
        elif i < filled:
            # Smooth gradient across full width with shimmer
            if ratio < 0.4:
                c = _lerp_color(th.accent, th.warm, ratio / 0.4)
            elif ratio < 0.7:
                c = _lerp_color(th.warm, th.primary, (ratio - 0.4) / 0.3)
            elif ratio < 0.85:
                c = _lerp_color(th.primary, th.highlight, (ratio - 0.7) / 0.15)
            else:
                c = _lerp_color(th.highlight, "#ff2222", (ratio - 0.85) / 0.15)
            # Subtle shimmer on active bars
            shimmer = 0.85 + 0.15 * math.sin(t * 6 + i * 0.4)
            cr, cg, cb = _hex_to_rgb(c)
            cr = min(255, int(cr * shimmer))
            cg = min(255, int(cg * shimmer))
            cb = min(255, int(cb * shimmer))
            text.append("\u2588", Style(color=_rgb_to_hex(cr, cg, cb)))
        else:
            # Dark background with faint gradient hint
            bg_bright = 0.06 + 0.04 * ratio
            v = int(255 * bg_bright)
            text.append("\u2591", Style(color=f"#{v:02x}{v:02x}{v:02x}"))

    text.append("\u258c", Style(color="#444444"))
    return text


def render_volume_gauge(
    volume: int, muted: bool, width: int, height: int = 2,
    theme: ColorTheme | None = None,
) -> list[Text]:
    """Render an animated volume gauge with arc-style dial and level indicator."""
    th = theme or _default_theme
    t = time.time()
    lines: list[Text] = []
    vol = max(0, min(100, volume))
    ratio = vol / 100.0

    # ── Row 1: Arc dial ──
    # Layout: " VOL ╭───dial───╮" row1, "     ╰───dial───╯ 100%" row2
    # Row1: 5 + 1 + dial_w + 1 = dial_w + 7
    # Row2: 5 + 1 + dial_w + 1 + 5 = dial_w + 12   (longest due to " 100%")
    dial_w = max(width - 14, 8)
    needle_pos = int(ratio * (dial_w - 1))

    line = Text()
    line.append(" VOL ", Style(color=th.warm, bold=True))
    line.append("╭", Style(color="#555555"))

    for i in range(dial_w):
        tick_ratio = i / max(dial_w - 1, 1)
        if i == needle_pos and not muted:
            # Needle — pulsing brightness
            pulse = 0.7 + 0.3 * math.sin(t * 4)
            br = int(255 * pulse)
            c = f"#{br:02x}{br:02x}{min(255, br + 40):02x}"
            line.append("▼", Style(color=c, bold=True))
        elif tick_ratio <= ratio and not muted:
            # Filled portion — fixed green → yellow → orange → red
            if tick_ratio < 0.5:
                c = _lerp_color("#00cc00", "#cccc00", tick_ratio * 2)
            elif tick_ratio < 0.8:
                c = _lerp_color("#cccc00", "#ff8800", (tick_ratio - 0.5) / 0.3)
            else:
                c = _lerp_color("#ff8800", "#ff2222", (tick_ratio - 0.8) / 0.2)
            line.append("━", Style(color=c))
        else:
            line.append("─", Style(color="#333333"))

    line.append("╮", Style(color="#555555"))
    lines.append(line)

    # ── Row 2: Scale markings + percentage ──
    line2 = Text()
    line2.append("     ", Style())  # align with "VOL " above
    line2.append("╰", Style(color="#555555"))

    # Scale ticks at 0, 25, 50, 75, 100
    scale_chars: list[tuple[str, str]] = []
    for i in range(dial_w):
        tick_ratio = i / max(dial_w - 1, 1)
        pct = int(tick_ratio * 100)
        if pct in (0, 25, 50, 75, 100) and abs(tick_ratio * (dial_w - 1) - i) < 0.5:
            scale_chars.append(("┼", "#666666"))
        else:
            scale_chars.append(("─", "#333333"))

    for ch, c in scale_chars:
        line2.append(ch, Style(color=c))
    line2.append("╯", Style(color="#555555"))

    # Volume percentage / muted indicator
    if muted:
        # Flashing mute indicator
        flash = int(t * 3) % 2 == 0
        if flash:
            line2.append(" MUTE", Style(color="#ff2222", bold=True))
        else:
            line2.append(" MUTE", Style(color="#661111", bold=True))
    else:
        vol_color = "#00cc00" if vol < 50 else ("#ff8800" if vol < 80 else "#ff2222")
        line2.append(f" {vol}%", Style(color=vol_color, bold=True))

    lines.append(line2)

    # ── Extra rows if height > 2: animated level bar ──
    if height > 2:
        bar_w = max(width - 4, 10)
        filled = int(ratio * bar_w) if not muted else 0
        bar = Text()
        bar.append("  ", Style())
        for i in range(bar_w):
            if i < filled:
                tick_r = i / max(bar_w - 1, 1)
                # Animated shimmer
                shimmer = 0.7 + 0.3 * math.sin(t * 6 + i * 0.3)
                if tick_r < 0.5:
                    base = _lerp_color("#00cc00", "#cccc00", tick_r * 2)
                elif tick_r < 0.8:
                    base = _lerp_color("#cccc00", "#ff8800", (tick_r - 0.5) / 0.3)
                else:
                    base = _lerp_color("#ff8800", "#ff2222", (tick_r - 0.8) / 0.2)
                # Apply shimmer to brightness
                br, bg, bb = _hex_to_rgb(base)
                br = int(min(255, br * shimmer))
                bg = int(min(255, bg * shimmer))
                bb = int(min(255, bb * shimmer))
                bar.append("█", Style(color=_rgb_to_hex(br, bg, bb)))
            else:
                bar.append("░", Style(color="#1a1a1a"))
        lines.append(bar)

    return lines


def render_party_lights(width: int, vu_left: float, vu_right: float) -> Text:
    t = time.time()
    avg_level = (vu_left + vu_right) / 2.0
    text = Text()
    num_lights = max(width, 10)

    for i in range(num_lights):
        char_idx = int((t * 4 + i * 0.7) % len(LIGHT_CHARS))
        char = LIGHT_CHARS[char_idx]

        hue = (t * 0.3 + i / num_lights) % 1.0
        brightness = 0.3 + 0.7 * avg_level
        pulse = 0.5 + 0.5 * math.sin(t * 6 + i * 0.8)
        brightness *= 0.7 + 0.3 * pulse

        r, g, b = _hsv_to_rgb(hue, 1.0, min(brightness, 1.0))
        color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        text.append(char, Style(color=color, bold=True))

    return text


def render_stereo_lights(width: int, vu_left: float, vu_right: float) -> Text:
    t = time.time()
    text = Text()
    center_str = " ◈◈ "
    half = max((width - len(center_str)) // 2, 4)

    for i in range(half):
        dist_from_center = (half - i) / half
        intensity = min(max(0, vu_left - dist_from_center * 0.5) * 2, 1.0)
        hue = (t * 0.2 + i * 0.03) % 1.0
        r, g, b = _hsv_to_rgb(hue, 1.0, 0.2 + 0.8 * intensity)
        color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        text.append("●" if intensity > 0.3 else "○", Style(color=color))

    text.append(center_str, Style(color=_rainbow_color(t * 0.5), bold=True))

    for i in range(half):
        dist_from_center = i / half
        intensity = min(max(0, vu_right - dist_from_center * 0.5) * 2, 1.0)
        hue = (t * 0.2 + (half + i) * 0.03) % 1.0
        r, g, b = _hsv_to_rgb(hue, 1.0, 0.2 + 0.8 * intensity)
        color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        text.append("●" if intensity > 0.3 else "○", Style(color=color))

    return text


def render_braille_art(
    image_data: bytes, width: int, height: int,
    theme: ColorTheme | None = None,
) -> list[Text]:
    """Convert raw JPEG image bytes into colored Unicode braille art for terminal display.

    Each braille character encodes a 2x4 pixel grid, so the image is resized to
    width*2 x height*4 pixels.  Brightness determines dot pattern; color is sampled
    from the centre of each 2x4 block of the original RGB image.
    """
    if _PILImage is None or _io is None:
        return []

    try:
        img = _PILImage.open(_io.BytesIO(image_data))
    except Exception:
        return []

    px_w = width * 2
    px_h = height * 4
    img_rgb = img.resize((px_w, px_h), _PILImage.LANCZOS).convert("RGB")
    img_gray = img_rgb.convert("L")

    rgb_pixels = img_rgb.load()
    gray_pixels = img_gray.load()

    # Braille dot bit offsets for a 2-wide x 4-tall grid
    # (col, row) -> bit
    _dot_bits = {
        (0, 0): 0x01, (1, 0): 0x08,
        (0, 1): 0x02, (1, 1): 0x10,
        (0, 2): 0x04, (1, 2): 0x20,
        (0, 3): 0x40, (1, 3): 0x80,
    }

    lines: list[Text] = []
    threshold = 128

    for by in range(height):
        line = Text()
        for bx in range(width):
            # Pixel origin for this braille cell
            ox = bx * 2
            oy = by * 4

            # Build braille codepoint from luminance
            code = 0
            for (dx, dy), bit in _dot_bits.items():
                px = ox + dx
                py = oy + dy
                if px < px_w and py < px_h and gray_pixels[px, py] >= threshold:
                    code |= bit

            char = chr(0x2800 + code)

            # Sample colour from centre of the 2x4 block
            cx = min(ox + 1, px_w - 1)
            cy = min(oy + 2, px_h - 1)
            r, g, b = rgb_pixels[cx, cy]
            color = f"#{r:02x}{g:02x}{b:02x}"

            line.append(char, Style(color=color))
        lines.append(line)

    return lines


def render_party_scene(
    width: int, vu_left: float, vu_right: float,
    beat_count: int = 0, beat_intensity: float = 0.0,
    theme: ColorTheme | None = None, height: int = 4,
    bpm: float = 0.0,
) -> list[Text]:
    """Render an ASCII art party scene with a DJ and dancing crowd, synced to beat.

    Height is variable: 3 rows minimum (dancers) + 1 floor line.
    Extra rows add more crowd depth with offset dancers.
    """
    t = time.time()
    avg_level = (vu_left + vu_right) / 2.0
    bounce = beat_count % 4  # animation frame driven by detected beats

    # DJ frames (3 rows each) - head bobbing while mixing
    dj_w = 9
    dj_frames = [
        [r" o/ ___|", r"/|  |==|", r"/|\ ~~~~"],
        [r"  o ___|", r" /| |==|", r"/|  ~~~~"],
        [r"\o/ ___|", r" |  |==|", r" |\ ~~~~"],
        [r" o\ ___|", r" |\ |==|", r"  |\ ~~~"],
    ]

    # Dancer frames (3 rows each) - 4 different poses
    dancer_a = [
        [" o/", "/| ", "/ \\"],
        ["\\o ", " |\\", "/ \\"],
        ["\\o/", " | ", "/ \\"],
        [" o ", "/|\\", "/ \\"],
    ]

    # Jumper frames (3 rows) - jumping up and down
    dancer_b = [
        ["\\o/", " | ", "/ \\"],
        ["_o_", " | ", "| |"],
        ["\\o/", " | ", "/ \\"],
        [" o ", "-|-", "/ \\"],
    ]

    # Headbanger (4 frames x 3 rows each)
    dancer_c = [
        [" o ", "/|\\", "/ \\"],
        [" o ", "/|\\", "/ \\"],
        ["\\o ", " |\\", "/ \\"],
        [" o/", "/| ", "/ \\"],
    ]

    # Spinner (4 frames x 3 rows each)
    dancer_d = [
        ["\\o/", " | ", "< >"],
        [" o>", " | ", " >\\"],
        ["/o\\", " | ", "< >"],
        ["<o ", " | ", "/< "],
    ]

    # Robot (4 frames x 3 rows each)
    dancer_e = [
        ["[o]", "/|\\", "| |"],
        ["[o]", "\\|/", "/ \\"],
        ["[o]", "-|-", "| |"],
        ["[o]", "_|_", "\\ /"],
    ]

    # Raver with glowsticks (4 frames x 3 rows each)
    dancer_f = [
        ["*o*", "/|\\", "/ \\"],
        ["°o°", "\\|/", "\\ /"],
        ["*o*", "/|\\", ">< "],
        ["°o°", "\\|/", "/ \\"],
    ]

    # Energy-reactive dancer selection
    energy = min(1.0, avg_level + beat_intensity * 0.5)
    if energy < 0.3:
        dancer_pool = [dancer_a, dancer_b]
    elif energy <= 0.6:
        dancer_pool = [dancer_a, dancer_b, dancer_c, dancer_d]
    else:
        dancer_pool = [dancer_a, dancer_b, dancer_c, dancer_d, dancer_e, dancer_f]

    scene_width = max(width, 40)
    lines: list[Text] = []
    beat_hue_offset = beat_intensity * 0.08

    # ── BPM meter row at the top ──
    bpm_line = Text()
    if bpm > 0:
        bpm_str = f"BPM:{bpm:5.1f}"
    else:
        bpm_str = "BPM: ---"
    for ci, ch in enumerate(bpm_str):
        color = _theme_color((t * 0.3 + ci * 0.08) % 1.0, theme)
        bpm_line.append(ch, Style(color=color, bold=True))
    # Pad the rest with floor-style animation
    pad_start = len(bpm_str) + 1
    bpm_line.append(" ", Style())
    for i in range(pad_start, scene_width):
        hue = (t * 0.08 + i / scene_width + beat_hue_offset) % 1.0
        brightness = 0.12 + 0.15 * avg_level
        pulse = 0.5 + 0.5 * math.sin(t * 3 + i * 0.6)
        brightness *= 0.6 + 0.4 * pulse
        r, g, b = _hsv_to_rgb(hue, 0.6, min(brightness, 0.4))
        color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        bpm_line.append("░", Style(color=color))
    lines.append(bpm_line)

    # Reserve 1 row for the floor line, 1 for BPM; remaining for dancer groups
    dancer_rows = max(height - 2, 3)
    # Each dancer group is 3 rows tall
    num_groups = max(1, dancer_rows // 3)

    dj = dj_frames[bounce % 4]

    for group_idx in range(num_groups):
        # Offset phase per group so back rows look different
        group_phase_offset = group_idx * 2

        for row_idx in range(3):
            text = Text()
            line_chars: list[str] = []

            # DJ booth on the first (front) group
            if group_idx == 0:
                dj_line = dj[row_idx] if row_idx < len(dj) else ""
                dj_line = dj_line.ljust(dj_w)
                line_chars.append(dj_line)
                crowd_start = dj_w
            else:
                # Back rows: indent slightly for depth effect
                indent = min(group_idx * 2, 6)
                line_chars.append(" " * indent)
                crowd_start = indent

            remaining = scene_width - crowd_start
            pos = 0
            dancer_idx = group_phase_offset
            while pos < remaining - 4:
                phase = (bounce + dancer_idx) % 4
                src = dancer_pool[dancer_idx % len(dancer_pool)][phase]
                d_line = src[row_idx] if row_idx < len(src) else "   "
                line_chars.append(d_line.ljust(5))
                pos += 5
                dancer_idx += 1

            full_line = "".join(line_chars)[:scene_width]

            # Colorize — dimmer for back rows to create depth
            depth_dim = max(0.5, 1.0 - group_idx * 0.15)
            for i, ch in enumerate(full_line):
                if ch in ('o', 'O'):
                    hue = (t * 0.15 + i * 0.015 + beat_hue_offset) % 1.0
                    r, g, b = _hsv_to_rgb(hue, 0.35, 0.85 * depth_dim)
                    color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
                    text.append(ch, Style(color=color))
                elif ch in ('/', '\\', '|', '-'):
                    hue = (t * 0.15 + i * 0.015 + beat_hue_offset) % 1.0
                    brightness = (0.3 + 0.4 * avg_level + beat_intensity * 0.1) * depth_dim
                    r, g, b = _hsv_to_rgb(hue, 0.5, min(brightness, 0.8))
                    color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
                    text.append(ch, Style(color=color))
                elif ch in ('=', '_', '~'):
                    _th = theme or _default_theme
                    text.append(ch, Style(color=_th.secondary))
                else:
                    text.append(ch, Style(color="#555555"))

            lines.append(text)

    # Fill any leftover rows (dancer_rows not divisible by 3) with floor effect
    extra = dancer_rows - num_groups * 3
    for ei in range(extra):
        filler = Text()
        for i in range(scene_width):
            hue = (t * 0.08 + i / scene_width + ei * 0.1 + beat_hue_offset) % 1.0
            brightness = 0.15 + 0.3 * avg_level
            pulse = 0.5 + 0.5 * math.sin(t * 3 + i * 0.5 + ei)
            brightness *= 0.6 + 0.4 * pulse
            r, g, b = _hsv_to_rgb(hue, 0.6, min(brightness, 0.6))
            color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
            filler.append("░", Style(color=color))
        lines.append(filler)

    # Animated dance floor line - gentle colour drift
    floor = Text()
    for i in range(scene_width):
        hue = (t * 0.1 + i / scene_width + beat_hue_offset) % 1.0
        brightness = 0.25 + 0.5 * avg_level
        pulse = 0.5 + 0.5 * math.sin(t * 4 + i * 0.4)
        brightness *= 0.7 + 0.3 * pulse
        r, g, b = _hsv_to_rgb(hue, 0.6, min(brightness, 0.8))
        color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        floor.append("▁", Style(color=color))
    lines.append(floor)

    return lines


def render_server_info(
    server_name: str, group: str, connected: bool,
    theme: ColorTheme | None = None,
) -> Text:
    th = theme or _default_theme
    text = Text()
    if connected:
        text.append(" ⚡ ", Style(color=th.accent, bold=True))
        text.append(server_name, Style(color=th.secondary))
        if group:
            text.append(" │ ", Style(color="#444444"))
            text.append(group, Style(color=th.warm))
    else:
        t = time.time()
        # Pulsing antenna icon while waiting
        pulse = "📡" if int(t * 2) % 2 == 0 else "⚡"
        text.append(f" {pulse} ", Style(color="#ff6600", bold=True))
        text.append(server_name or "Waiting for server...", Style(color="#ff6600", italic=True))
    return text


def render_codec_info(
    codec: str, sample_rate: int, bit_depth: int,
    theme: ColorTheme | None = None,
) -> Text:
    th = theme or _default_theme
    text = Text()
    text.append(" ♪ ", Style(color="#888888"))
    text.append(codec.upper(), Style(color=th.secondary))
    text.append(f" {sample_rate // 1000}kHz", Style(color="#888888"))
    text.append(f" {bit_depth}bit", Style(color="#888888"))
    return text


# Smoothed stats to avoid jitter (updated lazily)
_stats_cache: dict[str, object] = {"last_update": 0.0, "cpu": 0.0, "mem": 0.0, "net_rx": 0, "net_tx": 0, "net_prev_rx": 0, "net_prev_tx": 0, "net_time": 0.0, "uptime": 0.0}
_stats_start_time: float = time.monotonic()


def render_stats_info(theme: ColorTheme | None = None) -> Text:
    """Render system stats: CPU, memory, network, uptime."""
    th = theme or _default_theme
    t_now = time.monotonic()
    text = Text()
    cache = _stats_cache

    # Update stats every 0.5s to avoid overhead
    if _process is not None and t_now - float(cache["last_update"]) > 0.5:
        try:
            cache["cpu"] = _process.cpu_percent(interval=None)
            mem_info = _process.memory_info()
            cache["mem"] = mem_info.rss / (1024 * 1024)  # MB

            # Network I/O (system-wide — per-process not available on all platforms)
            net = psutil.net_io_counters()
            if net:
                now_rx, now_tx = net.bytes_recv, net.bytes_sent
                dt = t_now - float(cache["net_time"]) if float(cache["net_time"]) > 0 else 1.0
                if dt > 0:
                    cache["net_rx"] = int((now_rx - int(cache["net_prev_rx"])) / dt) if int(cache["net_prev_rx"]) > 0 else 0
                    cache["net_tx"] = int((now_tx - int(cache["net_prev_tx"])) / dt) if int(cache["net_prev_tx"]) > 0 else 0
                cache["net_prev_rx"] = now_rx
                cache["net_prev_tx"] = now_tx
                cache["net_time"] = t_now
        except Exception:
            pass
        cache["last_update"] = t_now

    uptime_s = t_now - _stats_start_time
    hours = int(uptime_s // 3600)
    mins = int((uptime_s % 3600) // 60)
    secs = int(uptime_s % 60)

    # CPU
    cpu_val = float(cache.get("cpu", 0))
    cpu_color = "#00cc00" if cpu_val < 30 else ("#ff8800" if cpu_val < 70 else "#ff2222")
    text.append(" CPU:", Style(color="#666666"))
    text.append(f"{cpu_val:4.1f}%", Style(color=cpu_color))

    # Memory
    mem_val = float(cache.get("mem", 0))
    text.append("  MEM:", Style(color="#666666"))
    text.append(f"{mem_val:.0f}MB", Style(color=th.secondary))

    # Network throughput
    rx_bytes = int(cache.get("net_rx", 0))
    tx_bytes = int(cache.get("net_tx", 0))

    def _fmt_rate(b: int) -> str:
        if b > 1024 * 1024:
            return f"{b / (1024 * 1024):.1f}MB/s"
        elif b > 1024:
            return f"{b / 1024:.0f}KB/s"
        return f"{b}B/s"

    text.append("  NET:", Style(color="#666666"))
    text.append(f"↓{_fmt_rate(rx_bytes)}", Style(color=th.accent))
    text.append(f" ↑{_fmt_rate(tx_bytes)}", Style(color=th.warm))

    # Uptime
    text.append("  UP:", Style(color="#666666"))
    if hours > 0:
        text.append(f"{hours}h{mins:02d}m", Style(color="#888888"))
    else:
        text.append(f"{mins}m{secs:02d}s", Style(color="#888888"))

    return text
