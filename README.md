# AlfiePRIME Musiciser

A party-themed TUI/GUI music player and [SendSpin](https://github.com/music-assistant/aiosendspin) receiver for [Music Assistant](https://music-assistant.io/). Displays a retro boom box interface with a real-time spectrum analyzer, VU meters, party lights, dancing crowd, and dynamic album art colouring.

## Features

- **Real-time spectrum analyzer** — 32-band FFT with automatic gain control, peak hold indicators, and beat detection
- **Dynamic album art colours** — extracts a colour palette from the currently playing album art and themes the entire UI (titles, borders, spectrum gradient, party lights). Falls back to rainbow when no artwork is available
- **VU meters** — stereo level meters with square-root scaling, peak-hold markers, and shimmer animation
- **Party lights** — animated light strips and stereo-reactive dots synced to the music
- **Dance floor** — ASCII art DJ and 6 dancer types (headbanger, spinner, robot, raver) across 3 rows, energy-reactive to audio level and beat intensity, with BPM display
- **Braille album art** — renders album artwork as coloured Unicode braille dot art (2x4 pixel grid per character) next to the Now Playing panel
- **Listening stats** — persistent per-artist and per-track play time tracking, session summary in the status bar, auto-saved to `~/.config/alfieprime-musiciser/stats.json`
- **OS media integration** — registers with the OS so media keys, lock screen, and desktop widgets can see what's playing and send play/pause/next/previous commands (MPRIS2 on Linux, SMTC on Windows)
- **Desktop notifications** — shows a notification on track change via `notify-send` (Linux)
- **Terminal tab title** — sets the terminal tab/window title to the current track via OSC escape sequences
- **Transport controls** — play/pause, next, previous, shuffle, repeat, volume via keyboard or mouse click
- **CRT animations** — 3-phase startup (boot, static hold with animated antenna and radio wave ripples, diagonal lights-on sweep) and power-off effects
- **Standby screensaver** — floating phrases with dim particles after 5 minutes of idle, wakes instantly on playback
- **System stats** — CPU usage, memory, network throughput, and session uptime in the status bar
- **Artwork pre-caching** — upcoming track artwork is extracted on background threads for instant theme changes on track switch
- **Resizable UI** — all sections dynamically scale to fill the terminal or window. The spectrum analyzer expands to use all available vertical space
- **Two connection modes**:
  - **Listen (mDNS)** — advertises via `_sendspin._tcp.local.` so Music Assistant discovers and connects automatically (recommended)
  - **Connect** — connects to a specific SendSpin server URL
- **Settings menu** — in-app settings overlay (`/` key) with animated CRT background, configurable auto-play, auto-volume, FPS limit (5-120), artwork toggle, album art colour toggle, static colour picker (16 presets + custom hex), and an advanced section (`A` key) for editing client name and UUID
- **Persistent UI state** — remembers art mode, calm mode, and settings across restarts
- **Persistent device identity** — remembers its client ID across restarts so Music Assistant recognises it as the same speaker
- **Standalone GUI mode** — runs in its own tkinter window (separate process) so audio never stutters from rendering load
- **Daemon mode** — run as a headless service with `--daemon` for background audio playback without any UI
- **Demo mode** — runs with synthetic audio for testing without a server

## Requirements

- Python 3.12+
- A running [Music Assistant](https://music-assistant.io/) server (or any SendSpin-compatible server)
- An audio output device

## Installation

### From source (recommended)

```bash
git clone https://github.com/alfiecg24/AlfiePrimeSSClient.git
cd AlfiePrimeSSClient
pip install .
```

### Global install with pipx

```bash
pipx install git+https://github.com/alfiecg24/AlfiePrimeSSClient.git
```

### Windows one-click installer

Run `install.bat` — it will install Python dependencies via pipx and create desktop/Start Menu shortcuts.

To uninstall, run `uninstall.bat`.

## Usage

```bash
# First run — interactive setup wizard
alfieprime-musiciser

# Listen mode with custom name (default)
alfieprime-musiciser --name "Living Room"

# Connect to a specific server
alfieprime-musiciser ws://192.168.1.100:8097/sendspin

# Custom listen port
alfieprime-musiciser --port 9000

# Demo mode (no server needed)
alfieprime-musiciser --demo

# Standalone GUI window (no terminal needed)
alfieprime-musiciser --gui

# Headless daemon (audio only, no display)
alfieprime-musiciser --daemon

# GUI entry point (uses pythonw on Windows — no console window)
alfieprime-musiciser-app
```

### Keyboard Controls

| Key | Action |
|-----|--------|
| `P` | Play / Pause |
| `N` | Next track |
| `B` | Previous track |
| `S` | Toggle shuffle |
| `R` | Cycle repeat (off / all / one) |
| `A` | Toggle full-screen album art mode (party scene with dancers, confetti, fireworks) |
| `C` | Toggle calm mode in art view (full-screen art only, no party effects) |
| `↑` | Volume up |
| `↓` | Volume down |
| `/` | Open settings menu (auto play, auto volume, FPS, artwork, colours, advanced) |
| `Q` | Quit (pauses playback on Music Assistant first) |

## Configuration

Settings are stored in `~/.config/alfieprime-musiciser/config.json` and are created on first run via the interactive setup wizard. Re-run the wizard at any time with:

```bash
alfieprime-musiciser --setup
```

## Architecture

The codebase is split into focused modules with a clean dependency graph:

```
colors.py          Color utilities, ColorTheme, album art extraction
state.py           PlayerState dataclass
config.py          Config, setup wizard, connection test
visualizer.py      AudioVisualizer (FFT, beat/BPM detection, delay queue)
renderer.py        All render_* functions (spectrum, VU, party scene, braille art, stats)
tui.py             BoomBoxTUI (layout, CRT animations, standby screensaver, input, run loops)
receiver.py        SendSpinReceiver (WebSocket, audio, metadata, artwork, notifications)
stats.py           ListeningStats (persistent per-artist/track play time tracking)
mpris.py           MPRIS2 D-Bus integration (Linux media keys, lock screen, KDE Connect)
smtc.py            Windows SMTC integration (media keys, lock screen, taskbar overlay)
main.py            Entry point, argparse, _run_with_config
gui.py             tkinter GUI process (separate OS process via Pipe)
launcher.py        GUI entry point (pythonw on Windows)
```

```
Main Process                          GUI Process (optional)
+---------------------------------+   +------------------------+
| asyncio event loop              |   | tkinter mainloop       |
|                                 |   |                        |
| SendSpinReceiver                |   | Text widget            |
|   - WebSocket client            |   | - tag-based colouring  |
|   - Audio playback              |   | - batch rendering      |
|   - Metadata/artwork listeners  |   |                        |
|   - Desktop notifications       |   +------------------------+
|   - ListeningStats              |        ^           |
|                                 |        | segments  | size/keys
| MPRIS2Server (Linux)            |        |           v
|   or SMTCServer (Windows)       |   +------------------------+
|   - OS media key routing        |   | multiprocessing.Pipe   |
|   - Track info to desktop       |   +------------------------+
|                                 |
| AudioVisualizer                 |
|   - FFT spectrum analysis       |
|   - Beat/BPM detection          |
|   - Playback-synced delay queue |
|                                 |
| BoomBoxTUI                      |
|   - Rich layout rendering       |
|   - CRT startup/shutdown anims  |
|   - Standby screensaver         |
|   - Braille album art panel     |
+---------------------------------+
```

In GUI mode, the tkinter window runs in a **separate OS process** communicating via a pipe. This completely isolates rendering from audio playback — no stuttering regardless of UI complexity.

### Performance

The 30fps render loop is heavily optimised to minimise per-frame allocations and redundant computation:

- **Style object caching** — Rich `Style` objects are LRU-cached by `(color, bold, dim, italic)` key, eliminating thousands of allocations per frame
- **Color caching** — `_hsv_to_rgb`, `_rainbow_color`, `_theme_color`, `_lerp_color`, `_hex_to_rgb`, and `_rgb_to_hex` all use dict/LRU caches with quantised inputs (256–512 steps) so repeated calls become dict lookups
- **Hex formatting LUT** — a 256-element lookup table replaces `f"#{v:02x}"` formatting in all hot paths
- **VU meter gradient pre-computation** — the 5-zone colour gradient is computed once per theme change and reused across frames
- **FFT band bin boundaries** — frequency-to-bin mappings are pre-computed once per sample rate change, not every frame
- **Console reuse** — Rich Console objects are cached and reused across frames, only recreated on terminal resize
- **Character batching** — the party scene groups consecutive same-category characters into single `Text.append()` calls instead of per-character appends

### SendSpin Protocol

The app connects as a SendSpin client with four roles:

- **PLAYER** — receives and plays audio streams (PCM, FLAC)
- **METADATA** — receives track info, progress, playback state
- **CONTROLLER** — sends transport commands (play, pause, next, etc.)
- **ARTWORK** — receives album art (JPEG, 128x128) for dynamic theming

Album artwork arrives as binary WebSocket messages. Since the client library doesn't expose artwork listeners directly, the binary message handler is monkey-patched to intercept artwork channels.

## Dependencies

| Package | Purpose |
|---------|---------|
| [sendspin](https://pypi.org/project/sendspin/) | SendSpin client, audio device handling, FLAC decoding |
| [numpy](https://numpy.org/) | FFT spectrum analysis, audio signal processing |
| [rich](https://rich.readthedocs.io/) | Terminal UI rendering with 24-bit colour |
| [Pillow](https://pillow.readthedocs.io/) | Album art colour extraction via median-cut quantization |
| [psutil](https://psutil.readthedocs.io/) | System stats (CPU, memory, network) — optional |
| [dbus-next](https://github.com/altdesktop/python-dbus-next) | MPRIS2 media controls — Linux only, auto-installed |
| [winsdk](https://github.com/pywinrt/python-winsdk) | SMTC media controls — Windows only, auto-installed |

## License

MIT
