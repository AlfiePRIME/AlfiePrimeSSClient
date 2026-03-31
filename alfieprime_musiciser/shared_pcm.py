"""Lock-free SPSC shared-memory ring buffer for cross-process PCM transfer.

Each receiver writes decoded float32 stereo PCM to its own ring.
The TUI process reads from the active source's ring and runs FFT locally.
"""
from __future__ import annotations

import atexit
import struct
import numpy as np
from multiprocessing.shared_memory import SharedMemory

# Header layout (64 bytes, cache-line aligned):
#   0:8   write_pos  (uint64, monotonically increasing sample count)
#   8:16  read_pos   (uint64, monotonically increasing sample count)
#  16:20  sample_rate (uint32)
#  20:22  bit_depth   (uint16)
#  22:24  channels    (uint16)
#  24:28  flags       (uint32)  bit 0 = format_changed
#  28:64  reserved
_HEADER = 64  # bytes


class SharedPCMRing:
    """Single-producer / single-consumer ring buffer backed by SharedMemory.

    Stores float32 interleaved stereo samples.  The write/read positions
    are monotonically increasing sample indices; wrap-around is handled
    via modulo on access.

    Parameters
    ----------
    name : str
        Shared memory segment name (unique per ring).
    capacity : int
        Ring capacity in *frames* (each frame = ``channels`` float32 samples).
    channels : int
        Number of interleaved channels (default 2 = stereo).
    create : bool
        True on the producer side (creates the segment).
    """

    def __init__(
        self,
        name: str,
        capacity: int = 48000,
        channels: int = 2,
        create: bool = False,
    ) -> None:
        self._channels = channels
        self._capacity = capacity
        self._total_samples = capacity * channels
        buf_bytes = _HEADER + self._total_samples * 4  # float32

        if create:
            self._shm = SharedMemory(name=name, create=True, size=buf_bytes)
            self._owned = True
            # Zero-initialise header
            self._shm.buf[:_HEADER] = b"\x00" * _HEADER
            self._set_format(48000, 16, 2)
            atexit.register(self._cleanup)
        else:
            self._shm = SharedMemory(name=name, create=False)
            self._owned = False

        # Numpy view over the data portion (zero-copy reads/writes)
        self._buf: np.ndarray = np.ndarray(
            (self._total_samples,),
            dtype=np.float32,
            buffer=self._shm.buf[_HEADER:],
        )
        self.name = name

    # -- Format metadata (written by producer, read by consumer) ----------

    def _set_format(self, sample_rate: int, bit_depth: int, channels: int) -> None:
        struct.pack_into("=IHH", self._shm.buf, 16, sample_rate, bit_depth, channels)
        # Set format_changed flag
        old = struct.unpack_from("=I", self._shm.buf, 24)[0]
        struct.pack_into("=I", self._shm.buf, 24, old | 1)

    def get_format(self) -> tuple[int, int, int]:
        """Return (sample_rate, bit_depth, channels)."""
        sr, bd, ch = struct.unpack_from("=IHH", self._shm.buf, 16)
        return sr, bd, ch

    def set_format(self, sample_rate: int, bit_depth: int, channels: int) -> None:
        """Producer: update the format metadata."""
        self._set_format(sample_rate, bit_depth, channels)

    def consume_format_change(self) -> bool:
        """Consumer: check and clear the format_changed flag."""
        flags = struct.unpack_from("=I", self._shm.buf, 24)[0]
        if flags & 1:
            struct.pack_into("=I", self._shm.buf, 24, flags & ~1)
            return True
        return False

    # -- Positions --------------------------------------------------------

    @property
    def _write_pos(self) -> int:
        return struct.unpack_from("=Q", self._shm.buf, 0)[0]

    @_write_pos.setter
    def _write_pos(self, v: int) -> None:
        struct.pack_into("=Q", self._shm.buf, 0, v)

    @property
    def _read_pos(self) -> int:
        return struct.unpack_from("=Q", self._shm.buf, 8)[0]

    @_read_pos.setter
    def _read_pos(self, v: int) -> None:
        struct.pack_into("=Q", self._shm.buf, 8, v)

    # -- Producer API -----------------------------------------------------

    def write(self, samples: np.ndarray) -> int:
        """Write float32 interleaved samples.  Returns count written."""
        n = len(samples)
        if n == 0:
            return 0
        cap = self._total_samples
        if n > cap:
            samples = samples[-cap:]
            n = cap

        wp = self._write_pos % cap
        end = wp + n
        if end <= cap:
            self._buf[wp:end] = samples
        else:
            first = cap - wp
            self._buf[wp:cap] = samples[:first]
            self._buf[: n - first] = samples[first:]

        self._write_pos += n
        return n

    def write_bytes_s16(self, data: bytes, channels: int = 2) -> int:
        """Convenience: decode S16LE bytes to float32 stereo and write."""
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        return self.write(samples)

    # -- Consumer API -----------------------------------------------------

    def read(self, max_samples: int = 0) -> np.ndarray:
        """Read available float32 samples (non-blocking).

        If the reader fell behind (more than ``capacity`` unread), it
        skips forward to the latest data.
        """
        wp = self._write_pos
        rp = self._read_pos
        cap = self._total_samples
        avail = wp - rp
        if avail <= 0:
            return np.empty(0, dtype=np.float32)
        if avail > cap:
            # Reader fell behind — skip to latest
            rp = wp - cap
        if max_samples > 0:
            avail = min(avail, max_samples)

        start = rp % cap
        end = start + avail
        if end <= cap:
            out = self._buf[start:end].copy()
        else:
            first = cap - start
            out = np.concatenate([self._buf[start:cap], self._buf[: avail - first]])

        self._read_pos = rp + avail
        return out

    def available(self) -> int:
        """Samples available for reading."""
        return max(0, int(self._write_pos - self._read_pos))

    # -- Lifecycle --------------------------------------------------------

    def close(self) -> None:
        try:
            self._shm.close()
        except Exception:
            pass

    def _cleanup(self) -> None:
        try:
            self._shm.close()
            if self._owned:
                self._shm.unlink()
        except Exception:
            pass

    def unlink(self) -> None:
        try:
            self._shm.unlink()
        except Exception:
            pass
