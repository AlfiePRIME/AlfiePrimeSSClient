"""Software audio mixer for DJ mode.

Intercepts PCM from both SendSpin and AirPlay sources, applies per-channel
volume, 3-band EQ, and crossfader, then outputs to a single audio device
and feeds per-channel + master visualizers.
"""
from __future__ import annotations

import logging
import math
import struct
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from alfieprime_musiciser.dj_state import DJState
    from alfieprime_musiciser.visualizer import AudioVisualizer

logger = logging.getLogger(__name__)

def _setup_dj_file_log() -> None:
    """Ensure DJ mixer diagnostics also go to the airplay debug log."""
    import os
    log_dir = os.path.join(os.path.expanduser("~"), ".cache", "alfieprime")
    log_file = os.path.join(log_dir, "airplay_debug.log")
    if os.path.isdir(log_dir):
        for h in logger.handlers:
            if isinstance(h, logging.FileHandler) and h.baseFilename == log_file:
                return  # already attached
        try:
            fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            fh.setLevel(logging.DEBUG)
            logger.addHandler(fh)
            logger.setLevel(logging.DEBUG)
        except Exception:
            pass

_setup_dj_file_log()

# ── Constants ────────────────────────────────────────────────────────────────

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16-bit
FRAME_SIZE = CHANNELS * SAMPLE_WIDTH  # 4 bytes per frame
CHUNK_FRAMES = 1024  # ~21ms at 48kHz
CHUNK_BYTES = CHUNK_FRAMES * FRAME_SIZE
RING_SIZE = SAMPLE_RATE * CHANNELS  # 1 second of stereo float32


# ── Biquad filter for EQ ────────────────────────────────────────────────────

def _low_shelf_coeffs(freq: float, gain_db: float, sr: float) -> tuple:
    """Second-order low-shelf biquad coefficients."""
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * freq / sr
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / 2 * math.sqrt(2.0)
    sqA = math.sqrt(A)

    b0 = A * ((A + 1) - (A - 1) * cos_w0 + 2 * sqA * alpha)
    b1 = 2 * A * ((A - 1) - (A + 1) * cos_w0)
    b2 = A * ((A + 1) - (A - 1) * cos_w0 - 2 * sqA * alpha)
    a0 = (A + 1) + (A - 1) * cos_w0 + 2 * sqA * alpha
    a1 = -2 * ((A - 1) + (A + 1) * cos_w0)
    a2 = (A + 1) + (A - 1) * cos_w0 - 2 * sqA * alpha

    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def _peaking_eq_coeffs(freq: float, gain_db: float, Q: float, sr: float) -> tuple:
    """Second-order peaking EQ biquad coefficients."""
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * freq / sr
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2 * Q)

    b0 = 1 + alpha * A
    b1 = -2 * cos_w0
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cos_w0
    a2 = 1 - alpha / A

    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def _high_shelf_coeffs(freq: float, gain_db: float, sr: float) -> tuple:
    """Second-order high-shelf biquad coefficients."""
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * freq / sr
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / 2 * math.sqrt(2.0)
    sqA = math.sqrt(A)

    b0 = A * ((A + 1) + (A - 1) * cos_w0 + 2 * sqA * alpha)
    b1 = -2 * A * ((A - 1) + (A + 1) * cos_w0)
    b2 = A * ((A + 1) + (A - 1) * cos_w0 - 2 * sqA * alpha)
    a0 = (A + 1) - (A - 1) * cos_w0 + 2 * sqA * alpha
    a1 = 2 * ((A - 1) - (A + 1) * cos_w0)
    a2 = (A + 1) - (A - 1) * cos_w0 - 2 * sqA * alpha

    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


try:
    from scipy.signal import sosfilt as _sosfilt
    _HAS_SCIPY = True
except ImportError:
    _sosfilt = None  # type: ignore[assignment]
    _HAS_SCIPY = False


class _BiquadFilter:
    """Stereo biquad IIR filter with state."""

    def __init__(self) -> None:
        self._coeffs = (1.0, 0.0, 0.0, 0.0, 0.0)  # pass-through
        self._bypass = True
        # SOS format for scipy: [b0, b1, b2, 1, a1, a2]
        self._sos = np.array([[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]])
        # State per channel: shape (1, 2) for sosfilt zi
        self._zi_l = np.zeros((1, 2), dtype=np.float64)
        self._zi_r = np.zeros((1, 2), dtype=np.float64)

    def set_coeffs(self, coeffs: tuple) -> None:
        self._coeffs = coeffs
        b0, b1, b2, a1, a2 = coeffs
        self._bypass = (b0 == 1.0 and b1 == 0.0 and b2 == 0.0
                        and a1 == 0.0 and a2 == 0.0)
        self._sos = np.array([[b0, b1, b2, 1.0, a1, a2]])

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Process interleaved stereo float32 samples."""
        if self._bypass:
            return samples

        n = len(samples)
        if n < 2:
            return samples

        # De-interleave to separate channels
        left = samples[0::2].astype(np.float64)
        right = samples[1::2].astype(np.float64)

        if _HAS_SCIPY:
            left, self._zi_l = _sosfilt(self._sos, left, zi=self._zi_l)
            right, self._zi_r = _sosfilt(self._sos, right, zi=self._zi_r)
        else:
            # Pure-numpy fallback: transposed direct form II
            b0, b1, b2, a1, a2 = self._coeffs
            left, self._zi_l = self._df2t(left, b0, b1, b2, a1, a2, self._zi_l)
            right, self._zi_r = self._df2t(right, b0, b1, b2, a1, a2, self._zi_r)

        # Re-interleave
        out = np.empty(n, dtype=np.float32)
        out[0::2] = left.astype(np.float32)
        out[1::2] = right.astype(np.float32)
        return out

    @staticmethod
    def _df2t(x, b0, b1, b2, a1, a2, zi):
        """Transposed direct form II — sample-by-sample fallback."""
        z1, z2 = zi[0, 0], zi[0, 1]
        out = np.empty_like(x)
        for i in range(len(x)):
            xi = x[i]
            yi = b0 * xi + z1
            z1 = b1 * xi - a1 * yi + z2
            z2 = b2 * xi - a2 * yi
            out[i] = yi
        zi_out = np.array([[z1, z2]])
        return out, zi_out


class _ChannelEQ:
    """3-band EQ for a single channel.

    Optimised to de-interleave once, apply all non-bypass filters in
    float64, then re-interleave once — avoiding repeated type conversions.
    """

    def __init__(self) -> None:
        self.bass = _BiquadFilter()
        self.mid = _BiquadFilter()
        self.treble = _BiquadFilter()
        self._last_params: tuple[int, int, int] = (0, 0, 0)
        self._all_bypass = True

    def update(self, bass_db: int, mid_db: int, treble_db: int) -> None:
        params = (bass_db, mid_db, treble_db)
        if params == self._last_params:
            return
        self._last_params = params
        self.bass.set_coeffs(_low_shelf_coeffs(250.0, float(bass_db), SAMPLE_RATE))
        self.mid.set_coeffs(_peaking_eq_coeffs(1000.0, float(mid_db), 0.7, SAMPLE_RATE))
        self.treble.set_coeffs(_high_shelf_coeffs(4000.0, float(treble_db), SAMPLE_RATE))
        self._all_bypass = self.bass._bypass and self.mid._bypass and self.treble._bypass

    def process(self, samples: np.ndarray) -> np.ndarray:
        if self._all_bypass:
            return samples

        n = len(samples)
        if n < 2:
            return samples

        # Single de-interleave
        left = samples[0::2].astype(np.float64)
        right = samples[1::2].astype(np.float64)

        # Apply active filters in sequence (staying in float64)
        for filt in (self.bass, self.mid, self.treble):
            if filt._bypass:
                continue
            if _HAS_SCIPY:
                left, filt._zi_l = _sosfilt(filt._sos, left, zi=filt._zi_l)
                right, filt._zi_r = _sosfilt(filt._sos, right, zi=filt._zi_r)
            else:
                b0, b1, b2, a1, a2 = filt._coeffs
                left, filt._zi_l = _BiquadFilter._df2t(left, b0, b1, b2, a1, a2, filt._zi_l)
                right, filt._zi_r = _BiquadFilter._df2t(right, b0, b1, b2, a1, a2, filt._zi_r)

        # Single re-interleave
        out = np.empty(n, dtype=np.float32)
        out[0::2] = left
        out[1::2] = right
        return out


# ── Ring buffer for PCM input ────────────────────────────────────────────────

class _InputRing:
    """Thread-safe ring buffer for incoming PCM (stereo float32)."""

    def __init__(self, size: int = RING_SIZE) -> None:
        self._buf = np.zeros(size, dtype=np.float32)
        self._size = size
        self._write = 0
        self._read = 0
        self._lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        with self._lock:
            n = len(data)
            if n >= self._size:
                # Only keep the latest window
                data = data[-self._size:]
                n = self._size
            end = self._write + n
            if end <= self._size:
                self._buf[self._write:end] = data
            else:
                first = self._size - self._write
                self._buf[self._write:] = data[:first]
                self._buf[:n - first] = data[first:]
            self._write = end % self._size

    def read(self, n: int) -> np.ndarray:
        with self._lock:
            avail = (self._write - self._read) % self._size
            if avail <= 0 and self._write != self._read:
                avail = self._size
            n = min(n, avail)
            if n <= 0:
                return np.zeros(0, dtype=np.float32)
            end = self._read + n
            if end <= self._size:
                out = self._buf[self._read:end].copy()
            else:
                first = self._size - self._read
                out = np.concatenate([
                    self._buf[self._read:],
                    self._buf[:n - first],
                ])
            self._read = end % self._size
            return out

    def available(self) -> int:
        with self._lock:
            return (self._write - self._read) % self._size

    def clear(self) -> None:
        with self._lock:
            self._read = self._write


# ── PCM conversion helpers ───────────────────────────────────────────────────

def _s16_to_float32(data: bytes) -> np.ndarray:
    """Convert s16le PCM bytes to interleaved stereo float32 [-1, 1]."""
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    samples /= 32768.0
    return samples


def _float32_to_s16(samples: np.ndarray) -> bytes:
    """Convert interleaved stereo float32 to s16le PCM bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767).astype(np.int16).tobytes()


def _resample_linear(data: np.ndarray, from_rate: int, to_rate: int, channels: int) -> np.ndarray:
    """Simple linear interpolation resampler for stereo/mono."""
    if from_rate == to_rate:
        return data
    ratio = to_rate / from_rate
    in_frames = len(data) // channels
    out_frames = int(in_frames * ratio)
    if channels == 2:
        reshaped = data.reshape(-1, 2)
        indices = np.linspace(0, in_frames - 1, out_frames)
        idx_floor = np.floor(indices).astype(int)
        idx_ceil = np.minimum(idx_floor + 1, in_frames - 1)
        frac = (indices - idx_floor).reshape(-1, 1)
        result = reshaped[idx_floor] * (1 - frac) + reshaped[idx_ceil] * frac
        return result.reshape(-1).astype(np.float32)
    else:
        indices = np.linspace(0, in_frames - 1, out_frames)
        idx_floor = np.floor(indices).astype(int)
        idx_ceil = np.minimum(idx_floor + 1, in_frames - 1)
        frac = indices - idx_floor
        return (data[idx_floor] * (1 - frac) + data[idx_ceil] * frac).astype(np.float32)


# ── Main Mixer ───────────────────────────────────────────────────────────────

class DJMixer:
    """Software mixer for two audio channels with EQ and crossfader.

    Feed PCM from both sources via ``feed_a()`` / ``feed_b()``.
    The mixer thread reads from both ring buffers, applies processing,
    and writes to a PyAudio output stream.
    """

    def __init__(
        self,
        dj_state: DJState,
        master_visualizer: AudioVisualizer | None = None,
        viz_a: AudioVisualizer | None = None,
        viz_b: AudioVisualizer | None = None,
        *,
        pcm_ring_a=None,
        pcm_ring_b=None,
        pcm_ring_mix=None,
        source_ring_a=None,
        source_ring_b=None,
    ) -> None:
        self._dj = dj_state
        self._master_viz = master_visualizer
        self._viz_a = viz_a
        self._viz_b = viz_b
        # SharedPCMRing outputs (replaces visualizer feeds when set)
        self._pcm_ring_a = pcm_ring_a
        self._pcm_ring_b = pcm_ring_b
        self._pcm_ring_mix = pcm_ring_mix
        # SharedPCMRing *inputs* — read directly from a source's ring
        # instead of using feed_a/feed_b + internal _InputRing.  This
        # lets the DJ mixer share the same PCM stream that the boombox
        # visualizer uses, avoiding timing/latency issues from a
        # separate feed path.
        self._source_ring_a = source_ring_a
        self._source_ring_b = source_ring_b
        self._ring_a = _InputRing()
        self._ring_b = _InputRing()
        self._eq_a = _ChannelEQ()
        self._eq_b = _ChannelEQ()
        self._running = False
        self._thread: threading.Thread | None = None
        self._pa = None
        self._stream = None
        # Source format info for resampling
        self._rate_a = SAMPLE_RATE
        self._rate_b = SAMPLE_RATE
        self._channels_a = 2
        self._channels_b = 2
        self._bit_depth_a = 16
        self._bit_depth_b = 16
        # Diagnostic counters — exposed on DJ screen for debugging
        self._feed_a_count = 0
        self._feed_b_count = 0
        self._mix_count = 0
        self._ring_b_reads = 0  # non-zero reads from ring_b

    def start(self) -> None:
        """Start the mixer output thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._mix_loop, daemon=True)
        self._thread.start()
        logger.info("DJ mixer started (%d Hz, stereo)", SAMPLE_RATE)

    def stop(self) -> None:
        """Stop the mixer and release audio resources."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        # Clear ring buffers so no stale audio leaks into the next session
        self._ring_a.clear()
        self._ring_b.clear()
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        logger.info("DJ mixer stopped")

    def set_format_a(self, rate: int, bit_depth: int, channels: int) -> None:
        self._rate_a = rate
        self._bit_depth_a = bit_depth
        self._channels_a = channels

    def set_format_b(self, rate: int, bit_depth: int, channels: int) -> None:
        self._rate_b = rate
        self._bit_depth_b = bit_depth
        self._channels_b = channels

    def feed_a(self, pcm_bytes: bytes | bytearray) -> None:
        """Feed SendSpin PCM into channel A."""
        try:
            samples = self._decode_and_resample(
                pcm_bytes, self._rate_a, self._bit_depth_a, self._channels_a,
            )
            self._ring_a.write(samples)
            self._feed_a_count += 1
        except Exception:
            logger.debug("DJ mixer: feed_a error", exc_info=True)

    def feed_b(self, pcm_bytes: bytes | bytearray) -> None:
        """Feed AirPlay PCM into channel B."""
        try:
            samples = self._decode_and_resample(
                pcm_bytes, self._rate_b, self._bit_depth_b, self._channels_b,
            )
            self._ring_b.write(samples)
            self._feed_b_count += 1
            if self._feed_b_count <= 3:
                peak = float(np.max(np.abs(samples))) if len(samples) > 0 else 0.0
                logger.warning(
                    "DJ feed_b[%d]: %d bytes → %d samples, peak=%.4f, "
                    "fmt=%dHz/%dbit/%dch",
                    self._feed_b_count, len(pcm_bytes), len(samples), peak,
                    self._rate_b, self._bit_depth_b, self._channels_b,
                )
        except Exception:
            logger.warning("DJ mixer: feed_b error", exc_info=True)

    def _decode_and_resample(
        self, data: bytes | bytearray, rate: int, bit_depth: int, channels: int,
    ) -> np.ndarray:
        """Decode PCM bytes to float32 stereo at SAMPLE_RATE."""
        if bit_depth == 16:
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        elif bit_depth == 24:
            # Vectorized 24-bit decode
            n_samples = len(data) // 3
            raw = np.frombuffer(data[:n_samples * 3], dtype=np.uint8).reshape(-1, 3)
            arr = (raw[:, 0].astype(np.int32)
                   | (raw[:, 1].astype(np.int32) << 8)
                   | (raw[:, 2].astype(np.int32) << 16))
            arr[arr >= 0x800000] -= 0x1000000
            samples = arr.astype(np.float32) / 8388608.0
        elif bit_depth == 32:
            samples = np.frombuffer(data, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

        # Mono to stereo
        if channels == 1:
            samples = np.repeat(samples, 2)

        # Resample to mixer rate
        if rate != SAMPLE_RATE:
            samples = _resample_linear(samples, rate, SAMPLE_RATE, 2)

        return samples

    def _read_channel(
        self,
        source_ring,
        internal_ring: _InputRing,
        chunk_stereo: int,
    ) -> np.ndarray:
        """Read PCM for one channel from a SharedPCMRing or internal ring.

        When a source_ring (SharedPCMRing) is set, reads from it and
        resamples to the mixer's SAMPLE_RATE.  Otherwise falls back to
        the internal _InputRing (fed via feed_a/feed_b).
        """
        if source_ring is None:
            return internal_ring.read(chunk_stereo)

        # Read format from the source ring header
        sr, _bd, _ch = source_ring.get_format()
        if sr == 0:
            sr = 44100  # sensible default

        if sr != SAMPLE_RATE:
            # Calculate how many source samples we need for one mixer chunk
            src_samples = int(chunk_stereo * sr / SAMPLE_RATE) + 4
            raw = source_ring.read(src_samples)
            if len(raw) == 0:
                return raw
            return _resample_linear(raw, sr, SAMPLE_RATE, CHANNELS)
        else:
            return source_ring.read(chunk_stereo)

    def _mix_loop(self) -> None:
        """Main mixer thread: read, process, output."""
        try:
            import pyaudio
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=self._pa.get_format_from_width(SAMPLE_WIDTH),
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                output=True,
                frames_per_buffer=CHUNK_FRAMES,
            )
        except Exception:
            logger.exception("DJ mixer: failed to open audio output")
            self._running = False
            return

        # Set visualizer format (when using in-process visualizers)
        if self._master_viz:
            self._master_viz.set_format(SAMPLE_RATE, 16, 2)
        if self._viz_a:
            self._viz_a.set_format(SAMPLE_RATE, 16, 2)
        if self._viz_b:
            self._viz_b.set_format(SAMPLE_RATE, 16, 2)
        # Set ring format metadata
        for ring in (self._pcm_ring_a, self._pcm_ring_b, self._pcm_ring_mix):
            if ring is not None:
                ring.set_format(SAMPLE_RATE, 16, 2)

        chunk_stereo = CHUNK_FRAMES * CHANNELS  # float32 samples per chunk
        # Pre-allocate silence buffer to avoid np.pad allocations in hot path
        _silence = np.zeros(chunk_stereo, dtype=np.float32)
        logger.warning("DJ mixer: mix_loop STARTED (pyaudio OK)")
        _diag_interval = 0

        # Pre-compute half-pi for crossfade
        _half_pi = math.pi / 2

        while self._running:
            try:
                dj = self._dj

                # Update EQ coefficients
                ch_a = dj.channel_a
                ch_b = dj.channel_b
                self._eq_a.update(ch_a.eq_bass, ch_a.eq_mid, ch_a.eq_treble)
                self._eq_b.update(ch_b.eq_bass, ch_b.eq_mid, ch_b.eq_treble)

                # Read from ring buffers (source rings override internal rings)
                pcm_a = self._read_channel(
                    self._source_ring_a, self._ring_a, chunk_stereo,
                )
                pcm_b = self._read_channel(
                    self._source_ring_b, self._ring_b, chunk_stereo,
                )
                self._mix_count += 1
                if len(pcm_b) > 0:
                    self._ring_b_reads += 1

                # Periodic diagnostic every ~5 seconds
                _diag_interval += 1
                if _diag_interval >= 240:  # ~5s at 48 iter/sec
                    _diag_interval = 0

                # Pad to chunk size if needed (use pre-allocated silence)
                len_a = len(pcm_a)
                len_b = len(pcm_b)
                if len_a < chunk_stereo:
                    if len_a == 0:
                        pcm_a = _silence
                    else:
                        tmp = _silence.copy()
                        tmp[:len_a] = pcm_a
                        pcm_a = tmp
                if len_b < chunk_stereo:
                    if len_b == 0:
                        pcm_b = _silence
                    else:
                        tmp = _silence.copy()
                        tmp[:len_b] = pcm_b
                        pcm_b = tmp

                # Apply EQ
                pcm_a = self._eq_a.process(pcm_a)
                pcm_b = self._eq_b.process(pcm_b)

                # Apply per-channel volume
                vol_a = ch_a.volume / 100.0
                vol_b = ch_b.volume / 100.0
                pcm_a *= vol_a
                pcm_b *= vol_b

                # Equal-power crossfade
                xf = dj.crossfader
                gain_a = math.cos(xf * _half_pi)
                gain_b = math.sin(xf * _half_pi)

                # Mix
                mixed = pcm_a * gain_a + pcm_b * gain_b

                # Periodic diagnostic every ~5 seconds
                if _diag_interval == 0:
                    _peak_a = float(np.max(np.abs(pcm_a))) if len_a > 0 else 0.0
                    _peak_b = float(np.max(np.abs(pcm_b))) if len_b > 0 else 0.0
                    _peak_mix = float(np.max(np.abs(mixed)))
                    logger.warning(
                        "DJ DIAG: feed_a=%d feed_b=%d rb_reads=%d "
                        "peak_a=%.4f peak_b=%.4f peak_mix=%.4f "
                        "xf=%.2f gA=%.3f gB=%.3f volB=%d "
                        "fmtB=%dHz/%dbit/%dch viz_paused=%s",
                        self._feed_a_count, self._feed_b_count,
                        self._ring_b_reads,
                        _peak_a, _peak_b, _peak_mix,
                        dj.crossfader, gain_a, gain_b, ch_b.volume,
                        self._rate_b, self._bit_depth_b, self._channels_b,
                        self._master_viz._paused if self._master_viz else False,
                    )

                # Feed per-channel and mixed PCM to shared rings (for TUI viz)
                if self._pcm_ring_a is not None:
                    self._pcm_ring_a.write(pcm_a)
                elif self._viz_a:
                    self._viz_a.feed_audio_float32(pcm_a)
                if self._pcm_ring_b is not None:
                    self._pcm_ring_b.write(pcm_b)
                elif self._viz_b:
                    self._viz_b.feed_audio_float32(pcm_b)
                if self._pcm_ring_mix is not None:
                    self._pcm_ring_mix.write(mixed)
                elif self._master_viz:
                    self._master_viz.feed_audio_float32(mixed)

                # Convert to s16 ONCE for audio output
                out_bytes = _float32_to_s16(mixed)
                self._stream.write(out_bytes)

            except Exception:
                logger.debug("DJ mixer: mix loop error", exc_info=True)
                time.sleep(0.01)

        # Cleanup handled by stop()
