#!/usr/bin/env python3
"""AlfiePRIME Musiciser - A boom box themed SendSpin receiver with audio visualizer.

A party-mode client for Music Assistant (or any SendSpin server).
Advertises via mDNS so servers discover and connect to us automatically.
Displays a retro boom box TUI with real-time spectrum analyzer and party lights.

Usage:
    alfieprime-musiciser                          # Listen + advertise via mDNS (default)
    alfieprime-musiciser --name "MKUltra"         # Custom mDNS name
    alfieprime-musiciser --port 9000              # Custom listen port
    alfieprime-musiciser ws://host:port/sendspin  # Connect to specific server
    alfieprime-musiciser --demo                   # Demo mode (no server needed)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import platform
import random
import signal
import struct
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING

import io as _io
import shutil

import numpy as np
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.style import Style
from rich.table import Table
from rich.text import Text

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from aiosendspin.client import AudioFormat

logger = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".config" / "alfieprime-musiciser"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    """Persistent configuration."""

    client_name: str = "MKUltra"
    mode: str = "listen"  # "listen" (mDNS) or "connect" (explicit URL)
    server_url: str = ""  # only used when mode == "connect"
    listen_port: int = 8928  # only used when mode == "listen"
    client_id: str = ""  # stable ID so Music Assistant remembers this device

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls) -> Config | None:
        if not CONFIG_FILE.exists():
            return None
        try:
            data = json.loads(CONFIG_FILE.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError, OSError):
            return None


def run_setup(console: Console, existing: Config | None = None) -> Config:
    """Interactive first-run setup (or reconfigure)."""
    console.print()
    console.print(Panel(
        "[bold bright_magenta]A L F I E P R I M E   M U S I C I Z E R   S E T U P[/]",
        border_style="bright_cyan",
    ))
    console.print()

    defaults = existing or Config()

    # Client name
    client_name = Prompt.ask(
        "[bright_cyan]Client name[/] (how this player appears in Music Assistant)",
        default=defaults.client_name,
        console=console,
    )

    # Connection mode
    console.print()
    console.print("[bold]Connection mode:[/]")
    console.print("  [bright_green]1[/] - Listen (mDNS) — server discovers and connects to us [dim](recommended)[/dim]")
    console.print("  [bright_green]2[/] - Connect — we connect to a specific server URL")
    console.print()

    default_mode_num = "1" if defaults.mode == "listen" else "2"
    mode_choice = Prompt.ask(
        "[bright_cyan]Choose mode[/]",
        choices=["1", "2"],
        default=default_mode_num,
        console=console,
    )

    mode = "listen" if mode_choice == "1" else "connect"
    server_url = ""
    listen_port = defaults.listen_port

    if mode == "connect":
        console.print()
        console.print("[dim]Enter the SendSpin/Music Assistant WebSocket URL.[/]")
        console.print("[dim]Examples: ws://192.168.1.100:8097/sendspin  or  ws://homeassistant.local:8097/sendspin[/]")
        console.print()
        server_url = Prompt.ask(
            "[bright_cyan]Server URL[/]",
            default=defaults.server_url or "",
            console=console,
        )
        # Normalise: add ws:// if missing
        if server_url and not server_url.startswith(("ws://", "wss://")):
            if ":" in server_url and "/" in server_url:
                server_url = "ws://" + server_url
            else:
                # Bare IP/hostname — add default sendspin port+path
                server_url = f"ws://{server_url}:8097/sendspin"
            console.print(f"[dim]Using URL: {server_url}[/]")
    else:
        console.print()
        listen_port = int(Prompt.ask(
            "[bright_cyan]Listen port[/]",
            default=str(defaults.listen_port),
            console=console,
        ))

    config = Config(
        client_name=client_name,
        mode=mode,
        server_url=server_url,
        listen_port=listen_port,
    )
    config.save()

    console.print()
    console.print(f"[bright_green]Config saved to {CONFIG_FILE}[/]")
    console.print()
    return config

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


@dataclass
class ColorTheme:
    """Dynamic color theme extracted from album art."""

    # Primary colors extracted from artwork
    primary: str = "#ff00ff"       # dominant color → borders, accents
    secondary: str = "#00ccff"     # second most dominant → text highlights
    accent: str = "#00ff88"        # third → active buttons, progress bar
    warm: str = "#ffaa00"          # fourth → warm accents (group name, etc.)
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

        if len(candidates) < 2:
            return None

        # Sort by saturation * brightness to prefer vivid colors
        candidates.sort(key=lambda c: _color_saturation(*c) * _color_brightness(*c), reverse=True)

        # Pick the top 4 most vivid colors
        primary = _boost_color(*candidates[0])
        secondary = _boost_color(*candidates[min(1, len(candidates) - 1)])
        accent = _boost_color(*candidates[min(2, len(candidates) - 1)])
        warm = _boost_color(*candidates[min(3, len(candidates) - 1)])

        primary_hex = _rgb_to_hex(*primary)
        secondary_hex = _rgb_to_hex(*secondary)
        accent_hex = _rgb_to_hex(*accent)
        warm_hex = _rgb_to_hex(*warm)

        # Generate spectrum gradient: accent → warm → primary
        spectrum = []
        for i in range(16):
            t = i / 15.0
            if t < 0.5:
                spectrum.append(_lerp_color(accent_hex, warm_hex, t * 2))
            else:
                spectrum.append(_lerp_color(warm_hex, primary_hex, (t - 0.5) * 2))

        # Dim variant of primary
        pr, pg, pb = primary
        primary_dim = _rgb_to_hex(max(30, pr // 3), max(30, pg // 3), max(30, pb // 3))

        return ColorTheme(
            primary=primary_hex,
            secondary=secondary_hex,
            accent=accent_hex,
            warm=warm_hex,
            primary_dim=primary_dim,
            bg_subtle=_rgb_to_hex(max(10, pr // 12), max(10, pg // 12), max(10, pb // 12)),
            spectrum_colors=spectrum,
            border_title=primary_hex,
            border_now_playing=secondary_hex,
            border_spectrum=accent_hex,
            border_vu=warm_hex,
            border_party=primary_hex,
            border_dance=warm_hex,
        )
    except Exception:
        logger.debug("Failed to extract theme from album art", exc_info=True)
        return None


# ─── Visualizer ──────────────────────────────────────────────────────────────

NUM_BANDS = 32
FFT_SIZE = 2048
RING_BUFFER_SIZE = FFT_SIZE * 4

from collections import deque


class AudioVisualizer:
    """FFT spectrum analyzer - extracts frequency data from raw PCM audio."""

    def __init__(self) -> None:
        self._ring_buffer = np.zeros(RING_BUFFER_SIZE, dtype=np.float32)
        self._write_pos = 0
        self._lock = threading.Lock()
        self._sample_rate = 48000
        self._bit_depth = 16
        self._channels = 2
        self._has_data = False
        self._bands = np.zeros(NUM_BANDS, dtype=np.float64)
        self._peaks = np.zeros(NUM_BANDS, dtype=np.float64)
        self._vu_left = 0.0
        self._vu_right = 0.0
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        # AGC: track recent peak dB to auto-scale spectrum sensitivity
        self._agc_peak_db = -60.0  # current tracked peak level in dB
        self._agc_floor_db = -60.0  # noise floor in dB
        self._agc_attack = 0.3  # how fast gain adapts to louder signals
        self._agc_release = 0.05  # how fast gain relaxes when quieter
        # Beat detection via spectral flux in bass range
        self._beat_count = 0  # increments on each detected beat
        self._beat_intensity = 0.0  # decays after each beat, 1.0 = just hit
        self._beat_cooldown = 0  # frames to wait before next beat detection
        self._prev_bass_spectrum = None  # previous frame's bass FFT bins
        self._flux_history = np.zeros(20, dtype=np.float64)  # ~0.67s at 30fps
        self._flux_hist_pos = 0
        # BPM estimation from beat timestamps
        self._beat_times: deque[float] = deque(maxlen=20)  # last 20 beat timestamps
        self._bpm = 0.0
        # Pause freeze
        self._paused = False
        # Playback-synced delay queue: hold audio until it's time to "play" it
        # Each entry: (mono_samples, vu_left, vu_right, cumulative_sample_count)
        self._delay_queue: deque[tuple[np.ndarray, float, float, int]] = deque()
        self._total_samples_queued = 0  # total mono samples queued since stream start
        self._total_samples_drained = 0  # total mono samples written to ring buffer
        self._stream_start_time = 0.0  # monotonic time of first audio feed
        self._vu_pending_left = 0.0  # VU from decode, applied when queue drains
        self._vu_pending_right = 0.0

    def set_format(self, sample_rate: int, bit_depth: int, channels: int) -> None:
        self._sample_rate = sample_rate
        self._bit_depth = bit_depth
        self._channels = channels

    def feed_audio(self, audio_data: bytes | bytearray) -> None:
        try:
            # _decode_pcm writes to _vu_pending_left/right for queued VU capture
            self._vu_pending_left = 0.0
            self._vu_pending_right = 0.0
            samples = self._decode_pcm(audio_data)
            if samples is None or len(samples) == 0:
                return
        except Exception:
            return

        with self._lock:
            if self._stream_start_time == 0.0:
                self._stream_start_time = time.monotonic()
            self._total_samples_queued += len(samples)
            self._delay_queue.append((samples, self._vu_pending_left, self._vu_pending_right, self._total_samples_queued))

    def _write_to_ring_buffer(self, samples: np.ndarray) -> None:
        """Write mono samples to the ring buffer."""
        n = len(samples)
        buf = self._ring_buffer
        pos = self._write_pos

        if n >= RING_BUFFER_SIZE:
            buf[:] = samples[-RING_BUFFER_SIZE:]
            self._write_pos = 0
        elif pos + n <= RING_BUFFER_SIZE:
            buf[pos : pos + n] = samples[:n]
            self._write_pos = pos + n
        else:
            first = RING_BUFFER_SIZE - pos
            buf[pos:] = samples[:first]
            remaining = n - first
            buf[:remaining] = samples[first : first + remaining]
            self._write_pos = remaining

    def _drain_queue(self) -> None:
        """Release queued audio that should have played by now."""
        if self._stream_start_time <= 0 or not self._delay_queue:
            return
        elapsed = time.monotonic() - self._stream_start_time
        # How many mono samples should have played by now
        playback_samples = int(elapsed * self._sample_rate)

        while self._delay_queue:
            samples, vu_l, vu_r, cum_count = self._delay_queue[0]
            if self._total_samples_drained + len(samples) <= playback_samples:
                self._delay_queue.popleft()
                self._write_to_ring_buffer(samples)
                self._total_samples_drained += len(samples)
                self._vu_left = vu_l
                self._vu_right = vu_r
                self._has_data = True
            else:
                break

    def _decode_pcm(self, data: bytes | bytearray) -> np.ndarray | None:
        bd = self._bit_depth
        ch = self._channels

        if bd == 16:
            dtype = np.int16
            max_val = 32768.0
        elif bd == 32:
            dtype = np.int32
            max_val = 2147483648.0
        elif bd == 24:
            n_samples = len(data) // 3
            if n_samples == 0:
                return None
            arr = np.zeros(n_samples, dtype=np.int32)
            for i in range(n_samples):
                b0 = data[i * 3]
                b1 = data[i * 3 + 1]
                b2 = data[i * 3 + 2]
                val = b0 | (b1 << 8) | (b2 << 16)
                if val & 0x800000:
                    val -= 0x1000000
                arr[i] = val
            samples = arr.astype(np.float32) / 8388608.0
            if ch > 1:
                samples = samples.reshape(-1, ch)
                self._update_vu_raw(samples)
                samples = samples.mean(axis=1)
            return samples
        else:
            return None

        samples_int = np.frombuffer(data, dtype=dtype)
        samples = samples_int.astype(np.float32) / max_val

        if ch > 1 and len(samples) >= ch:
            samples = samples.reshape(-1, ch)
            self._update_vu_raw(samples)
            samples = samples.mean(axis=1)

        return samples

    def _update_vu_raw(self, stereo: np.ndarray) -> None:
        if stereo.shape[1] >= 2:
            self._vu_pending_left = float(np.sqrt(np.mean(stereo[:, 0] ** 2)))
            self._vu_pending_right = float(np.sqrt(np.mean(stereo[:, 1] ** 2)))
        else:
            rms = float(np.sqrt(np.mean(stereo[:, 0] ** 2)))
            self._vu_pending_left = rms
            self._vu_pending_right = rms

    def set_paused(self, paused: bool) -> None:
        """Freeze visualizer output when paused."""
        self._paused = paused

    def get_spectrum(self) -> tuple[list[float], list[float], float, float]:
        # When paused, decay gracefully instead of freezing
        if self._paused:
            self._decay()
            return (self._bands.tolist(), self._peaks.tolist(), self._vu_left, self._vu_right)

        with self._lock:
            # Drain queued audio that matches current playback position
            self._drain_queue()

            if not self._has_data:
                self._decay()
                return (self._bands.tolist(), self._peaks.tolist(), self._vu_left, self._vu_right)

            pos = self._write_pos
            if pos >= FFT_SIZE:
                segment = self._ring_buffer[pos - FFT_SIZE : pos].copy()
            else:
                segment = np.concatenate(
                    [self._ring_buffer[RING_BUFFER_SIZE - (FFT_SIZE - pos) :], self._ring_buffer[:pos]]
                )

        windowed = segment * self._window
        spectrum = np.abs(np.fft.rfft(windowed))

        n_bins = len(spectrum)
        band_levels = np.zeros(NUM_BANDS)

        freq_min = 20.0
        freq_max = self._sample_rate / 2.0
        for i in range(NUM_BANDS):
            f_low = freq_min * (freq_max / freq_min) ** (i / NUM_BANDS)
            f_high = freq_min * (freq_max / freq_min) ** ((i + 1) / NUM_BANDS)
            bin_low = max(1, int(f_low * FFT_SIZE / self._sample_rate))
            bin_high = min(n_bins - 1, int(f_high * FFT_SIZE / self._sample_rate))
            if bin_high > bin_low:
                band_levels[i] = np.mean(spectrum[bin_low:bin_high])
            elif bin_low < n_bins:
                band_levels[i] = spectrum[bin_low]

        band_levels = np.maximum(band_levels, 1e-10)
        db = 20 * np.log10(band_levels)

        # AGC: track the peak dB of current frame and adapt range
        frame_peak_db = float(np.max(db))
        if frame_peak_db > self._agc_peak_db:
            self._agc_peak_db += (frame_peak_db - self._agc_peak_db) * self._agc_attack
        else:
            self._agc_peak_db += (frame_peak_db - self._agc_peak_db) * self._agc_release
        # Don't let AGC peak sit way above actual signal
        self._agc_peak_db = max(self._agc_peak_db, frame_peak_db - 12.0)

        # Dynamic range: use the current frame peak to keep bars responsive
        ceiling = max(frame_peak_db + 3.0, self._agc_peak_db, self._agc_floor_db + 15.0)
        dyn_range = max(ceiling - self._agc_floor_db, 15.0)
        normalized = np.clip((db - self._agc_floor_db) / dyn_range, 0, 1)

        attack = 0.7
        decay = 0.85
        mask = normalized > self._bands
        self._bands = np.where(mask, self._bands * (1 - attack) + normalized * attack, self._bands * decay)

        peak_mask = self._bands > self._peaks
        self._peaks = np.where(peak_mask, self._bands, self._peaks * 0.97)

        # Beat detection via spectral flux (onset detection) in bass range
        bass_bin_low = max(1, int(20 * FFT_SIZE / self._sample_rate))
        bass_bin_high = min(n_bins - 1, int(250 * FFT_SIZE / self._sample_rate))
        bass_spectrum = spectrum[bass_bin_low:bass_bin_high].copy()

        if self._prev_bass_spectrum is not None and len(self._prev_bass_spectrum) == len(bass_spectrum):
            diff = bass_spectrum - self._prev_bass_spectrum
            flux = float(np.sum(np.maximum(diff, 0) ** 2))
        else:
            flux = 0.0
        self._prev_bass_spectrum = bass_spectrum

        idx = self._flux_hist_pos % len(self._flux_history)
        self._flux_history[idx] = flux
        self._flux_hist_pos += 1
        filled = min(self._flux_hist_pos, len(self._flux_history))
        flux_median = float(np.median(self._flux_history[:filled]))

        if self._beat_cooldown > 0:
            self._beat_cooldown -= 1

        # Trigger immediately - low threshold, no delay
        threshold = flux_median * 1.2 + 0.00001
        if flux > threshold and self._beat_cooldown == 0:
            self._beat_count += 1
            self._beat_intensity = 1.0
            self._beat_cooldown = 3  # ~100ms at 30fps
            # Record beat time for BPM estimation
            now = time.monotonic()
            self._beat_times.append(now)
            if len(self._beat_times) >= 4:
                # Average interval over recent beats
                intervals = [
                    self._beat_times[i] - self._beat_times[i - 1]
                    for i in range(1, len(self._beat_times))
                ]
                # Filter outliers (keep intervals within 2x of median)
                intervals.sort()
                median = intervals[len(intervals) // 2]
                valid = [iv for iv in intervals if 0.3 * median < iv < 2.5 * median]
                if valid:
                    avg_interval = sum(valid) / len(valid)
                    if avg_interval > 0:
                        self._bpm = 60.0 / avg_interval

        # Decay beat intensity
        self._beat_intensity *= 0.6

        return (self._bands.tolist(), self._peaks.tolist(), self._vu_left, self._vu_right)

    def _decay(self) -> None:
        self._bands *= 0.9
        self._peaks *= 0.95
        self._vu_left *= 0.85
        self._vu_right *= 0.85

    def get_beat(self) -> tuple[int, float]:
        """Return (beat_count, beat_intensity). Count increments on each beat."""
        return self._beat_count, self._beat_intensity

    def get_bpm(self) -> float:
        """Return estimated BPM from recent beat detection. 0 if unknown."""
        # If no beats in last 3 seconds, BPM is stale
        if self._beat_times and (time.monotonic() - self._beat_times[-1]) > 3.0:
            self._bpm = 0.0
        return self._bpm

    def reset_pipeline(self) -> None:
        """Reset the audio pipeline (ring buffer, delay queue) but keep visual state
        so spectrum/VU can decay naturally."""
        with self._lock:
            self._ring_buffer[:] = 0
            self._write_pos = 0
            self._has_data = False
            self._delay_queue.clear()
            self._total_samples_queued = 0
            self._total_samples_drained = 0
            self._stream_start_time = 0.0

    def reset(self) -> None:
        """Full reset — pipeline + visual state."""
        self.reset_pipeline()
        self._bands[:] = 0
        self._peaks[:] = 0
        self._vu_left = 0.0
        self._vu_right = 0.0
        self._agc_peak_db = -60.0
        self._prev_bass_spectrum = None
        self._flux_history[:] = 0
        self._flux_hist_pos = 0
        self._beat_cooldown = 0
        self._beat_intensity = 0.0
        self._beat_times.clear()
        self._bpm = 0.0


# ─── Renderer ────────────────────────────────────────────────────────────────

LIGHT_CHARS = ["◉", "◈", "✦", "✧", "◆", "◇", "⬥", "⬦"]


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
    text.append("[↑↓]Vol ", Style(color="#444444"))
    text.append("[←→]Seek", Style(color="#444444"))

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

    for row in range(height):
        line = Text()
        if pad_left > 0:
            line.append(" " * pad_left)
        threshold = 1.0 - (row + 1) / height

        for b in range(num_bands):
            level = bands[b] if b < len(bands) else 0.0
            peak = peaks[b] if b < len(peaks) else 0.0

            if level > threshold:
                color = spec_colors[min(int((height - 1 - row) / height * (len(spec_colors) - 1)), len(spec_colors) - 1)]
                line.append("\u2588" * bar_w, Style(color=color, bold=True))
            elif abs(peak - (1.0 - row / height)) < (1.0 / height):
                line.append("\u2594" * bar_w, Style(color="#ffffff", bold=True))
            else:
                line.append("\u00b7" * bar_w, Style(color=th.bg_subtle))

        lines.append(line)

    return lines


def render_vu_meter(
    level: float, width: int, label: str, color: str,
    theme: ColorTheme | None = None,
) -> Text:
    th = theme or _default_theme
    # VU meter gradient: use theme accent → warm → primary for low → mid → high
    vu_low = th.accent
    vu_mid = th.warm
    vu_high = th.primary
    meter_width = max(width - 6, 10)
    filled = min(int(level * meter_width), meter_width)

    text = Text()
    text.append(f" {label} ", Style(color=color, bold=True))
    text.append("\u2590", Style(color="#444444"))

    for i in range(meter_width):
        if i < filled:
            ratio = i / meter_width
            c = vu_low if ratio < 0.6 else (vu_mid if ratio < 0.8 else vu_high)
            text.append("\u2588", Style(color=c))
        else:
            text.append("\u2591", Style(color="#222222"))

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
            # Filled portion — gradient from accent → warm → primary
            if tick_ratio < 0.5:
                c = _lerp_color(th.accent, th.warm, tick_ratio * 2)
            elif tick_ratio < 0.8:
                c = _lerp_color(th.warm, th.primary, (tick_ratio - 0.5) / 0.3)
            else:
                # Red zone
                c = _lerp_color(th.primary, "#ff2222", (tick_ratio - 0.8) / 0.2)
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
        vol_color = th.accent if vol < 50 else (th.warm if vol < 80 else th.primary)
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
                    base = _lerp_color(th.accent, th.warm, tick_r * 2)
                elif tick_r < 0.8:
                    base = _lerp_color(th.warm, th.primary, (tick_r - 0.5) / 0.3)
                else:
                    base = _lerp_color(th.primary, "#ff2222", (tick_r - 0.8) / 0.2)
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
    center_str = " \u25c8\u25c8 "
    half = max((width - len(center_str)) // 2, 4)

    for i in range(half):
        dist_from_center = (half - i) / half
        intensity = min(max(0, vu_left - dist_from_center * 0.5) * 2, 1.0)
        hue = (t * 0.2 + i * 0.03) % 1.0
        r, g, b = _hsv_to_rgb(hue, 1.0, 0.2 + 0.8 * intensity)
        color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        text.append("\u25cf" if intensity > 0.3 else "\u25cb", Style(color=color))

    text.append(center_str, Style(color=_rainbow_color(t * 0.5), bold=True))

    for i in range(half):
        dist_from_center = i / half
        intensity = min(max(0, vu_right - dist_from_center * 0.5) * 2, 1.0)
        hue = (t * 0.2 + (half + i) * 0.03) % 1.0
        r, g, b = _hsv_to_rgb(hue, 1.0, 0.2 + 0.8 * intensity)
        color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        text.append("\u25cf" if intensity > 0.3 else "\u25cb", Style(color=color))

    return text


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

    # DJ frames (3 rows each) - mixing the decks
    # Row 3 includes BPM readout on the deck
    if bpm > 0:
        bpm_str = f"{bpm:5.1f}"
        bpm_deck = f"/|_{bpm_str}|"
    else:
        bpm_deck = r"/|_____|"
    dj_w = max(10, len(bpm_deck) + 2)
    dj_frames = [
        [r" o/ ___|", r"/|  |==|", bpm_deck],
        [r"\o/ ___|", r" |  |==|", bpm_deck],
        [r" \o ___|", r"/|\ |==|", bpm_deck],
        [r"\o/ ___|", r" |  |==|", bpm_deck],
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

    scene_width = max(width, 40)
    # Reserve 1 row for the floor line; remaining rows are for dancer groups
    dancer_rows = max(height - 1, 3)
    # Each dancer group is 3 rows tall; compute how many groups stack vertically
    num_groups = max(1, dancer_rows // 3)
    lines: list[Text] = []
    beat_hue_offset = beat_intensity * 0.08

    for group_idx in range(num_groups):
        dj = dj_frames[(bounce + group_idx) % 4]
        # Offset phase per group so back rows look different
        group_phase_offset = group_idx * 2

        for row_idx in range(3):
            text = Text()
            line_chars: list[str] = []

            # DJ booth only on the first (front) group
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
                if dancer_idx % 2 == 0:
                    src = dancer_a[phase]
                else:
                    src = dancer_b[phase]
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
                    th = theme or _default_theme
                    text.append(ch, Style(color=th.secondary))
                elif ch.isdigit() or ch == '.':
                    # BPM digits — animated colour like title
                    color = _theme_color((t * 0.3 + i * 0.06) % 1.0, theme)
                    text.append(ch, Style(color=color, bold=True))
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
        text.append(" \u26a1 ", Style(color=th.accent, bold=True))
        text.append(server_name, Style(color=th.secondary))
        if group:
            text.append(" \u2502 ", Style(color="#444444"))
            text.append(group, Style(color=th.warm))
    else:
        t = time.time()
        # Pulsing antenna icon while waiting
        pulse = "\U0001f4e1" if int(t * 2) % 2 == 0 else "\u26a1"
        text.append(f" {pulse} ", Style(color="#ff6600", bold=True))
        text.append(server_name or "Waiting for server...", Style(color="#ff6600", italic=True))
    return text


def render_codec_info(
    codec: str, sample_rate: int, bit_depth: int,
    theme: ColorTheme | None = None,
) -> Text:
    th = theme or _default_theme
    text = Text()
    text.append(" \u266a ", Style(color="#888888"))
    text.append(codec.upper(), Style(color=th.secondary))
    text.append(f" {sample_rate // 1000}kHz", Style(color="#888888"))
    text.append(f" {bit_depth}bit", Style(color="#888888"))
    return text


# ─── Player State ────────────────────────────────────────────────────────────


@dataclass
class PlayerState:
    title: str = ""
    artist: str = ""
    album: str = ""
    progress_ms: int = 0
    duration_ms: int = 0
    is_playing: bool = False
    server_name: str = ""
    group_name: str = ""
    connected: bool = False
    codec: str = "pcm"
    sample_rate: int = 48000
    bit_depth: int = 16
    volume: int = 100
    muted: bool = False
    # For progress interpolation between server updates
    playback_speed: float = 1.0  # 1.0 = normal, 0.0 = paused
    progress_update_time: float = 0.0  # monotonic time of last progress update
    # Controller state
    supported_commands: list[str] = field(default_factory=list)
    repeat_mode: str = "off"  # off, one, all
    shuffle: bool = False
    # Color theme from album art
    theme: ColorTheme = field(default_factory=ColorTheme)

    def get_interpolated_progress(self) -> int:
        """Get progress interpolated from last server update."""
        if not self.is_playing or self.progress_update_time <= 0 or self.duration_ms <= 0:
            return self.progress_ms
        elapsed = time.monotonic() - self.progress_update_time
        speed = self.playback_speed if self.playback_speed > 0 else 1.0
        interpolated = self.progress_ms + int(elapsed * 1000 * speed)
        return max(0, min(interpolated, self.duration_ms))


# ─── TUI ─────────────────────────────────────────────────────────────────────

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
        # Seek acceleration tracking
        self._seek_last_time: float = 0.0
        self._seek_hold_count: int = 0

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
        fixed_rows = 1 + 3 + np_rows + 4 + 4 + 3 + 1 + 2 + 2
        available = max(self._term_height - fixed_rows, 8)
        # Dance floor gets a fixed size; spectrum expands to fill all remaining space
        dance_height = max(4, min(7, available // 3))
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

        # Status bar
        status_table = Table.grid(padding=0, expand=True)
        status_table.add_column(ratio=1)
        status_table.add_column(ratio=1)
        status_table.add_row(
            render_server_info(self.state.server_name, self.state.group_name, self.state.connected, theme=th),
            render_codec_info(self.state.codec, self.state.sample_rate, self.state.bit_depth, theme=th),
        )
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
        elif k == "arrow_right":
            # Seek forward — accelerates when held
            now = time.monotonic()
            if now - self._seek_last_time < 0.25:
                self._seek_hold_count += 1
            else:
                self._seek_hold_count = 0
            self._seek_last_time = now
            seek_ms = 5000 + min(self._seek_hold_count, 20) * 2500  # 5s → 55s
            self._fire_command(f"seek_forward:{seek_ms}")
        elif k == "arrow_left":
            now = time.monotonic()
            if now - self._seek_last_time < 0.25:
                self._seek_hold_count += 1
            else:
                self._seek_hold_count = 0
            self._seek_last_time = now
            seek_ms = 5000 + min(self._seek_hold_count, 20) * 2500
            self._fire_command(f"seek_backward:{seek_ms}")

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
            elif data[i:i + 3] == b"\x1b[C":
                self._handle_key("arrow_right")
                i += 3
            elif data[i:i + 3] == b"\x1b[D":
                self._handle_key("arrow_left")
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
        """Generate CRT power-on animation segments.

        progress: 0.0 → 1.0
          0.00-0.15  Black screen, faint hum glow in center
          0.15-0.40  Bright horizontal scanline appears at center
          0.40-0.75  Scanline expands vertically with phosphor bloom
          0.75-1.00  Static/noise clears into content
        """
        segs: list[tuple[str, str | None, str | None, bool]] = []
        mid_row = term_h // 2

        if progress < 0.15:
            # Black with a faint center dot
            dot_brightness = int(progress / 0.15 * 60)
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

        elif progress < 0.40:
            # Bright horizontal scanline at center
            p = (progress - 0.15) / 0.25
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

        elif progress < 0.75:
            # Expanding vertically — phosphor bloom
            p = (progress - 0.40) / 0.35
            visible_half = max(1, int(p * mid_row))
            for row in range(term_h):
                dist = abs(row - mid_row)
                if dist <= visible_half:
                    # Scanline with phosphor glow — brightness fades at edges
                    edge_fade = 1.0 - (dist / max(visible_half, 1)) * 0.6
                    flicker = 0.85 + 0.15 * math.sin(time.time() * 60 + row * 3)
                    br = int(min(255, 200 * edge_fade * flicker))
                    # Slight green/blue phosphor tint
                    r = int(br * 0.8)
                    g = int(min(255, br * 1.0))
                    b = int(min(255, br * 0.9))
                    c = f"#{r:02x}{g:02x}{b:02x}"
                    # Static noise chars
                    line = "".join(
                        random.choice("░▒▓█▌▐─━╌╍")
                        if random.random() < (1.0 - p) * 0.6
                        else random.choice("·. ")
                        for _ in range(term_w)
                    )
                    segs.append((line, c, None, False))
                else:
                    segs.append((" " * term_w, None, None, False))
                if row < term_h - 1:
                    segs.append(("\n", None, None, False))

        else:
            # Static clearing — render real content with noise overlay fading out
            p = (progress - 0.75) / 0.25  # 0→1
            real_segs = self._render_frame_gui()
            noise_chance = (1.0 - p) * 0.4
            for text, fg, bg, bold in real_segs:
                if "\n" in text or not text.strip():
                    segs.append((text, fg, bg, bold))
                    continue
                # Randomly replace some chars with static
                out = []
                for ch in text:
                    if random.random() < noise_chance and ch.strip():
                        out.append(random.choice("░▒▓"))
                    else:
                        out.append(ch)
                segs.append(("".join(out), fg, bg, bold))

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

            # ── CRT startup animation ──
            duration = 1.8
            start = time.monotonic()
            while time.monotonic() - start < duration:
                progress = (time.monotonic() - start) / duration
                segs = self._crt_startup_segments(progress, term_w, term_h)
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
            # ── CRT startup animation ──
            duration = 1.8
            start = time.monotonic()
            while time.monotonic() - start < duration and gui.alive:
                gui.process_events()
                progress = (time.monotonic() - start) / duration
                term_w, term_h = self._get_terminal_size()
                segs = self._crt_startup_segments(progress, term_w, term_h)
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


# ─── SendSpin Receiver ───────────────────────────────────────────────────────


def _get_device_info():
    """Build DeviceInfo for the client hello."""
    from aiosendspin.models.core import DeviceInfo
    from importlib.metadata import version

    system = platform.system()
    product_name = system
    if system == "Linux":
        try:
            os_release = Path("/etc/os-release")
            if os_release.exists():
                for raw_line in os_release.read_text().splitlines():
                    if raw_line.startswith("PRETTY_NAME="):
                        product_name = raw_line.split("=", 1)[1].strip().strip('"')
                        break
        except OSError:
            pass

    try:
        sw_version = f"alfieprime-musiciser (aiosendspin {version('aiosendspin')})"
    except Exception:
        sw_version = "alfieprime-musiciser"

    return DeviceInfo(product_name=product_name, manufacturer=None, software_version=sw_version)


class SendSpinReceiver:
    """Connects to a Music Assistant / SendSpin server, receives audio + metadata.

    Connection modes:
    - No URL: Listens on port 8928, advertises via mDNS (_sendspin._tcp.local.)
      so Music Assistant / SendSpin servers discover and connect to us automatically.
    - With URL: Client-initiated connection to a specific server with auto-reconnect.
    """

    def __init__(
        self, tui: BoomBoxTUI, visualizer: AudioVisualizer,
        server_url: str | None = None, listen_port: int = 8928,
        client_name: str = "MKUltra", config: Config | None = None,
    ) -> None:
        self._tui = tui
        self._visualizer = visualizer
        self._server_url = server_url
        self._listen_port = listen_port
        self._client_name = client_name
        self._client = None
        self._audio_handler = None
        self._listener = None
        self._audio_device = None
        self._supported_formats = None
        self._config = config
        # Use persisted client_id so Music Assistant recognises us across restarts
        if config and config.client_id:
            self._client_id = config.client_id
        else:
            self._client_id = f"alfieprime-musiciser-{uuid.uuid4().hex[:8]}"
            # Persist the newly generated ID for future runs
            if config:
                config.client_id = self._client_id
                config.save()
        self._connection_lock: asyncio.Lock | None = None
        self._flac_decoder = None
        self._flac_fmt = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None

        # Wire up transport control commands from TUI
        self._tui.set_command_callback(self._on_transport_command)

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_event_loop()

        from sendspin.audio_devices import detect_supported_audio_formats, query_devices
        from sendspin.audio_connector import AudioStreamHandler

        # Pick the default audio output device
        devices = query_devices()
        self._audio_device = next((d for d in devices if d.is_default), devices[0] if devices else None)
        if self._audio_device is None:
            raise RuntimeError("No audio output devices found")

        logger.info("Using audio device: %s", self._audio_device.name)
        self._supported_formats = detect_supported_audio_formats(self._audio_device)

        # Audio stream handler (manages playback + FLAC decoding)
        self._audio_handler = AudioStreamHandler(
            audio_device=self._audio_device,
            volume=100,
            muted=False,
            on_format_change=self._on_format_change,
            on_event=self._on_stream_event,
        )

        # Connect
        if self._server_url:
            # Client-initiated: we connect to the server
            self._client = self._create_client()
            self._audio_handler.attach_client(self._client)
            await self._connection_loop_url(self._server_url)
        else:
            # Server-initiated: listen + advertise via mDNS, server connects to us
            await self._run_listener()

    def _create_client(self) -> "SendspinClient":
        """Create a new SendspinClient instance."""
        from aiosendspin.client import SendspinClient
        from aiosendspin.models.player import ClientHelloPlayerSupport
        from aiosendspin.models.types import PlayerCommand, Roles

        # Build artwork support if Pillow is available
        artwork_support = None
        artwork_roles: list[Roles] = []
        if Image is not None:
            from aiosendspin.models.artwork import ArtworkChannel, ClientHelloArtworkSupport
            from aiosendspin.models.types import ArtworkSource, PictureFormat
            artwork_support = ClientHelloArtworkSupport(
                channels=[
                    ArtworkChannel(
                        source=ArtworkSource.ALBUM,
                        format=PictureFormat.JPEG,
                        media_width=128,
                        media_height=128,
                    ),
                ],
            )
            artwork_roles = [Roles.ARTWORK]

        client = SendspinClient(
            client_id=self._client_id,
            client_name=self._client_name,
            roles=[Roles.PLAYER, Roles.METADATA, Roles.CONTROLLER, *artwork_roles],
            device_info=_get_device_info(),
            player_support=ClientHelloPlayerSupport(
                supported_formats=self._supported_formats,
                buffer_capacity=32_000_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
            artwork_support=artwork_support,
            initial_volume=100,
            initial_muted=False,
        )

        # Patch binary message handler to also handle artwork channels
        if Image is not None:
            self._patch_artwork_handler(client)

        # Register callbacks
        client.add_audio_chunk_listener(self._on_audio_chunk)
        client.add_metadata_listener(self._on_metadata)
        client.add_group_update_listener(self._on_group_update)
        client.add_controller_state_listener(self._on_controller_state)
        client.add_server_command_listener(self._on_server_command)

        return client

    # ── Server-initiated mode (mDNS listener) ──

    async def _run_listener(self) -> None:
        """Listen for incoming server connections, advertised via mDNS."""
        from aiosendspin.client import ClientListener
        from aiosendspin.models.core import ClientGoodbyeMessage, ClientGoodbyePayload
        from aiosendspin.models.types import GoodbyeReason

        self._connection_lock = asyncio.Lock()

        self._listener = ClientListener(
            client_id=self._client_id,
            on_connection=self._handle_server_connection,
            port=self._listen_port,
            client_name=self._client_name,
        )
        await self._listener.start()

        self._tui.state.server_name = f"Listening on :{self._listen_port}"
        self._tui.state.connected = False
        logger.info(
            "Listening on port %d, advertising via mDNS (_sendspin._tcp.local.)",
            self._listen_port,
        )

        try:
            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)
        finally:
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            if self._audio_handler is not None:
                await self._audio_handler.shutdown()
            await self._listener.stop()
            self._listener = None

    async def _handle_server_connection(self, ws) -> None:
        """Handle an incoming server WebSocket connection."""
        from aiosendspin.models.core import ClientGoodbyeMessage, ClientGoodbyePayload
        from aiosendspin.models.types import GoodbyeReason

        assert self._connection_lock is not None
        assert self._audio_handler is not None

        logger.info("Server connected")

        async with self._connection_lock:
            # Clean up previous client if any
            if self._client is not None:
                logger.info("Disconnecting from previous server")
                self._tui.state.connected = False
                self._tui.state.is_playing = False
                await self._audio_handler.handle_disconnect()
                if self._client.connected:
                    with contextlib.suppress(Exception):
                        await self._client._send_message(  # noqa: SLF001
                            ClientGoodbyeMessage(
                                payload=ClientGoodbyePayload(reason=GoodbyeReason.ANOTHER_SERVER)
                            ).to_json()
                        )
                await self._client.disconnect()

            # Create fresh client for this connection
            client = self._create_client()
            self._client = client
            self._audio_handler.attach_client(client)

            try:
                await client.attach_websocket(ws)
            except TimeoutError:
                logger.warning("Handshake with server timed out")
                await self._audio_handler.handle_disconnect()
                if self._client is client:
                    self._client = None
                return
            except Exception:
                logger.exception("Error during server handshake")
                await self._audio_handler.handle_disconnect()
                if self._client is client:
                    self._client = None
                return

        # Handshake complete - update TUI
        server_info = client.server_info
        server_name = server_info.name if server_info else "Server"
        self._tui.state.connected = True
        self._tui.state.server_name = server_name
        logger.info("Connected to server: %s", server_name)

        # Wait for disconnect
        try:
            disconnect_event = asyncio.Event()
            unsub = client.add_disconnect_listener(disconnect_event.set)
            await disconnect_event.wait()
            unsub()
            logger.info("Server disconnected")
        except Exception:
            logger.exception("Error waiting for server disconnect")
        finally:
            if self._client is client:
                self._tui.state.connected = False
                self._tui.state.is_playing = False
                self._tui.state.server_name = f"Listening on :{self._listen_port}"
                await self._audio_handler.handle_disconnect()
                self._visualizer.reset()

    # ── Client-initiated mode (explicit URL) ──

    async def _connection_loop_url(self, url: str) -> None:
        """Connect to a specific URL with reconnection."""
        from aiohttp import ClientError

        self._tui.state.server_name = url
        self._tui.state.connected = False
        backoff = 1.0

        while self._running:
            try:
                logger.info("Connecting to %s", url)
                assert self._client is not None
                await self._client.connect(url)
                self._tui.state.connected = True
                self._tui.state.server_name = url
                backoff = 1.0

                # Wait for disconnect
                disconnect_event = asyncio.Event()
                unsub = self._client.add_disconnect_listener(disconnect_event.set)
                await disconnect_event.wait()
                unsub()

                self._tui.state.connected = False
                self._tui.state.is_playing = False
                if self._audio_handler:
                    await self._audio_handler.handle_disconnect()
                logger.info("Disconnected, reconnecting...")

            except (TimeoutError, OSError, ClientError) as e:
                logger.warning("Connection error (%s), retrying in %.0fs", type(e).__name__, backoff)
                self._tui.state.connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
            except Exception:
                logger.exception("Unexpected connection error")
                break

    def _on_audio_chunk(
        self, server_timestamp_us: int, audio_data: bytes | bytearray, fmt: AudioFormat,
    ) -> None:
        """Feed audio to visualizer (audio playback is handled by AudioStreamHandler)."""
        from aiosendspin.models.types import AudioCodec

        pcm = fmt.pcm_format
        self._visualizer.set_format(pcm.sample_rate, pcm.bit_depth, pcm.channels)

        if fmt.codec == AudioCodec.PCM:
            self._visualizer.feed_audio(audio_data)
        elif fmt.codec == AudioCodec.FLAC:
            # Decode FLAC to PCM before feeding visualizer
            if self._flac_decoder is None or self._flac_fmt != fmt:
                from sendspin.decoder import FlacDecoder
                self._flac_decoder = FlacDecoder(fmt)
                self._flac_fmt = fmt
            decoded = self._flac_decoder.decode(audio_data)
            if decoded:
                self._visualizer.feed_audio(decoded)

    def _on_metadata(self, payload) -> None:
        """Handle metadata updates from server."""
        from aiosendspin.models.types import RepeatMode, UndefinedField

        state = self._tui.state
        meta = payload.metadata
        if meta is None:
            return

        if not isinstance(getattr(meta, "title", UndefinedField()), UndefinedField):
            state.title = meta.title or ""
        if not isinstance(getattr(meta, "artist", UndefinedField()), UndefinedField):
            state.artist = meta.artist or ""
        if not isinstance(getattr(meta, "album", UndefinedField()), UndefinedField):
            state.album = meta.album or ""

        repeat = getattr(meta, "repeat", UndefinedField())
        if not isinstance(repeat, UndefinedField) and repeat is not None:
            if repeat == RepeatMode.ONE:
                state.repeat_mode = "one"
            elif repeat == RepeatMode.ALL:
                state.repeat_mode = "all"
            else:
                state.repeat_mode = "off"

        shuffle = getattr(meta, "shuffle", UndefinedField())
        if not isinstance(shuffle, UndefinedField) and shuffle is not None:
            state.shuffle = shuffle

        progress = getattr(meta, "progress", UndefinedField())
        if not isinstance(progress, UndefinedField):
            if progress is not None:
                state.progress_ms = progress.track_progress or 0
                state.duration_ms = progress.track_duration or 0
                # playback_speed is multiplied by 1000 (1000 = normal)
                speed = progress.playback_speed
                state.playback_speed = (speed or 0) / 1000.0
                state.progress_update_time = time.monotonic()
                logger.debug(
                    "Progress update: %dms / %dms, speed=%s",
                    state.progress_ms, state.duration_ms, speed,
                )
            else:
                state.progress_ms = 0
                state.duration_ms = 0
                state.playback_speed = 0.0
                state.progress_update_time = 0.0

    def _on_group_update(self, payload) -> None:
        """Handle group update messages."""
        from aiosendspin.models.types import PlaybackStateType

        state = self._tui.state
        if payload.group_name:
            state.group_name = payload.group_name
        if payload.playback_state:
            was_playing = state.is_playing
            state.is_playing = payload.playback_state == PlaybackStateType.PLAYING
            self._visualizer.set_paused(not state.is_playing)
            # When transitioning to paused, snapshot the interpolated progress
            if was_playing and not state.is_playing:
                state.progress_ms = state.get_interpolated_progress()
                state.progress_update_time = 0.0
            # When transitioning to playing, ensure interpolation has a start time
            elif state.is_playing and not was_playing:
                state.progress_update_time = time.monotonic()

    def _on_format_change(
        self, codec: str | None, sample_rate: int, bit_depth: int, channels: int,
    ) -> None:
        """Handle audio format changes."""
        self._tui.state.codec = codec or "PCM"
        self._tui.state.sample_rate = sample_rate
        self._tui.state.bit_depth = bit_depth
        self._visualizer.set_format(sample_rate, bit_depth, channels)

    def _on_stream_event(self, event: str) -> None:
        """Handle stream start/stop events."""
        self._tui.state.is_playing = event == "start"
        self._visualizer.set_paused(event != "start")
        if event == "stop":
            # Only reset the audio pipeline — keep the visual state (bands,
            # peaks, VU) so the spectrum and meters decay gracefully on pause.
            self._visualizer.reset_pipeline()
            self._flac_decoder = None
            self._flac_fmt = None
        elif event == "start":
            # Reset decoder on new stream (format may change)
            self._flac_decoder = None
            self._flac_fmt = None

    def _on_controller_state(self, payload) -> None:
        """Handle controller state updates (supported commands, volume, mute)."""
        state = self._tui.state
        ctrl = payload.controller
        if ctrl is None:
            return
        state.supported_commands = [cmd.value for cmd in ctrl.supported_commands]
        state.volume = ctrl.volume
        state.muted = ctrl.muted

    def _on_server_command(self, payload) -> None:
        """Handle server commands (volume, mute)."""
        from aiosendspin.models.types import PlayerCommand

        if payload.player is None:
            return
        cmd = payload.player
        if cmd.command == PlayerCommand.VOLUME and cmd.volume is not None:
            self._tui.state.volume = cmd.volume
            if self._audio_handler is not None:
                self._audio_handler.set_volume(cmd.volume, muted=self._tui.state.muted)
        elif cmd.command == PlayerCommand.MUTE and cmd.mute is not None:
            self._tui.state.muted = cmd.mute
            if self._audio_handler is not None:
                self._audio_handler.set_volume(self._tui.state.volume, muted=cmd.mute)

    def _patch_artwork_handler(self, client) -> None:
        """Monkey-patch the client's binary message handler to capture artwork."""
        from aiosendspin.models import BINARY_HEADER_SIZE
        from aiosendspin.models.types import BinaryMessageType

        original_handler = client._handle_binary_message  # noqa: SLF001

        artwork_types = {
            BinaryMessageType.ARTWORK_CHANNEL_0.value,
            BinaryMessageType.ARTWORK_CHANNEL_1.value,
            BinaryMessageType.ARTWORK_CHANNEL_2.value,
            BinaryMessageType.ARTWORK_CHANNEL_3.value,
        }

        def patched_handler(payload: bytes) -> None:
            if len(payload) >= BINARY_HEADER_SIZE:
                raw_type = payload[0]
                if raw_type in artwork_types:
                    image_data = payload[BINARY_HEADER_SIZE:]
                    channel = raw_type - BinaryMessageType.ARTWORK_CHANNEL_0.value
                    if image_data:
                        self._on_artwork(channel, image_data)
                    else:
                        self._on_artwork_cleared(channel)
                    return
            original_handler(payload)

        client._handle_binary_message = patched_handler  # noqa: SLF001

    def _on_artwork(self, channel: int, image_data: bytes) -> None:
        """Handle received album artwork - extract colors and update theme."""
        logger.debug("Received artwork for channel %d (%d bytes)", channel, len(image_data))
        theme = _extract_theme_from_image(image_data)
        if theme is not None:
            self._tui.state.theme = theme
            logger.info(
                "Updated theme from album art: primary=%s secondary=%s accent=%s",
                theme.primary, theme.secondary, theme.accent,
            )
        else:
            # Extraction failed (too dark, greyscale, etc.) - revert to defaults
            self._tui.state.theme = ColorTheme()

    def _on_artwork_cleared(self, channel: int) -> None:
        """Handle artwork cleared - revert to default colours."""
        logger.debug("Artwork cleared for channel %d", channel)
        self._tui.state.theme = ColorTheme()

    def _on_transport_command(self, command: str) -> None:
        """Handle a transport command from the TUI (called from input thread)."""
        state = self._tui.state

        # Volume changes are local — apply immediately even without server
        if command == "volume_up":
            new_vol = min(100, state.volume + 5)
            state.volume = new_vol
            if self._audio_handler is not None:
                self._audio_handler.set_volume(new_vol, muted=state.muted)
            return
        elif command == "volume_down":
            new_vol = max(0, state.volume - 5)
            state.volume = new_vol
            if self._audio_handler is not None:
                self._audio_handler.set_volume(new_vol, muted=state.muted)
            return

        # Seek commands — apply to local progress immediately for responsiveness
        if command.startswith("seek_forward:"):
            seek_ms = int(command.split(":")[1])
            new_prog = min(state.duration_ms, state.get_interpolated_progress() + seek_ms)
            state.progress_ms = new_prog
            state.progress_update_time = time.monotonic()
            # Also send to server if connected
            if self._client and self._client.connected and self._loop:
                from aiosendspin.models.types import MediaCommand
                async def _seek_fwd() -> None:
                    assert self._client is not None
                    try:
                        await self._client.send_group_command(
                            MediaCommand.SEEK, seek_position=new_prog,
                        )
                    except Exception:
                        logger.debug("Seek command not supported or failed")
                asyncio.run_coroutine_threadsafe(_seek_fwd(), self._loop)
            return
        elif command.startswith("seek_backward:"):
            seek_ms = int(command.split(":")[1])
            new_prog = max(0, state.get_interpolated_progress() - seek_ms)
            state.progress_ms = new_prog
            state.progress_update_time = time.monotonic()
            if self._client and self._client.connected and self._loop:
                from aiosendspin.models.types import MediaCommand
                async def _seek_bwd() -> None:
                    assert self._client is not None
                    try:
                        await self._client.send_group_command(
                            MediaCommand.SEEK, seek_position=new_prog,
                        )
                    except Exception:
                        logger.debug("Seek command not supported or failed")
                asyncio.run_coroutine_threadsafe(_seek_bwd(), self._loop)
            return

        if self._client is None or not self._client.connected:
            return
        if self._loop is None:
            return

        from aiosendspin.models.types import MediaCommand

        cmds = set(state.supported_commands)

        async def _send() -> None:
            assert self._client is not None
            try:
                if command == "play_pause":
                    if state.is_playing and "pause" in cmds:
                        await self._client.send_group_command(MediaCommand.PAUSE)
                    elif not state.is_playing and "play" in cmds:
                        await self._client.send_group_command(MediaCommand.PLAY)
                elif command == "next" and "next" in cmds:
                    await self._client.send_group_command(MediaCommand.NEXT)
                elif command == "previous" and "previous" in cmds:
                    await self._client.send_group_command(MediaCommand.PREVIOUS)
                elif command == "shuffle":
                    if state.shuffle and "unshuffle" in cmds:
                        state.shuffle = False
                        await self._client.send_group_command(MediaCommand.UNSHUFFLE)
                    elif not state.shuffle and "shuffle" in cmds:
                        state.shuffle = True
                        await self._client.send_group_command(MediaCommand.SHUFFLE)
                elif command == "repeat":
                    if state.repeat_mode == "off" and "repeat_all" in cmds:
                        state.repeat_mode = "all"
                        await self._client.send_group_command(MediaCommand.REPEAT_ALL)
                    elif state.repeat_mode == "all" and "repeat_one" in cmds:
                        state.repeat_mode = "one"
                        await self._client.send_group_command(MediaCommand.REPEAT_ONE)
                    elif state.repeat_mode == "one" and "repeat_off" in cmds:
                        state.repeat_mode = "off"
                        await self._client.send_group_command(MediaCommand.REPEAT_OFF)
            except Exception:
                logger.exception("Error sending command: %s", command)

        asyncio.run_coroutine_threadsafe(_send(), self._loop)

    async def _run_demo_mode(self) -> None:
        """Demo mode with simulated audio."""
        self._tui.state.connected = True
        self._tui.state.server_name = "Demo Mode"
        self._tui.state.group_name = "Party Room"
        self._tui.state.is_playing = True
        self._tui.state.codec = "PCM"
        self._tui.state.sample_rate = 48000
        self._tui.state.bit_depth = 16
        self._visualizer.set_format(48000, 16, 2)

        tracks = [
            ("Neon Dreams", "Synthwave Collective", "Midnight Drive", 234000),
            ("Bass Drop Protocol", "DJ Electron", "Circuit Breaker", 198000),
            ("Retrowave Sunset", "Chrome Future", "Analog Memories", 267000),
            ("Digital Groove", "Bit Crusher", "Sample Rate", 185000),
            ("Phantom Signal", "Ghost Frequency", "Spectral Analysis", 312000),
        ]
        track_idx = 0
        t_title, t_artist, t_album, t_duration = tracks[track_idx]
        self._tui.state.title = t_title
        self._tui.state.artist = t_artist
        self._tui.state.album = t_album
        self._tui.state.duration_ms = t_duration
        self._tui.state.progress_ms = 0

        sample_rate = 48000
        chunk_size = 2048
        bytes_per_chunk = chunk_size * 2 * 2  # 16-bit stereo

        beat_bpm = 128
        beat_freq = beat_bpm / 60.0
        bass_freq = 60.0
        mid_freq = 440.0
        time_pos = 0.0
        dt = chunk_size / sample_rate

        while self._running:
            audio_data = bytearray(bytes_per_chunk)
            t = time_pos

            beat_phase = (t * beat_freq) % 1.0
            kick = max(0, 1.0 - beat_phase * 8) * 0.8
            snare_phase = ((t * beat_freq) + 0.5) % 1.0
            snare = max(0, 1.0 - snare_phase * 12) * 0.3

            for i in range(chunk_size):
                sample_t = t + i / sample_rate
                bass = math.sin(2 * math.pi * bass_freq * sample_t) * kick * 0.6
                melody_env = 0.3 + 0.2 * math.sin(2 * math.pi * 0.25 * sample_t)
                mid = math.sin(2 * math.pi * mid_freq * sample_t) * melody_env * 0.3
                mid += math.sin(2 * math.pi * mid_freq * 1.5 * sample_t) * melody_env * 0.15
                noise = (random.random() * 2 - 1) * snare * 0.2
                hi = math.sin(2 * math.pi * 8000 * sample_t) * 0.05

                left = max(-1.0, min(1.0, bass + mid + noise + hi + math.sin(2 * math.pi * 200 * sample_t) * 0.1))
                right = max(-1.0, min(1.0, bass + mid * 0.8 + noise + hi * 1.2 + math.sin(2 * math.pi * 250 * sample_t) * 0.1))

                offset = i * 4
                struct.pack_into("<hh", audio_data, offset, int(left * 32000), int(right * 32000))

            self._visualizer.feed_audio(bytes(audio_data))
            time_pos += dt

            self._tui.state.progress_ms = int(time_pos * 1000) % t_duration
            if self._tui.state.progress_ms < 100 and time_pos > 1.0:
                track_idx = (track_idx + 1) % len(tracks)
                t_title, t_artist, t_album, t_duration = tracks[track_idx]
                self._tui.state.title = t_title
                self._tui.state.artist = t_artist
                self._tui.state.album = t_album
                self._tui.state.duration_ms = t_duration

            await asyncio.sleep(chunk_size / sample_rate * 0.5)

    def stop(self) -> None:
        self._running = False


# ─── Main ────────────────────────────────────────────────────────────────────


async def _run_with_config(config: Config, demo: bool = False, gui: bool = False) -> None:
    """Run the TUI + receiver using the given config."""
    visualizer = AudioVisualizer()
    tui = BoomBoxTUI(visualizer, gui=gui)

    server_url = config.server_url if config.mode == "connect" else None

    receiver = SendSpinReceiver(
        tui, visualizer,
        server_url=server_url,
        listen_port=config.listen_port,
        client_name=config.client_name,
        config=config,
    )

    loop = asyncio.get_running_loop()
    if IS_WINDOWS:
        # Windows doesn't support loop.add_signal_handler; use signal module directly
        signal.signal(signal.SIGINT, lambda *_: (receiver.stop(), tui.stop()))
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: (receiver.stop(), tui.stop()))

    if demo:
        receiver._running = True
        await asyncio.gather(tui.run(), receiver._run_demo_mode())
    else:
        await asyncio.gather(tui.run(), receiver.start())


def _test_connection(config: Config, console: Console) -> str | None:
    """Try a quick connection to validate the config. Returns error string or None on success."""
    if config.mode == "listen":
        # For listen mode, just check the port is bindable
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", config.listen_port))
            return None
        except OSError as e:
            return f"Cannot bind to port {config.listen_port}: {e}"

    # For connect mode, try a quick WebSocket handshake
    if not config.server_url:
        return "No server URL configured"

    import asyncio

    async def _try_connect() -> str | None:
        try:
            from aiohttp import ClientSession, ClientError, WSMsgType
            timeout_s = 5
            async with ClientSession() as session:
                async with session.ws_connect(config.server_url, timeout=timeout_s) as ws:
                    await ws.close()
            return None
        except (TimeoutError, OSError, ClientError) as e:
            return f"{type(e).__name__}: {e}"
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    return asyncio.run(_try_connect())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AlfiePRIME Musiciser - boom box receiver for Music Assistant",
        epilog=(
            "On first run, an interactive setup wizard will guide you through\n"
            "configuration. Settings are saved to ~/.config/alfieprime-musiciser/config.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--setup", action="store_true", help="Re-run the setup wizard")
    parser.add_argument("--demo", action="store_true", help="Run in demo mode without a server")
    parser.add_argument("--gui", action="store_true", help="Run in a standalone GUI window instead of the terminal")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # When launched via pythonw.exe (gui_scripts), stdout/stderr are None.
    # Redirect them to devnull so logging / print don't crash.
    _headless = sys.stdout is None
    if _headless:
        _devnull = open(os.devnull, "w")  # noqa: SIM115
        sys.stdout = _devnull
        sys.stderr = _devnull

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    # GUI headless path: skip interactive console setup, load existing config
    # or use defaults, and go straight to the GUI.
    if args.gui and _headless:
        config = Config.load() or Config()
        if not config.client_id:
            config.client_id = str(uuid.uuid4())
            config.save()
        try:
            asyncio.run(_run_with_config(config, gui=True))
        except KeyboardInterrupt:
            pass
        return

    console = Console()

    # Demo mode — skip config entirely
    if args.demo:
        try:
            asyncio.run(_run_with_config(Config(), demo=True, gui=args.gui))
        except KeyboardInterrupt:
            pass
        return

    # Check sendspin is installed
    try:
        from aiosendspin.client import SendspinClient  # noqa: F401
        from sendspin.audio_devices import query_devices  # noqa: F401
    except ImportError:
        console.print(
            "[bold red]Error:[/] sendspin package is not installed.\n"
            "Install it with: [bright_cyan]pip install 'sendspin>=0.12.0'[/]"
        )
        sys.exit(1)

    # Load or create config
    config = None if args.setup else Config.load()

    if config is None:
        # First run or --setup: run the wizard
        config = run_setup(console)

    # Connection test + retry loop
    while True:
        console.print(f"[dim]Mode:[/] [bright_cyan]{config.mode}[/]", highlight=False)
        if config.mode == "connect":
            console.print(f"[dim]Server:[/] [bright_cyan]{config.server_url}[/]", highlight=False)
        else:
            console.print(f"[dim]Listen port:[/] [bright_cyan]{config.listen_port}[/]", highlight=False)
        console.print(f"[dim]Client name:[/] [bright_cyan]{config.client_name}[/]", highlight=False)
        console.print()

        console.print("[dim]Testing connection...[/]")
        error = _test_connection(config, console)

        if error is None:
            console.print("[bright_green]OK![/] Starting party...\n")
            break
        else:
            console.print(f"\n[bold red]Connection failed:[/] {error}\n")
            choice = Prompt.ask(
                "What would you like to do?",
                choices=["retry", "setup", "quit"],
                default="setup",
                console=console,
            )
            if choice == "retry":
                continue
            elif choice == "setup":
                config = run_setup(console, existing=config)
                continue
            else:
                return

    # Run!
    try:
        asyncio.run(_run_with_config(config, gui=args.gui))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
