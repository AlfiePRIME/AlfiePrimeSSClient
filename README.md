# AlfiePRIME Musiciser

A party-themed TUI/GUI music player and [SendSpin](https://github.com/music-assistant/aiosendspin) receiver for [Music Assistant](https://music-assistant.io/). Displays a retro boom box interface with a real-time spectrum analyzer, VU meters, party lights, dancing crowd, and dynamic album art colouring.

## Features

- **Real-time spectrum analyzer** — 32-band FFT with automatic gain control, peak hold indicators, and beat detection
- **Dynamic album art colours** — extracts a colour palette from the currently playing album art and themes the entire UI (titles, borders, spectrum gradient, party lights). Falls back to rainbow when no artwork is available
- **VU meters** — stereo level meters with themed gradients
- **Party lights** — animated light strips and stereo-reactive dots synced to the music
- **Dance floor** — ASCII art DJ and dancing crowd that bounce to detected beats, with depth-layered rows at larger terminal sizes
- **Transport controls** — play/pause, next, previous, shuffle, repeat via keyboard or mouse click
- **CRT animations** — old-school CRT power-on/power-off effects on startup and shutdown
- **Resizable UI** — all sections dynamically scale to fill the terminal or window. The spectrum analyzer expands to use all available vertical space
- **Two connection modes**:
  - **Listen (mDNS)** — advertises via `_sendspin._tcp.local.` so Music Assistant discovers and connects automatically (recommended)
  - **Connect** — connects to a specific SendSpin server URL
- **Persistent device identity** — remembers its client ID across restarts so Music Assistant recognises it as the same speaker
- **Standalone GUI mode** — runs in its own tkinter window (separate process) so audio never stutters from rendering load
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
| `Q` | Quit |

## Configuration

Settings are stored in `~/.config/alfieprime-musiciser/config.json` and are created on first run via the interactive setup wizard. Re-run the wizard at any time with:

```bash
alfieprime-musiciser --setup
```

## Architecture

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
|   - Beat detection              |        |           v
|   - Playback-synced delay queue |   +------------------------+
|                                 |   | multiprocessing.Pipe   |
| BoomBoxTUI                      |   +------------------------+
|   - Rich layout rendering       |
|   - CRT animations              |
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

## License

MIT
