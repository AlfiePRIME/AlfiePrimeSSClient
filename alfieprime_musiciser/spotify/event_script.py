#!/usr/bin/env python3
"""Standalone event script for librespot's --onevent callback.

librespot invokes this script as a subprocess with event details in
environment variables.  We serialise the event as JSON and write it to a
named pipe so the main process can pick it up without polling.

Environment variables set by librespot:
    PLAYER_EVENT  – event type (e.g. "playing", "paused", "stopped",
                    "track_changed", "volume_set")
    TRACK_ID      – Spotify track URI (on track_changed / playing)
    OLD_TRACK_ID  – previous track URI (on track_changed)
    VOLUME        – volume level 0-65535 (on volume_set)
    POSITION_MS   – playback position in ms
    DURATION_MS   – track duration in ms
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    pipe_path = os.environ.get("MUSICISER_EVENT_PIPE", "")
    if not pipe_path:
        return

    event = os.environ.get("PLAYER_EVENT", "")
    if not event:
        return

    payload = {
        "event": event,
        "track_id": os.environ.get("TRACK_ID", ""),
        "old_track_id": os.environ.get("OLD_TRACK_ID", ""),
        "position_ms": os.environ.get("POSITION_MS", "0"),
        "duration_ms": os.environ.get("DURATION_MS", "0"),
        "volume": os.environ.get("VOLUME", ""),
        "timestamp": time.time(),
    }

    try:
        # Open the pipe in non-blocking write mode with a short timeout.
        # If the reader isn't connected we silently drop the event.
        fd = os.open(pipe_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (json.dumps(payload) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass  # pipe not ready – drop silently


if __name__ == "__main__":
    main()
