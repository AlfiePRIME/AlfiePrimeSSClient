#!/bin/sh
# Lightweight event script for librespot's --onevent callback.
# Uses pure shell to avoid Python interpreter startup latency which
# can stall librespot and cause track skipping.
#
# librespot sets these environment variables:
#   PLAYER_EVENT  - event type (playing, paused, stopped, changed, etc.)
#   TRACK_ID      - Spotify track URI
#   OLD_TRACK_ID  - previous track URI
#   VOLUME        - volume level 0-65535
#   POSITION_MS   - playback position in ms
#   DURATION_MS   - track duration in ms

[ -z "$MUSICISER_EVENT_PIPE" ] && exit 0
[ -z "$PLAYER_EVENT" ] && exit 0
[ ! -p "$MUSICISER_EVENT_PIPE" ] && exit 0

# Build JSON payload — shell string concatenation (no jq dependency)
JSON="{\"event\":\"${PLAYER_EVENT}\",\"track_id\":\"${TRACK_ID:-}\",\"old_track_id\":\"${OLD_TRACK_ID:-}\",\"position_ms\":\"${POSITION_MS:-0}\",\"duration_ms\":\"${DURATION_MS:-0}\",\"volume\":\"${VOLUME:-}\"}"

# Non-blocking write to the named pipe.
# If the reader isn't connected or the pipe is full, silently drop.
# The dd trick avoids blocking: open O_WRONLY|O_NONBLOCK via /proc
exec 3>"$MUSICISER_EVENT_PIPE" 2>/dev/null && {
    printf '%s\n' "$JSON" >&3 2>/dev/null
    exec 3>&-
}

exit 0
