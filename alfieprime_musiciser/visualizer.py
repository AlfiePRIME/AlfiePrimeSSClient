from __future__ import annotations

import threading
import time

import numpy as np
from collections import deque

NUM_BANDS = 32
FFT_SIZE = 2048
RING_BUFFER_SIZE = FFT_SIZE * 4


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
