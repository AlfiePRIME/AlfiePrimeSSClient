from __future__ import annotations

import logging
from dataclasses import dataclass, field

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# ─── Album Art Color Theme ────────────────────────────────────────────────────


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _color_brightness(r: int, g: int, b: int) -> float:
    """Perceived brightness (0-255) using luminance formula."""
    return 0.299 * r + 0.587 * g + 0.114 * b


def _color_saturation(r: int, g: int, b: int) -> float:
    """Return saturation 0-1."""
    mx = max(r, g, b)
    mn = min(r, g, b)
    return (mx - mn) / mx if mx > 0 else 0.0


def _boost_color(r: int, g: int, b: int, min_brightness: int = 80) -> tuple[int, int, int]:
    """Ensure a color is bright enough for terminal display."""
    br = _color_brightness(r, g, b)
    if br < min_brightness and br > 0:
        factor = min_brightness / br
        r = min(255, int(r * factor))
        g = min(255, int(g * factor))
        b = min(255, int(b * factor))
    return r, g, b


def _lerp_color(hex1: str, hex2: str, t: float) -> str:
    """Linearly interpolate between two hex colors."""
    r1, g1, b1 = _hex_to_rgb(hex1)
    r2, g2, b2 = _hex_to_rgb(hex2)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return _rgb_to_hex(r, g, b)


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[float, float, float]:
    if s == 0.0:
        return v, v, v
    i = int(h * 6.0)
    f = (h * 6.0) - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i %= 6
    if i == 0:
        return v, t, p
    if i == 1:
        return q, v, p
    if i == 2:
        return p, v, t
    if i == 3:
        return p, q, v
    if i == 4:
        return t, p, v
    return v, p, q


@dataclass
class ColorTheme:
    """Dynamic color theme extracted from album art."""

    # Primary colors extracted from artwork (6 slots)
    primary: str = "#ff00ff"       # dominant color → borders, accents
    secondary: str = "#00ccff"     # second most dominant → text highlights
    accent: str = "#00ff88"        # third → active buttons, progress bar
    warm: str = "#ffaa00"          # fourth → warm accents (group name, etc.)
    highlight: str = "#ff6644"     # fifth → extra variety
    cool: str = "#8855ff"          # sixth → extra variety
    # Derived colors
    primary_dim: str = "#666666"   # dimmed variant of primary
    bg_subtle: str = "#1a1a1a"     # subtle background tint

    # Spectrum gradient (16 colors) - generated from primary→accent→secondary
    spectrum_colors: list[str] = field(default_factory=list)

    # Panel border styles
    border_title: str = "bright_magenta"
    border_now_playing: str = "bright_cyan"
    border_spectrum: str = "bright_green"
    border_vu: str = "bright_yellow"
    border_party: str = "bright_magenta"
    border_dance: str = "bright_yellow"

    def __post_init__(self) -> None:
        if not self.spectrum_colors:
            self.spectrum_colors = list(DEFAULT_SPECTRUM_COLORS)


# Default theme (used when no album art is available)
DEFAULT_SPECTRUM_COLORS = [
    "#00ff00", "#33ff00", "#66ff00", "#99ff00", "#ccff00",
    "#ffff00", "#ffcc00", "#ff9900", "#ff6600", "#ff3300",
    "#ff0000", "#ff0033", "#ff0066", "#ff0099", "#ff00cc",
    "#ff00ff",
]

_default_theme = ColorTheme()


def _extract_theme_from_image(image_data: bytes) -> ColorTheme | None:
    """Extract a color theme from album art image bytes."""
    if Image is None:
        return None
    try:
        import io
        img = Image.open(io.BytesIO(image_data))
        # Resize to small image for fast color quantization
        img = img.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
        # Quantize to extract dominant colors
        quantized = img.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette()
        if palette is None:
            return None

        # Get color frequency to sort by dominance
        pixel_counts = sorted(
            quantized.getcolors(maxcolors=8) or [],
            key=lambda x: x[0],
            reverse=True,
        )

        # Extract top colors, filtering out very dark and very desaturated ones
        candidates: list[tuple[int, int, int]] = []
        for _count, idx in pixel_counts:
            r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
            br = _color_brightness(r, g, b)
            sat = _color_saturation(r, g, b)
            # Skip very dark colors and near-grays
            if br > 30 and (sat > 0.15 or br > 150):
                candidates.append((r, g, b))

        if not candidates:
            return None

        # Sort by saturation * brightness to prefer vivid colors
        candidates.sort(key=lambda c: _color_saturation(*c) * _color_brightness(*c), reverse=True)

        # When we have fewer than 6 distinct colors, generate extras by
        # shifting the hue of existing ones so the theme stays vibrant.
        n_originals = len(candidates)
        while len(candidates) < 6:
            # Cycle through original colors as base for interpolation
            base = candidates[(len(candidates) - n_originals) % n_originals]
            br, bg, bb = base
            # Convert to HSV, shift hue, convert back
            mx = max(br, bg, bb)
            mn = min(br, bg, bb)
            diff = mx - mn
            if diff == 0:
                h = 0.0
            elif mx == br:
                h = ((bg - bb) / diff) % 6
            elif mx == bg:
                h = (bb - br) / diff + 2
            else:
                h = (br - bg) / diff + 4
            h /= 6.0
            s = diff / mx if mx > 0 else 0.0
            v = mx / 255.0
            # Small hue shifts to stay in the same colour family
            shift = 0.06 + 0.05 * len(candidates)  # ~0.11, 0.16, 0.21
            new_h = (h + shift) % 1.0
            new_s = min(1.0, max(0.2, s + 0.1 * (1 - len(candidates) % 2 * 2)))  # nudge sat up/down
            new_v = min(1.0, max(0.35, v + 0.12 * (len(candidates) % 2 * 2 - 1)))
            nr, ng, nb = _hsv_to_rgb(new_h, new_s, new_v)
            candidates.append((int(nr * 255), int(ng * 255), int(nb * 255)))

        # Pick the top 6 most vivid colors
        primary = _boost_color(*candidates[0])
        secondary = _boost_color(*candidates[1])
        accent = _boost_color(*candidates[2])
        warm = _boost_color(*candidates[3])
        highlight = _boost_color(*candidates[4])
        cool = _boost_color(*candidates[5])

        primary_hex = _rgb_to_hex(*primary)
        secondary_hex = _rgb_to_hex(*secondary)
        accent_hex = _rgb_to_hex(*accent)
        warm_hex = _rgb_to_hex(*warm)
        highlight_hex = _rgb_to_hex(*highlight)
        cool_hex = _rgb_to_hex(*cool)

        # Generate spectrum gradient through all 6 colors
        anchors = [accent_hex, warm_hex, highlight_hex, primary_hex, cool_hex, secondary_hex]
        spectrum = []
        for i in range(16):
            t = i / 15.0
            seg = t * (len(anchors) - 1)
            lo = int(seg)
            hi = min(lo + 1, len(anchors) - 1)
            frac = seg - lo
            spectrum.append(_lerp_color(anchors[lo], anchors[hi], frac))

        # Dim variant of primary
        pr, pg, pb = primary
        primary_dim = _rgb_to_hex(max(30, pr // 3), max(30, pg // 3), max(30, pb // 3))

        return ColorTheme(
            primary=primary_hex,
            secondary=secondary_hex,
            accent=accent_hex,
            warm=warm_hex,
            highlight=highlight_hex,
            cool=cool_hex,
            primary_dim=primary_dim,
            bg_subtle=_rgb_to_hex(max(10, pr // 12), max(10, pg // 12), max(10, pb // 12)),
            spectrum_colors=spectrum,
            border_title=primary_hex,
            border_now_playing=secondary_hex,
            border_spectrum=accent_hex,
            border_vu=warm_hex,
            border_party=highlight_hex,
            border_dance=cool_hex,
        )
    except Exception:
        logger.debug("Failed to extract theme from album art", exc_info=True)
        return None
