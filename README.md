# AlfiePRIME Musiciser

A party-themed TUI/GUI music player and [SendSpin](https://github.com/music-assistant/aiosendspin) receiver for [Music Assistant](https://music-assistant.io/). Displays a retro boom box interface with a real-time spectrum analyzer, VU meters, party lights, dancing crowd, and dynamic album art colouring.

## Features

- **Real-time spectrum analyzer** — 32-band FFT with automatic gain control, peak hold indicators, and beat detection
- **Dynamic album art colours** — extracts a colour palette from the currently playing album art and themes the entire UI (titles, borders, spectrum gradient, party lights). Falls back to rainbow when no artwork is available
- **VU meters** — stereo level meters with square-root scaling, peak-hold markers, and shimmer animation
- **Party lights** — animated light strips and stereo-reactive dots synced to the music
- **Dance floor** — ASCII art DJ and dancing crowd that bounce to detected beats, with BPM display and depth-layered rows at larger terminal sizes
- **Transport controls** — play/pause, next, previous, shuffle, repeat, volume via keyboard or mouse click
- **CRT animations** — 3-phase startup (boot, static hold with animated connecting screen, diagonal lights-on sweep) and power-off effects
- **System stats** — CPU usage, memory, network throughput, and session uptime in the status bar
- **Artwork pre-caching** — upcoming track artwork is extracted on background threads for instant theme changes on track switch
- **Resizable UI** — all sections dynamically scale to fill the terminal or window. The spectrum analyzer expands to use all available vertical space
- **Two connection modes**:
  - **Listen (mDNS)** — advertises via `_sendspin._tcp.local.` so Music Assistant discovers and connects automatically (recommended)
  - **Connect** — connects to a specific SendSpin server URL
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
| `↑` | Volume up |
| `↓` | Volume down |
| `Q` | Quit |

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
renderer.py        All render_* functions (spectrum, VU, party scene, stats)
tui.py             BoomBoxTUI (layout, CRT animations, input, run loops)
receiver.py        SendSpinReceiver (WebSocket, audio, metadata, artwork)
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
|                                 |   +------------------------+
| AudioVisualizer                 |        ^           |
|   - FFT spectrum analysis       |        | segments  | size/keys
|   - Beat/BPM detection          |        |           v
|   - Playback-synced delay queue |   +------------------------+
|                                 |   | multiprocessing.Pipe   |
| BoomBoxTUI                      |   +------------------------+
|   - Rich layout rendering       |
|   - CRT startup/shutdown anims  |
+---------------------------------+
```

In GUI mode, the tkinter window runs in a **separate OS process** communicating via a pipe. This completely isolates rendering from audio playback — no stuttering regardless of UI complexity.

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

## License

MIT
