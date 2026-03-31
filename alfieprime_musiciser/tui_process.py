"""TUI subprocess entry point.

Launches BoomBoxTUI in its own process so rendering never competes
with the audio pipeline for the GIL.  Receives state updates via a
pipe and sends user commands back via a queue.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import multiprocessing.connection
import sys
import threading
import time

logger = logging.getLogger(__name__)


# ── State receiver thread ────────────────────────────────────────────────────

def _state_receiver(
    pipe: multiprocessing.connection.Connection,
    tui,
    viz: "VisualizerProxy",
    dj_viz_a: "VisualizerProxy",
    dj_viz_b: "VisualizerProxy",
    mixer_diag: "MixerDiagProxy",
) -> None:
    """Background thread: drain state updates from control process."""
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

        # -- Update visualizer proxy --
        viz._update(data)

        # -- DJ mode --
        dj_active = data.get("dj_active", False)
        if dj_active:
            va = data.get("dj_viz_a")
            if va:
                b, p, vl, vr, bc, bi = va
                dj_viz_a._bands = b
                dj_viz_a._peaks = p
                dj_viz_a._vu_left = vl
                dj_viz_a._vu_right = vr
                dj_viz_a._beat_count = bc
                dj_viz_a._beat_intensity = bi
            vb = data.get("dj_viz_b")
            if vb:
                b, p, vl, vr, bc, bi = vb
                dj_viz_b._bands = b
                dj_viz_b._peaks = p
                dj_viz_b._vu_left = vl
                dj_viz_b._vu_right = vr
                dj_viz_b._beat_count = bc
                dj_viz_b._beat_intensity = bi
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
) -> None:
    """Entry point for the TUI subprocess.

    Called via ``multiprocessing.Process(target=tui_main, ...)``.
    """
    # Late imports so only the TUI process pays the import cost
    from alfieprime_musiciser.config import Config
    from alfieprime_musiciser.shared_state import (
        MixerDiagProxy,
        SharedDJState,
        VisualizerProxy,
    )
    from alfieprime_musiciser.tui import BoomBoxTUI

    # ── Build config ──
    config = Config(**config_dict)

    # ── Create proxy objects ──
    viz_proxy = VisualizerProxy()
    dj_viz_a = VisualizerProxy()
    dj_viz_b = VisualizerProxy()
    mixer_diag = MixerDiagProxy()
    shared_dj = SharedDJState(dj_array)

    # ── Create TUI (uses proxy visualizer) ──
    tui = BoomBoxTUI(viz_proxy, config=config)

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
    _orig_start_dj = tui._start_dj_mode
    _orig_stop_dj = tui._stop_dj_mode

    def _start_dj_proxy() -> None:
        tui._dj_viz_a = dj_viz_a
        tui._dj_viz_b = dj_viz_b
        tui._dj_mixer = mixer_diag  # provides diagnostic counters
        tui._dj_mode = True
        viz_proxy.set_paused(False)
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

    from alfieprime_musiciser.colors import ColorTheme
    tui._dj_source_data = _dj_source_data_proxy

    # ── Override stop to signal control process ──
    _orig_stop = tui.stop

    def _stop_proxy() -> None:
        if tui._dj_mode:
            _stop_dj_proxy()
        tui._running = False
        cmd_queue.put_nowait(("quit",))

    tui.stop = _stop_proxy

    # Set cleanup function for clean shutdown
    tui._cleanup_fn = lambda: cmd_queue.put_nowait(("quit",))

    # ── Start state receiver thread ──
    recv_thread = threading.Thread(
        target=_state_receiver,
        args=(state_pipe, tui, viz_proxy, dj_viz_a, dj_viz_b, mixer_diag),
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
