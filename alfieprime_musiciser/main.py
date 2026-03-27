#!/usr/bin/env python3
"""AlfiePRIME Musiciser - A boom box themed SendSpin receiver with audio visualizer.

A party-mode client for Music Assistant (or any SendSpin server).
Advertises via mDNS so servers discover and connect to us automatically.
Displays a retro boom box TUI with real-time spectrum analyzer and party lights.

Usage:
    alfieprime-musiciser                          # Listen + advertise via mDNS (default)
    alfieprime-musiciser --name "MKUltra"         # Custom mDNS name
    alfieprime-musiciser --port 9000              # Custom listen port
    alfieprime-musiciser ws://host:port/sendspin  # Connect to specific server
    alfieprime-musiciser --demo                   # Demo mode (no server needed)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import uuid

from rich.console import Console
from rich.prompt import Prompt

from alfieprime_musiciser.config import Config, run_setup, _test_connection
from alfieprime_musiciser.receiver import SendSpinReceiver
from alfieprime_musiciser.tui import BoomBoxTUI
from alfieprime_musiciser.visualizer import AudioVisualizer

# Optional AirPlay support
try:
    from alfieprime_musiciser.airplay import _HAS_AIRPLAY, _MISSING_REASON
except ImportError:
    _HAS_AIRPLAY = False
    _MISSING_REASON = "airplay module not found"

IS_WINDOWS = sys.platform == "win32"

logger = logging.getLogger(__name__)


async def _run_with_config(
    config: Config, demo: bool = False, gui: bool = False, daemon: bool = False,
    airplay_name: str | None = None, airplay_port: int = 7000,
) -> None:
    """Run the TUI + receiver using the given config."""
    visualizer = AudioVisualizer()

    if daemon:
        # Headless daemon mode — no TUI/GUI, audio only
        server_url = config.server_url if config.mode == "connect" else None
        receiver = SendSpinReceiver(
            None, visualizer,  # type: ignore[arg-type]
            server_url=server_url,
            listen_port=config.listen_port,
            client_name=config.client_name,
            config=config,
        )
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        if IS_WINDOWS:
            signal.signal(signal.SIGINT, lambda *_: (receiver.stop(), stop_event.set()))
            signal.signal(signal.SIGTERM, lambda *_: (receiver.stop(), stop_event.set()))
        else:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: (receiver.stop(), stop_event.set()))

        logger.info("Running in daemon mode (audio only, no display)")
        await asyncio.gather(receiver.start(), stop_event.wait())
        return

    tui = BoomBoxTUI(visualizer, gui=gui, config=config)

    server_url = config.server_url if config.mode == "connect" else None

    receiver = SendSpinReceiver(
        tui, visualizer,
        server_url=server_url,
        listen_port=config.listen_port,
        client_name=config.client_name,
        config=config,
    )

    # Optional AirPlay receiver — auto-start if deps available and config enables it
    airplay_receiver = None
    if config.airplay_enabled and _HAS_AIRPLAY:
        from alfieprime_musiciser.airplay.receiver import AirPlayReceiver
        airplay_receiver = AirPlayReceiver(
            tui, visualizer,
            device_name=airplay_name or "",
            port=airplay_port,
            config=config,
        )
        logger.info("AirPlay receiver enabled on port %d", airplay_port)

    # Source switch callback — mute/unmute audio handlers when user switches
    def _on_source_switch(new_source: str) -> None:
        if receiver._audio_handler is not None:
            if new_source == "sendspin":
                receiver._audio_handler.set_volume(
                    tui.state.volume, muted=tui.state.muted,
                )
            else:
                receiver._audio_handler.set_volume(0, muted=True)
        # Sync visualizer pause state with the new source's play state
        visualizer.set_paused(not tui.state.is_playing)

    tui._source_switch_callback = _on_source_switch

    # DJ mode activation callback — mute native audio, connect mixer to receivers
    def _on_dj_activate(active: bool, mixer) -> None:
        if active and mixer is not None:
            # Mute native audio outputs — mixer does its own playback
            if receiver._audio_handler is not None:
                receiver._audio_handler.set_volume(0, muted=True)
            # Store mixer reference on receivers for PCM tapping
            receiver._dj_mixer = mixer
            if airplay_receiver is not None:
                airplay_receiver._dj_mixer = mixer
            logger.info("DJ mode: native audio muted, mixer connected")
        else:
            # Restore native audio
            receiver._dj_mixer = None
            if airplay_receiver is not None:
                airplay_receiver._dj_mixer = None
            # Unmute the active source's audio handler
            source = tui.state.active_source or "sendspin"
            if receiver._audio_handler is not None:
                if source == "sendspin":
                    receiver._audio_handler.set_volume(
                        tui.state.volume, muted=tui.state.muted,
                    )
                else:
                    receiver._audio_handler.set_volume(0, muted=True)
            logger.info("DJ mode: native audio restored")

    tui._dj_activate_callback = _on_dj_activate

    loop = asyncio.get_running_loop()

    def _stop_all():
        receiver.stop()
        if airplay_receiver:
            airplay_receiver.stop()
        tui.stop()

    if IS_WINDOWS:
        signal.signal(signal.SIGINT, lambda *_: _stop_all())
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _stop_all)

    tasks: list = [tui.run()]
    if demo:
        receiver._running = True
        tasks.append(receiver._run_demo_mode())
    else:
        if config.sendspin_enabled:
            tasks.append(receiver.start())
        else:
            logger.info("SendSpin receiver disabled by config")
    if airplay_receiver:
        tasks.append(airplay_receiver.start())
    await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AlfiePRIME Musiciser - boom box receiver for Music Assistant",
        epilog=(
            "On first run, an interactive setup wizard will guide you through\n"
            "configuration. Settings are saved to ~/.config/alfieprime-musiciser/config.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--setup", action="store_true", help="Re-run the setup wizard")
    parser.add_argument("--demo", action="store_true", help="Run in demo mode without a server")
    parser.add_argument("--gui", action="store_true", help="Run in a standalone GUI window instead of the terminal")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run as a headless service (no GUI/TUI, audio only)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--airplay-name", default=None, help="AirPlay device name (default: client name)")
    parser.add_argument("--airplay-port", type=int, default=7000, help="AirPlay RTSP port (default: 7000)")
    args = parser.parse_args()

    # When launched via pythonw.exe (gui_scripts), stdout/stderr are None.
    # Redirect them to devnull so logging / print don't crash.
    _headless = sys.stdout is None
    if _headless:
        _devnull = open(os.devnull, "w")  # noqa: SIM115
        sys.stdout = _devnull
        sys.stderr = _devnull

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)
        # Explicitly suppress chatty library loggers
        logging.getLogger("aiosendspin").setLevel(logging.WARNING)
        logging.getLogger("sendspin").setLevel(logging.WARNING)

    # GUI headless path: skip interactive console setup, load existing config
    # or use defaults, and go straight to the GUI.
    if args.gui and _headless:
        config = Config.load() or Config()
        if not config.client_id:
            config.client_id = str(uuid.uuid4())
            config.save()
        try:
            asyncio.run(_run_with_config(config, gui=True))
        except KeyboardInterrupt:
            pass
        return

    # Daemon mode — headless service, audio only
    if args.daemon:
        if args.verbose:
            logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s", force=True)
        else:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", force=True)
        config = Config.load()
        if config is None:
            print("No config found. Run once without --daemon to set up.", file=sys.stderr)
            sys.exit(1)
        if not config.client_id:
            config.client_id = str(uuid.uuid4())
            config.save()
        logger.info("AlfiePRIME Musiciser daemon starting")
        logger.info("Mode: %s | Client: %s", config.mode, config.client_name)
        try:
            asyncio.run(_run_with_config(config, daemon=True))
        except KeyboardInterrupt:
            pass
        logger.info("Daemon stopped")
        return

    console = Console()

    # Demo mode — skip config entirely
    if args.demo:
        try:
            asyncio.run(_run_with_config(Config(), demo=True, gui=args.gui))
        except KeyboardInterrupt:
            pass
        return

    # Check sendspin is installed
    try:
        from aiosendspin.client import SendspinClient  # noqa: F401
        from sendspin.audio_devices import query_devices  # noqa: F401
    except ImportError:
        console.print(
            "[bold red]Error:[/] sendspin package is not installed.\n"
            "Install it with: [bright_cyan]pip install 'sendspin>=0.12.0'[/]"
        )
        sys.exit(1)

    # Load or create config
    config = None if args.setup else Config.load()

    if config is None:
        # First run or --setup: run the wizard
        config = run_setup(console)

    # Connection test + retry loop
    while True:
        console.print(f"[dim]Mode:[/] [bright_cyan]{config.mode}[/]", highlight=False)
        if config.mode == "connect":
            console.print(f"[dim]Server:[/] [bright_cyan]{config.server_url}[/]", highlight=False)
        else:
            console.print(f"[dim]Listen port:[/] [bright_cyan]{config.listen_port}[/]", highlight=False)
        console.print(f"[dim]Client name:[/] [bright_cyan]{config.client_name}[/]", highlight=False)
        console.print()

        console.print("[dim]Testing connection...[/]")
        error = _test_connection(config, console)

        if error is None:
            console.print("[bright_green]OK![/] Starting party...\n")
            break
        else:
            console.print(f"\n[bold red]Connection failed:[/] {error}\n")
            choice = Prompt.ask(
                "What would you like to do?",
                choices=["retry", "setup", "quit"],
                default="setup",
                console=console,
            )
            if choice == "retry":
                continue
            elif choice == "setup":
                config = run_setup(console, existing=config)
                continue
            else:
                return

    # Run!
    try:
        asyncio.run(_run_with_config(
            config, gui=args.gui,
            airplay_name=args.airplay_name,
            airplay_port=args.airplay_port,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
