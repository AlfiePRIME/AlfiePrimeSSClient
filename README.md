# AlfiePRIME Musiciser

A party-themed TUI/GUI music player, [SendSpin](https://github.com/music-assistant/aiosendspin) receiver, and **AirPlay 2 receiver** for [Music Assistant](https://music-assistant.io/). Displays a retro boom box interface with a real-time spectrum analyzer, VU meters, party lights, dancing crowd, and dynamic album art colouring. Stream from any Apple device via AirPlay or from Music Assistant via SendSpin — simultaneously if you like.

<!-- Screenshots: place PNG images (800-1200px wide) in a docs/screenshots/ directory -->
<!-- ![Boombox](docs/screenshots/boombox.png) -->

## Features

- **Real-time spectrum analyzer** — 32-band FFT with automatic gain control, peak hold indicators, and smoothed BPM-tracking beat detection
- **Dynamic album art colours** — extracts a colour palette from the currently playing album art and themes the entire UI (titles, borders, spectrum gradient, party lights). Falls back to rainbow when no artwork is available
- **VU meters** — stereo level meters with square-root scaling, peak-hold markers, and shimmer animation
- **Party lights** — animated light strips and stereo-reactive dots synced to the music
- **Dance floor** — ASCII art DJ (with hype mode at high energy) and 12 dancer types (6 male + 6 female variants: swayer, jumper, headbanger, spinner, robot, raver) across multiple depth rows, smoothed energy-reactive crowd density (fast attack, slow decay), BPM-scaled animation, and beat-reactive DJ equipment glow
- **AirPlay 2 receiver** — appears as an AirPlay speaker on your network. Stream audio from any iPhone, iPad, or Mac. Receives album artwork, track metadata (title, artist, album, duration), and progress. Supports transient pairing (no PIN required), buffered and realtime audio streams, and automatic codec negotiation (PCM, ALAC, AAC)
- **Dual-protocol support** — run AirPlay and SendSpin receivers simultaneously. Switch between active sources with `T` when both are connected. Each source maintains independent playback state (artwork, metadata, progress) with seamless hot-switching
- **DJ mixing console** — two-channel software mixer with per-channel 3-band EQ, equal-power crossfader, smart auto-fade with bass ducking, per-deck animated turntables, VU meters, and a master spectrum output
- **Full-screen album art mode** — visually-square braille art centred on screen with party effects (wandering dancers, confetti, fireworks) or calm mode (large art with album info panel showing artist, album, year, track number, and codec details). Toggle with `A`, calm with `C`
- **Braille album art** — renders album artwork as coloured Unicode braille dot art (2×4 pixel grid per character) with Floyd-Steinberg dithering next to the Now Playing panel
- **Listening stats** — persistent per-artist and per-track play time tracking, session summary in the status bar, auto-saved to `~/.config/alfieprime-musiciser/stats.json`
- **OS media integration** — registers with the OS so media keys, lock screen, and desktop widgets can see what's playing and send play/pause/next/previous commands (MPRIS2 on Linux, SMTC on Windows)
- **Desktop notifications** — shows a notification on track change via `notify-send` (Linux)
- **Connection toast notifications** — on-screen overlay showing device name, protocol, and assigned source when a client connects or disconnects (auto-dismisses after 3 seconds)
- **Terminal tab title** — sets the terminal tab/window title to the current track via OSC escape sequences
- **Transport controls** — play/pause, next, previous, shuffle, repeat, volume via keyboard or mouse click
- **CRT animations** — 3-phase startup (boot, static hold with animated antenna and radio wave ripples, diagonal lights-on sweep) and power-off effects. After 2 minutes of connecting, displays a "is your server running?" hint while still listening
- **Standby screensaver** — floating phrases in a bordered box with dim particles after 5 minutes of idle, wakes instantly on playback or any keypress
- **System stats** — CPU usage, memory, network throughput, and session uptime in the status bar
- **Artwork pre-caching** — upcoming track artwork is extracted on background threads for instant theme changes on track switch
- **Resizable UI** — all sections dynamically scale to fill the terminal or window. The spectrum analyzer expands to use all available vertical space
- **Three connection modes**:
  - **Listen (mDNS)** — advertises via `_sendspin._tcp.local.` so Music Assistant discovers and connects automatically (recommended)
  - **Connect** — connects to a specific SendSpin server URL
  - **AirPlay** — advertises as an AirPlay 2 speaker via mDNS (enabled by default, toggle in settings)
- **Settings menu** — in-app settings overlay (`/` key) with animated CRT background, fade transitions, scattered dancing easter egg, configurable auto-play, auto-volume, FPS limit (5–120), brightness slider (50–150%), artwork toggle, album art colour toggle, static colour picker (16 presets + custom hex), protocol settings (enable/disable AirPlay/SendSpin, device swap prompt, forget AirPlay devices), and an advanced section for editing client name, UUID, and factory-resetting the config
- **Persistent UI state** — remembers art mode, calm mode, and settings across restarts
- **Persistent device identity** — remembers its client ID across restarts so Music Assistant recognises it as the same speaker
- **Standalone GUI mode** — runs in its own tkinter window (separate process) so audio never stutters from rendering load
- **Daemon mode** — run as a headless service with `--daemon` for background audio playback without any UI
- **Demo mode** — runs with synthetic audio for testing without a server

## Requirements

- Python 3.12+
- A running [Music Assistant](https://music-assistant.io/) server (or any SendSpin-compatible server), and/or any AirPlay 2 sender (iPhone, iPad, Mac)
- An audio output device
- `libasound2` (Linux) or equivalent for AirPlay audio playback

## Installation

### From source (recommended)

```bash
git clone https://github.com/AlfiePRIME/AlfiePRIME-Musiciser.git
cd AlfiePRIME-Musiciser
pip install .
```

### Global install with pipx

```bash
pipx install git+https://github.com/AlfiePRIME/AlfiePRIME-Musiciser.git
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

---

## Screens

### Boombox (Main Screen)

<!-- ![Boombox Screen](docs/screenshots/boombox.png) -->

The default view. A retro boom box with real-time audio visualisation.

**Layout (top to bottom):**

| Section | Description |
|---------|-------------|
| **Title banner** | Track title with dynamic colour wave animation |
| **Now Playing** | Artist, album, codec info, progress bar with timestamps |
| **Braille art** | Album artwork rendered as Unicode braille dots (2×4 pixel grid per character) with Floyd-Steinberg dithering |
| **Transport controls** | Play/Pause, Previous, Next, Shuffle, Repeat — clickable with mouse |
| **VU meters** | Stereo level meters with peak-hold markers and shimmer |
| **Volume gauge** | Current volume percentage with coloured bar |
| **Spectrum analyzer** | 32-band FFT visualiser with AGC, peak hold, and BPM display. Expands to fill available terminal height |
| **Party lights** | Animated light strips synced to bass and treble energy |
| **Dance floor** | ASCII DJ with hype mode, 12 dancer types across depth rows, energy-reactive crowd density |
| **Status bar** | Server name, codec, sample rate, bit depth, system stats (CPU, RAM, network, uptime), listening stats |

**Keyboard Controls:**

| Key | Action |
|-----|--------|
| `P` | Play / Pause |
| `N` | Next track |
| `B` | Previous track |
| `S` | Toggle shuffle |
| `R` | Cycle repeat (off / all / one) |
| `M` | Toggle mute |
| `↑` / `↓` | Volume up / down |
| `A` | Toggle album art mode |
| `D` | Enter DJ mode |
| `T` | Switch active source (AirPlay ↔ SendSpin) |
| `/` | Open settings |
| `Q` | Quit |

> **Note:** When AirPlay is the active source, transport controls (play/pause, next, previous, shuffle, repeat) are hidden since AirPlay 2 has no reverse command channel for third-party receivers. Use the iPhone/iPad controls instead. Volume and mute work locally.

---

### Album Art Mode

<!-- ![Art Mode - Party](docs/screenshots/art_party.png) -->
<!-- ![Art Mode - Calm](docs/screenshots/art_calm.png) -->

Full-screen album art with two sub-modes.

**Party Mode** (`A` to enter):
- Large braille album art centred on screen
- Wandering animated dancers in the background
- Confetti and firework particle effects
- Beat-reactive colour pulses
- Binary background animation that shifts every few seconds

**Calm Mode** (`C` to toggle):
- Large braille album art on the left
- Info panel on the right showing:
  - Track title and artist
  - Album name and year
  - Track number
  - Codec, sample rate, and bit depth
- Info panel is sized to its content, not stretched to full height

| Key | Action |
|-----|--------|
| `A` | Toggle art mode on/off |
| `C` | Toggle calm/party sub-mode |
| `P` | Play / Pause |
| `N` / `B` | Next / Previous track |
| `↑` / `↓` | Volume up / down |
| `D` | Enter DJ mode |
| `Q` | Quit |

---

### DJ Mixing Console

<!-- ![DJ Screen](docs/screenshots/dj_mixer.png) -->

A two-channel software audio mixer for live crossfading between sources. Enter with `D` from any screen.

**Layout (top to bottom):**

| Section | Description |
|---------|-------------|
| **Title** | "♪ ALFIEPRIME DJ ♪" with beat-reactive glow |
| **Left deck (Channel A)** | Animated turntable with spinning vinyl, tonearm, track info, and connection status |
| **Center mixer** | Per-channel stereo VU meters (6 rows, green→accent→red gradient), crossfader slider with visual blend |
| **Right deck (Channel B)** | Same as left deck, mirrored |
| **EQ section** | 3-band EQ sliders per channel (Bass/Mid/Treble, ±12 dB) with volume bars |
| **Master output** | Full-width spectrum analyser showing the mixed output |
| **Status bar** | Per-channel codec info, sample rate, bit depth, connection indicators |
| **Key hints** | Dynamic hint bar that flashes on keypress |

**Audio Processing Chain:**

```
Input (16/24/32-bit) → Float32 decode → Mono→Stereo → Resample to 48kHz
    → 3-Band EQ (Bass 250Hz / Mid 1kHz / Treble 4kHz) → Per-channel volume
    → Equal-power crossfade → Master output → Audio device + Visualisers
```

**Smart Fade:** Press `F` to auto-crossfade to the opposite channel. Uses BPM-synced timing (8 beats, clamped 2–8 seconds) with an ease-in-out curve and bass ducking on the outgoing channel (-12 dB ramp). Press `F` again or move the crossfader manually to cancel.

**DJ Source Modes** (configurable in settings):
- **Mixed** (default) — Channel A: SendSpin, Channel B: AirPlay
- **Dual SendSpin** — Both channels receive from separate SendSpin instances
- **Dual AirPlay** — Both channels receive from separate AirPlay instances

**Keyboard Controls:**

| Key | Action |
|-----|--------|
| `P` | Play / Pause (both sources) |
| `Tab` | Switch channel focus (A ↔ B) |
| `←` / `→` | Crossfader left / right (±5%) |
| `↑` / `↓` | Focused channel volume ±5 |
| `1` / `Shift+1` | Bass EQ +2 / -2 dB |
| `2` / `Shift+2` | Mid EQ +2 / -2 dB |
| `3` / `Shift+3` | Treble EQ +2 / -2 dB |
| `0` | Reset EQ to flat (0 dB all bands) |
| `X` | Center crossfader (50/50) |
| `F` | Smart Fade (auto-crossfade to opposite channel) |
| `D` | Exit DJ mode |
| `Q` | Quit |

---

### Settings Menu

<!-- ![Settings Menu](docs/screenshots/settings.png) -->

In-app overlay opened with `/`. Animated CRT scanline background with fade-in transition.

**Sections:**

| Setting | Description |
|---------|-------------|
| **Auto Play** | Automatically send play command when a server connects |
| **Auto Volume** | Set volume to a target level on connect |
| **FPS** | Render frame rate (5–120 fps) |
| **Brightness** | Display brightness multiplier (50–150%) |
| **Show Artwork** | Toggle braille album art panel |
| **Art Colours** | Use album art colours for UI theming |
| **Static Colour** | Fixed UI colour (16 presets + custom hex, overrides art colours) |
| **Protocol** | Enable/disable AirPlay and SendSpin, device swap prompt, forget paired AirPlay devices |
| **Advanced** (`A` key) | Edit client name, UUID, DJ source mode, factory reset (with danger CRT warning) |

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate settings |
| `←` / `→` | Adjust value |
| `Enter` | Confirm / toggle |
| `/` or `C` | Close settings |
| `A` | Open advanced settings |

---

## Configuration

Settings are stored in `~/.config/alfieprime-musiciser/config.json` and are created on first run via the interactive setup wizard. Re-run the wizard at any time with:

```bash
alfieprime-musiciser --setup
```

## Architecture

The codebase is split into focused modules with a clean dependency graph:

```
colors.py          Color utilities, ColorTheme, album art extraction
state.py           PlayerState dataclass, toast notifications
config.py          Config, setup wizard, connection test
visualizer.py      AudioVisualizer (FFT, smoothed beat/BPM detection, delay queue)
renderer.py        All render_* functions (spectrum, VU, party scene, braille art, stats)
tui.py             BoomBoxTUI (layout, input handling, run loops) — uses mixins below
tui_settings.py    SettingsMixin (settings menu, colour picker, advanced, reset config)
tui_animations.py  AnimationsMixin (CRT startup/shutdown, standby screensaver, transitions)
tui_dj.py          DJMixin (DJ mixing console screen, turntable rendering, EQ display)
dj_mixer.py        DJMixer (software audio mixer, ring buffers, 3-band EQ, crossfader)
dj_state.py        DJState, ChannelState (per-channel volume/EQ/crossfader state)
receiver.py        SendSpinReceiver (WebSocket, audio, metadata, artwork, notifications)
airplay/           AirPlay 2 receiver package
  receiver.py      AirPlayReceiver (RTSP server, metadata hooks, PCM consumer, mDNS)
  vendor/          Vendored ap2-receiver with patches for our pipeline
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
| AirPlayReceiver                 |        |           v
|   - RTSP server (vendor ap2)    |   +------------------------+
|   - HAP transient pairing       |   | multiprocessing.Pipe   |
|   - Audio child processes       |   +------------------------+
|   - mDNS advertisement          |
|   - PCM queue → visualizer      |
|                                 |
| DJMixer (when DJ mode active)   |
|   - Ring buffers for both decks |
|   - 3-band biquad EQ per deck  |
|   - Equal-power crossfader     |
|   - PyAudio output stream      |
|   - Per-channel + master viz   |
|                                 |
| MPRIS2Server (Linux)            |
|   or SMTCServer (Windows)       |
|   - OS media key routing        |
|   - Track info to desktop       |
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
- **Braille art memoization** — `render_braille_art()` caches the decoded/rendered result keyed on image data hash + dimensions, skipping JPEG decode and PIL resize on unchanged artwork

### SendSpin Protocol

The app connects as a SendSpin client with four roles:

- **PLAYER** — receives and plays audio streams (PCM, FLAC)
- **METADATA** — receives track info, progress, playback state
- **CONTROLLER** — sends transport commands (play, pause, next, etc.)
- **ARTWORK** — receives album art (JPEG, 128×128) for dynamic theming

Album artwork arrives as binary WebSocket messages. Since the client library doesn't expose artwork listeners directly, the binary message handler is monkey-patched to intercept artwork channels.

### AirPlay 2 Protocol

The AirPlay receiver is built on a vendored fork of [ap2-receiver](https://github.com/openairplay/airplay2-receiver), patched to integrate with our audio and metadata pipeline:

- **RTSP server** on port 7000 handling SETUP, RECORD, SET_PARAMETER, SETPEERSEX, SETRATEANCHORTIME, TEARDOWN
- **Transient pairing** (feature bit 48) — devices connect without a PIN prompt
- **Audio delivery** via child processes: `AudioRealtime` (UDP, low-latency) and `AudioBuffered` (TCP, buffered)
- **PCM queue** bridges decoded audio from child processes to the parent's visualizer thread
- **Metadata** arrives through three channels: DMAP tagged data, binary plist SET_PARAMETER, and `/command` POST
- **Artwork** arrives as `image/*` SET_PARAMETER (enabled by feature bit 15: `AudioMetaCovers`)
- **Progress** via DMAP `progress` field and SETRATEANCHORTIME rate changes
- **Clean shutdown** with stream teardown, force-kill timeouts, HAP state cleanup, and mDNS deregistration

AirPlay pairing state is stored in `~/.cache/alfieprime/pairings/`. Client pairings are cleared on shutdown to ensure clean reconnection; the server keypair persists unless "Forget AirPlay Devices" is enabled in settings.

## Dependencies

| Package | Purpose |
|---------|---------|
| [sendspin](https://pypi.org/project/sendspin/) | SendSpin client, audio device handling, FLAC decoding |
| [numpy](https://numpy.org/) | FFT spectrum analysis, audio signal processing |
| [rich](https://rich.readthedocs.io/) | Terminal UI rendering with 24-bit colour |
| [Pillow](https://pillow.readthedocs.io/) | Album art colour extraction via median-cut quantization |
| [PyAudio](https://pypi.org/project/PyAudio/) | AirPlay audio output via PortAudio |
| [zeroconf](https://pypi.org/project/zeroconf/) | mDNS advertisement for AirPlay 2 discovery |
| [cryptography](https://cryptography.io/) | AirPlay HAP pairing (ed25519, x25519) |
| [PyCryptodome](https://pypi.org/project/pycryptodome/) | AirPlay ChaCha20-Poly1305 encryption |
| [biplist](https://pypi.org/project/biplist/) | Binary plist parsing for AirPlay 2 metadata |
| [hkdf](https://pypi.org/project/hkdf/) | HKDF key derivation for AirPlay pairing |
| [psutil](https://psutil.readthedocs.io/) | System stats (CPU, memory, network) — optional |
| [dbus-next](https://github.com/altdesktop/python-dbus-next) | MPRIS2 media controls — Linux only, auto-installed |
| [winsdk](https://github.com/pywinrt/python-winsdk) | SMTC media controls — Windows only, optional (requires Visual Studio Build Tools to compile) |

## License

MIT
