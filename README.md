# AlfiePRIME Musiciser

A party-themed TUI/GUI music player and multi-protocol audio receiver for [Music Assistant](https://music-assistant.io/). Stream from any Apple device via **AirPlay 2**, cast from the Spotify app via **Spotify Connect**, or receive from Music Assistant via **SendSpin** — up to two sources simultaneously. Features a retro boom box interface with real-time spectrum analysis, VU meters, party lights, dynamic album art colouring, and a two-channel DJ mixing console.

<!-- Screenshots: place PNG images (800-1200px wide) in a docs/screenshots/ directory -->
<!-- ![Boombox](docs/screenshots/boombox.png) -->

## Features

### Audio Sources

- **SendSpin** — WebSocket-based receiver for [Music Assistant](https://music-assistant.io/). Full transport controls, metadata, and artwork
- **AirPlay 2** — appears as an AirPlay speaker on your network. Stream from any iPhone, iPad, or Mac with automatic codec negotiation (PCM, ALAC, AAC) and transient pairing
- **Spotify Connect** — appears as a Spotify Connect device via [librespot](https://github.com/librespot-org/librespot). Cast from any Spotify app. Optional Web API integration for rich metadata, artwork, and transport controls
- **Multi-source** — run all three receivers simultaneously. Up to 2 sources can be connected at once. Switch between active sources with `T`. Each source maintains independent playback state with seamless hot-switching

### Visualisation

- **Spectrum analyzer** — 32-band FFT with automatic gain control, peak hold, and BPM display
- **Dynamic album art colours** — extracts a palette from album art and themes the entire UI. Falls back to rainbow cycling when no artwork is available
- **VU meters** — stereo level meters with peak-hold markers and shimmer
- **Party lights** — animated light strips synced to bass and treble energy
- **Dance floor** — ASCII DJ with hype mode, 12 dancer types across depth rows, BPM-scaled animation
- **Braille album art** — Unicode braille dot art (2x4 pixel grid) with Floyd-Steinberg dithering

### System Integration

- **OS media controls** — MPRIS2 (Linux) / SMTC (Windows) for media keys, lock screen, and desktop widgets
- **Desktop notifications** — track change notifications via `notify-send` (Linux)
- **Listening stats** — persistent per-artist/track play time, auto-saved to config directory
- **Terminal tab title** — sets terminal title to current track via OSC escape sequences

## Requirements

- Python 3.12+
- A [Music Assistant](https://music-assistant.io/) server and/or AirPlay 2 sender and/or Spotify app
- An audio output device
- `libasound2` (Linux) for AirPlay/Spotify audio playback
- `librespot` binary for Spotify Connect (`pacman -S librespot` or `cargo install librespot`)

## Installation

### Linux / macOS (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/AlfiePRIME/AlfiePRIME-Musiciser/main/install.sh | bash
```

or with wget:

```bash
wget -qO- https://raw.githubusercontent.com/AlfiePRIME/AlfiePRIME-Musiciser/main/install.sh | bash
```

Checks for Python 3.12+, git, and pipx. Clones the repo and installs via pipx. Run again to update.

### Windows (one-liner)

```powershell
powershell -c "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/AlfiePRIME/AlfiePRIME-Musiciser/main/install.bat' -OutFile '$env:TEMP\install.bat'; Start-Process '$env:TEMP\install.bat'"
```

Checks for Python 3.12+ and git. Clones the repo, installs via pipx, and creates desktop/Start Menu shortcuts. Run `uninstall.bat` to remove.

### Manual install

```bash
git clone https://github.com/AlfiePRIME/AlfiePRIME-Musiciser.git
cd AlfiePRIME-Musiciser
pipx install .
```

## Usage

```bash
# First run — interactive setup wizard
alfieprime-musiciser

# Listen mode with custom name (default)
alfieprime-musiciser --name "Living Room"

# Connect to a specific server
alfieprime-musiciser ws://192.168.1.100:8097/sendspin

# Custom Spotify device name
alfieprime-musiciser --spotify-name "My Speaker"

# Demo mode (no server needed)
alfieprime-musiciser --demo

# Standalone GUI window (no terminal needed)
alfieprime-musiciser --gui

# Headless daemon (audio only, no display)
alfieprime-musiciser --daemon

# Re-run setup wizard
alfieprime-musiciser --setup
```

---

## Screens

### Boombox (Main Screen)

The default view. A retro boom box with real-time audio visualisation.

| Section | Description |
|---------|-------------|
| **Title banner** | Track title with dynamic colour wave animation |
| **Now Playing** | Artist, album, codec info, progress bar with timestamps |
| **Braille art** | Album artwork as coloured Unicode braille dots |
| **Transport controls** | Play/Pause, Previous, Next, Shuffle, Repeat — clickable with mouse. Hidden when unsupported by the active source |
| **VU meters** | Stereo level meters with peak-hold markers |
| **Volume gauge** | Current volume with coloured bar |
| **Spectrum analyzer** | 32-band FFT visualiser. Expands to fill available height |
| **Party lights** | Animated light strips synced to audio energy |
| **Dance floor** | ASCII DJ with hype mode, dancers, energy-reactive crowd |
| **Status bar** | Source, codec, system stats, listening stats |

| Key | Action |
|-----|--------|
| `P` | Play / Pause |
| `N` / `B` | Next / Previous track |
| `S` / `R` | Toggle shuffle / Cycle repeat |
| `M` | Toggle mute |
| `↑` / `↓` | Volume up / down |
| `A` | Toggle album art mode |
| `D` | Enter DJ mode |
| `T` | Switch active source |
| `/` | Open settings |
| `Q` | Quit |

---

### Album Art Mode

Full-screen album art with two sub-modes.

**Party Mode** (`A`): Large braille art centred on screen with wandering dancers, confetti, fireworks, beat-reactive colour pulses, and binary background animation.

**Calm Mode** (`C`): Large braille art on the left with an info panel on the right showing track details, album, codec, and audio format.

| Key | Action |
|-----|--------|
| `A` | Toggle art mode on/off |
| `C` | Toggle calm/party sub-mode |
| `P` / `N` / `B` | Play-Pause / Next / Previous |
| `↑` / `↓` | Volume up / down |
| `D` | Enter DJ mode |

---

### DJ Mixing Console

A two-channel software audio mixer for live crossfading between sources. Enter with `D`.

| Section | Description |
|---------|-------------|
| **Decks A & B** | Animated turntable with spinning vinyl, tonearm, track info, connection status |
| **Center mixer** | Per-channel stereo VU meters, crossfader slider |
| **EQ section** | 3-band EQ per channel (Bass/Mid/Treble, +/-12 dB) |
| **Master output** | Full-width spectrum analyser of the mixed output |

**Audio chain:** Input -> Float32 -> Mono->Stereo -> Resample 48kHz -> 3-Band EQ -> Per-channel volume -> Equal-power crossfade -> Master output

**Smart Fade** (`F`): Auto-crossfade using BPM-synced timing (8 beats, 2-8s) with ease-in-out curve and bass ducking on the outgoing channel.

**DJ Source Modes** (configurable in settings):
- **Mixed** — auto-detects any two connected sources
- **Dual SendSpin** / **Dual AirPlay** / **Dual Spotify** — both channels from the same protocol
- **SS+SP** / **AP+SP** — Spotify paired with SendSpin or AirPlay

| Key | Action |
|-----|--------|
| `Tab` | Switch channel focus (A / B) |
| `←` / `→` | Crossfader +/-5% |
| `↑` / `↓` | Focused channel volume +/-5 |
| `1`/`2`/`3` | Bass/Mid/Treble EQ +2 dB (`Shift` for -2 dB) |
| `0` | Reset EQ to flat |
| `X` | Center crossfader |
| `F` | Smart Fade |
| `P` | Play / Pause both |
| `D` | Exit DJ mode |

---

### Settings Menu

Tab-based overlay opened with `/`. Animated CRT scanline background with fade transitions and scattered dancing easter egg.

Navigate tabs with `←`/`→`, `Tab`, or number keys `1`-`6`. Press `?` on any setting for a help description.

**General** — Auto Play, Auto Volume, FPS Limit, Brightness, Show Artwork, Album Art Colours, Static Colour

**SendSpin** — SendSpin Receiver, Device Swap Prompt, Auto Action

**AirPlay** — AirPlay Receiver, Remember Devices

**Spotify** *(Linux/macOS only)* — Spotify Connect, Bitrate, Device Name, Remember Devices, Username, Web API Client ID

**DJ Mode** — DJ Source Mode, Open DJ on Start, Album Art Colours

**Advanced** — Client Name, Client UUID, Re-run Setup, Reset Config

| Key | Action |
|-----|--------|
| `←` / `→` | Switch tab (or adjust value on adjustable items) |
| `1`-`6` / `Tab` | Jump to tab |
| `↑` / `↓` | Navigate items |
| `Enter` | Toggle / edit |
| `?` | Help for selected setting |
| `Esc` / `C` | Close settings |

---

### CRT Animations

- **Startup** — 3-phase boot sequence: phosphor warmup, static hold with animated antenna and radio waves (shows "Connecting..." with spinner), diagonal lights-on sweep
- **Shutdown** — content noise and vertical collapse, "Shutting down..." static hold while receivers clean up, line shrink to dot with afterglow fade
- **Mode transitions** — CRT-style collapse and expand when switching between screens
- **Standby** — floating phrases in a bouncing box with dim particles after 5 minutes idle

---

## Configuration

Settings are stored in `~/.config/alfieprime-musiciser/config.json` and created on first run via the setup wizard. Re-run with `--setup`.

## Architecture

```
colors.py          Color utilities, ColorTheme, album art palette extraction
state.py           PlayerState dataclass, source snapshots, toast notifications
config.py          Config dataclass, setup wizard, connection test
visualizer.py      AudioVisualizer (FFT, beat/BPM detection, delay queue)
renderer.py        All render functions (spectrum, VU, party scene, braille art)
tui.py             BoomBoxTUI (layout, input, run loops) — uses mixins:
tui_settings.py    SettingsMixin (tab-based settings, colour picker, reset)
tui_animations.py  AnimationsMixin (CRT startup/shutdown, standby, transitions)
tui_dj.py          DJMixin (DJ console, turntable rendering, EQ display)
dj_mixer.py        DJMixer (ring buffers, 3-band biquad EQ, crossfader)
dj_state.py        DJState, ChannelState (per-channel state)
receiver.py        SendSpinReceiver (WebSocket, audio, metadata, artwork)
airplay/           AirPlay 2 receiver package
  receiver.py      AirPlayReceiver (RTSP, metadata, PCM consumer, mDNS)
  vendor/          Vendored ap2-receiver with pipeline patches
spotify/           Spotify Connect receiver package
  receiver.py      SpotifyConnectReceiver (librespot subprocess, PCM reader,
                     Web API wrapper, metadata polling, transport controls)
stats.py           ListeningStats (persistent play time tracking)
mpris.py           MPRIS2 D-Bus integration (Linux)
smtc.py            Windows SMTC integration
main.py            Entry point, argparse, receiver wiring, signal handling
gui.py             tkinter GUI process (separate OS process via Pipe)
launcher.py        GUI entry point (pythonw on Windows)
```

In GUI mode, the tkinter window runs in a **separate OS process** communicating via a pipe, completely isolating rendering from audio playback.

### Performance

- **Style/colour caching** — Rich `Style` objects and colour conversions are LRU-cached, eliminating thousands of allocations per frame
- **Hex formatting LUT** — 256-element lookup table replaces `f"#{v:02x}"` in hot paths
- **Pre-computation** — VU gradients, FFT bin boundaries, and braille art are computed once and reused
- **Console reuse** — Rich Console objects cached across frames, recreated only on resize
- **Character batching** — party scene groups consecutive same-category characters into single appends

## Dependencies

| Package | Purpose |
|---------|---------|
| [sendspin](https://pypi.org/project/sendspin/) | SendSpin client, audio device handling, FLAC decoding |
| [numpy](https://numpy.org/) | FFT spectrum analysis, audio signal processing |
| [rich](https://rich.readthedocs.io/) | Terminal UI rendering with 24-bit colour |
| [Pillow](https://pillow.readthedocs.io/) | Album art colour extraction via median-cut quantization |
| [PyAudio](https://pypi.org/project/PyAudio/) | AirPlay/Spotify audio output via PortAudio |
| [zeroconf](https://pypi.org/project/zeroconf/) | mDNS advertisement for AirPlay 2 discovery |
| [cryptography](https://cryptography.io/) | AirPlay HAP pairing (ed25519, x25519) |
| [PyCryptodome](https://pypi.org/project/pycryptodome/) | AirPlay ChaCha20-Poly1305 encryption |
| [biplist](https://pypi.org/project/biplist/) | Binary plist parsing for AirPlay 2 metadata |
| [hkdf](https://pypi.org/project/hkdf/) | HKDF key derivation for AirPlay pairing |
| [spotipy](https://pypi.org/project/spotipy/) | Spotify Web API (OAuth PKCE) — optional, for metadata/controls |
| [psutil](https://psutil.readthedocs.io/) | System stats (CPU, memory, network) — optional |
| [dbus-next](https://github.com/altdesktop/python-dbus-next) | MPRIS2 media controls — Linux only |
| [winsdk](https://github.com/pywinrt/python-winsdk) | SMTC media controls — Windows only, optional |

## License

MIT
