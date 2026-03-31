"""TUI subprocess entry point.

Launches BoomBoxTUI in its own process with its own AudioVisualizer.
PCM audio arrives via SharedPCMRings (zero-copy shared memory).
Metadata arrives via a pipe from the control process.
User commands go back via a multiprocessing Queue.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import multiprocessing.connection
import threading
import time

logger = logging.getLogger(__name__)


# ── PCM consumer thread ─────────────────────────────────────────────────────

def _pcm_consumer(
    ring_names: dict[str, str],
    dj_ring_names: dict[str, str],
    tui,
    viz_master,
    viz_dj_a,
    viz_dj_b,
) -> None:
    """Background thread: read PCM from SharedPCMRings and feed visualizers.

    In normal mode, reads from the active source's ring.
    In DJ mode, reads from the three DJ output rings.
    """
    from alfieprime_musiciser.shared_pcm import SharedPCMRing

    # Open source rings (one per receiver)
    rings: dict[str, SharedPCMRing] = {}
    for src, name in ring_names.items():
        try:
            rings[src] = SharedPCMRing(name, create=False)
        except Exception:
            logger.warning("Could not open PCM ring '%s' for source '%s'", name, src)

    # Open DJ output rings (created by control process when DJ activates)
    dj_rings: dict[str, SharedPCMRing | None] = {"a": None, "b": None, "mix": None}

    def _open_dj_rings() -> bool:
        """Try to open DJ rings.  Returns True if all opened."""
        for key in ("a", "b", "mix"):
            if dj_rings[key] is None and key in dj_ring_names:
                try:
                    dj_rings[key] = SharedPCMRing(dj_ring_names[key], create=False)
                except Exception:
                    pass
        return all(dj_rings[k] is not None for k in ("a", "b", "mix") if k in dj_ring_names)

    def _close_dj_rings() -> None:
        for key in list(dj_rings):
            if dj_rings[key] is not None:
                try:
                    dj_rings[key].close()
                except Exception:
                    pass
                dj_rings[key] = None

    _dj_rings_open = False
    _last_format: dict[str, tuple] = {}

    while getattr(tui, "_running", True):
        try:
            if tui._dj_mode:
                # DJ mode: read from mixer output rings
                if not _dj_rings_open:
                    _dj_rings_open = _open_dj_rings()
                    if not _dj_rings_open:
                        time.sleep(0.05)
                        continue

                for key, viz in (("a", viz_dj_a), ("b", viz_dj_b), ("mix", viz_master)):
                    ring = dj_rings.get(key)
                    if ring is None:
                        continue
                    if ring.consume_format_change():
                        sr, bd, ch = ring.get_format()
                        viz.set_format(sr, bd, ch)
                    data = ring.read(8192)
                    if len(data) > 0:
                        viz.feed_audio_float32(data)
            else:
                # Normal mode: read from active source ring
                if _dj_rings_open:
                    _close_dj_rings()
                    _dj_rings_open = False

                src = tui.state.active_source
                ring = rings.get(src)
                if ring is None:
                    time.sleep(0.02)
                    continue
                # Check format change
                if ring.consume_format_change():
                    sr, bd, ch = ring.get_format()
                    viz_master.set_format(sr, bd, ch)
                data = ring.read(8192)
                if len(data) > 0:
                    viz_master.set_paused(False)
                    viz_master.feed_audio_float32(data)

            time.sleep(0.005)  # ~200 Hz poll rate

        except Exception:
            logger.debug("PCM consumer error", exc_info=True)
            time.sleep(0.02)

    # Cleanup
    for r in rings.values():
        try:
            r.close()
        except Exception:
            pass
    _close_dj_rings()


# ── State receiver thread ────────────────────────────────────────────────────

def _state_receiver(
    pipe: multiprocessing.connection.Connection,
    tui,
    mixer_diag: "MixerDiagProxy",
) -> None:
    """Background thread: drain metadata updates from control process."""
    from alfieprime_musiciser.colors import ColorTheme
    from alfieprime_musiciser.shared_state import STATE_FIELDS

    _last_artwork_hash: int = 0

    while True:
        try:
            if not pipe.poll(0.05):
                continue
            # Drain all pending updates, use the latest
            data = pipe.recv()
            while pipe.poll():
                data = pipe.recv()
        except (EOFError, OSError):
            break
        if data is None:
            break

        # -- Update PlayerState fields --
        state = tui.state
        for f in STATE_FIELDS:
            if f in data:
                setattr(state, f, data[f])

        # Re-base progress_update_time to local monotonic clock
        remote_mono = data.get("_progress_update_mono", 0.0)
        if remote_mono > 0:
            state.progress_update_time = time.monotonic()

        # ColorTheme
        td = data.get("theme_dict")
        if td:
            state.theme = ColorTheme(**{k: v for k, v in td.items() if v is not None})

        # Artwork (only when changed)
        art = data.get("artwork_data")
        if art is not None:
            h = hash(art) if art else 0
            if h != _last_artwork_hash:
                state.artwork_data = art
                _last_artwork_hash = h

        # Source volumes and snapshots
        sv = data.get("_source_volumes")
        if sv is not None:
            state._source_volumes = sv
        ss = data.get("_source_snapshots")
        if ss is not None:
            state._source_snapshots = ss

        # DJ mixer diagnostics
        mixer_diag._update(data)

        # Source B data for DJ screen
        sb = data.get("source_b_data")
        if sb is not None:
            tui._source_b_cache = sb


# ── TUI subprocess entry ─────────────────────────────────────────────────────

def tui_main(
    state_pipe: multiprocessing.connection.Connection,
    cmd_queue: multiprocessing.Queue,
    dj_array: multiprocessing.Array,
    config_dict: dict,
    ring_names: dict[str, str],
    dj_ring_names: dict[str, str],
) -> None:
    """Entry point for the TUI subprocess.

    Called via ``multiprocessing.Process(target=tui_main, ...)``.
    """
    from alfieprime_musiciser.config import Config
    from alfieprime_musiciser.shared_state import MixerDiagProxy, SharedDJState
    from alfieprime_musiciser.tui import BoomBoxTUI
    from alfieprime_musiciser.visualizer import AudioVisualizer

    # ── Build config ──
    config = Config(**config_dict)

    # ── Create REAL visualizers (FFT runs in this process) ──
    viz_master = AudioVisualizer()
    viz_dj_a = AudioVisualizer()
    viz_dj_b = AudioVisualizer()
    mixer_diag = MixerDiagProxy()
    shared_dj = SharedDJState(dj_array)

    # ── Create TUI with real visualizer ──
    tui = BoomBoxTUI(viz_master, config=config)

    # Replace DJState with shared version
    tui._dj_state = shared_dj

    # Cache for source-B data (populated by state receiver)
    tui._source_b_cache: dict = {}

    # ── Monkey-patch callbacks to route through command queue ──
    tui._command_callback = lambda cmd: cmd_queue.put_nowait(("transport", cmd))
    tui._sendspin_command_callback = lambda cmd: cmd_queue.put_nowait(("sendspin_cmd", cmd))
    tui._source_switch_callback = lambda src: cmd_queue.put_nowait(("source_switch", src))
    tui._airplay_dj_play_pause = lambda pause: cmd_queue.put_nowait(("airplay_dj_pp", pause))
    tui._spotify_dj_play_pause = lambda pause: cmd_queue.put_nowait(("spotify_dj_pp", pause))
    tui._spotify_command_callback = lambda cmd: cmd_queue.put_nowait(("spotify_cmd", cmd))

    # ── Override DJ lifecycle to route through command queue ──
    def _start_dj_proxy() -> None:
        tui._dj_viz_a = viz_dj_a
        tui._dj_viz_b = viz_dj_b
        tui._dj_mixer = mixer_diag  # provides diagnostic counters
        tui._dj_mode = True
        viz_master.set_paused(False)
        cmd_queue.put_nowait(("dj_activate", True))
        logger.info("DJ mode activated (TUI process)")

    def _stop_dj_proxy() -> None:
        tui._dj_mode = False
        cmd_queue.put_nowait(("dj_activate", False))
        tui._dj_viz_a = None
        tui._dj_viz_b = None
        tui._dj_mixer = None
        logger.info("DJ mode deactivated (TUI process)")

    tui._start_dj_mode = _start_dj_proxy
    tui._stop_dj_mode = _stop_dj_proxy

    # ── Override _dj_source_data to use cached source-B data ──
    _orig_dj_source_data = tui._dj_source_data
    from alfieprime_musiciser.colors import ColorTheme

    def _dj_source_data_proxy(source: str) -> dict:
        if source.endswith("_b"):
            return tui._source_b_cache or {
                "title": "", "artist": "", "album": "", "artwork_data": b"",
                "theme": ColorTheme(), "is_playing": False,
                "progress_ms": 0, "duration_ms": 0,
                "server_name": "", "codec": "pcm",
                "sample_rate": 48000, "bit_depth": 16,
            }
        return _orig_dj_source_data(source)

    tui._dj_source_data = _dj_source_data_proxy

    # ── Override stop to signal control process ──
    def _stop_proxy() -> None:
        if tui._dj_mode:
            _stop_dj_proxy()
        tui._running = False
        cmd_queue.put_nowait(("quit",))

    tui.stop = _stop_proxy
    tui._cleanup_fn = lambda: cmd_queue.put_nowait(("quit",))

    # ── Start PCM consumer thread (reads from SharedPCMRings) ──
    pcm_thread = threading.Thread(
        target=_pcm_consumer,
        args=(ring_names, dj_ring_names, tui, viz_master, viz_dj_a, viz_dj_b),
        daemon=True,
    )
    pcm_thread.start()

    # ── Start state receiver thread (metadata only, no viz data) ──
    recv_thread = threading.Thread(
        target=_state_receiver,
        args=(state_pipe, tui, mixer_diag),
        daemon=True,
    )
    recv_thread.start()

    # ── Run TUI event loop ──
    try:
        asyncio.run(tui.run())
    except KeyboardInterrupt:
        pass
    finally:
        cmd_queue.put_nowait(("quit",))
