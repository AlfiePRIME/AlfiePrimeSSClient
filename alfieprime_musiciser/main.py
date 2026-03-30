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
import time
import os
import signal
import sys
import uuid

from rich.console import Console
from rich.prompt import Prompt

from alfieprime_musiciser.config import Config, run_setup, _test_connection
from alfieprime_musiciser.setup_wizard import run_setup_wizard, play_intro_animation
from alfieprime_musiciser.receiver import SendSpinReceiver
from alfieprime_musiciser.tui import BoomBoxTUI
from alfieprime_musiciser.visualizer import AudioVisualizer

# Optional AirPlay support
try:
    from alfieprime_musiciser.airplay import _HAS_AIRPLAY, _MISSING_REASON
except ImportError:
    _HAS_AIRPLAY = False
    _MISSING_REASON = "airplay module not found"

# Optional Spotify Connect support
try:
    from alfieprime_musiciser.spotify import _HAS_SPOTIFY, _MISSING_REASON as _SPOTIFY_MISSING_REASON
except ImportError:
    _HAS_SPOTIFY = False
    _SPOTIFY_MISSING_REASON = "spotify module not found"

IS_WINDOWS = sys.platform == "win32"

logger = logging.getLogger(__name__)


async def _run_with_config(
    config: Config, demo: bool = False, gui: bool = False, daemon: bool = False,
    airplay_name: str | None = None, airplay_port: int = 7000,
    spotify_name: str | None = None,
) -> None:
    """Run the TUI + receiver using the given config."""
    visualizer = AudioVisualizer()
    _effective_name = config.client_name or __import__("socket").gethostname()

    if daemon:
        # Headless daemon mode — no TUI/GUI, audio only
        server_url = config.server_url if config.mode == "connect" else None
        receiver = SendSpinReceiver(
            None, visualizer,  # type: ignore[arg-type]
            server_url=server_url,
            listen_port=config.listen_port,
            client_name=_effective_name,
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
        client_name=_effective_name,
        config=config,
    )

    # Optional AirPlay receiver — auto-start if deps available and config enables it
    _hostname = __import__("socket").gethostname()
    airplay_receiver = None
    dj_mode = config.dj_source_mode
    if config.airplay_enabled and _HAS_AIRPLAY:
        from alfieprime_musiciser.airplay.receiver import AirPlayReceiver
        # In dual AirPlay mode, name the primary as "MusiciserSource1@Hostname"
        if dj_mode == "dual_airplay":
            _ap_name = airplay_name or f"MusiciserSource1@{_hostname}"
        else:
            _ap_name = airplay_name or ""
        airplay_receiver = AirPlayReceiver(
            tui, visualizer,
            device_name=_ap_name,
            port=airplay_port,
            config=config,
        )
        logger.info("AirPlay receiver enabled on port %d", airplay_port)

    # Optional Spotify Connect receiver (not available on Windows)
    spotify_receiver = None
    if config.spotify_enabled and _HAS_SPOTIFY and not IS_WINDOWS:
        from alfieprime_musiciser.spotify.receiver import SpotifyConnectReceiver
        _sp_name = spotify_name or config.spotify_device_name or ""
        spotify_receiver = SpotifyConnectReceiver(
            tui, visualizer,
            device_name=_sp_name,
            config=config,
        )
        logger.info("Spotify Connect receiver enabled as '%s'", _sp_name)

    # 2-source connection limit
    _connected_sources: set[str] = set()

    def _can_connect(source: str) -> bool:
        if source in _connected_sources:
            return True
        return len(_connected_sources) < 2

    def _on_source_connected(source: str) -> None:
        _connected_sources.add(source)

    def _on_source_disconnected(source: str) -> None:
        _connected_sources.discard(source)

    # In dual SendSpin mode, rename primary receiver to "Hostname Source 1"
    if dj_mode == "dual_sendspin":
        receiver._client_name = f"{_hostname} Source 1"

    # Second receiver for dual DJ source modes
    receiver_b = None
    # Generate/load a stable UUID for the second receiver
    if not config.client_id_b:
        import uuid as _uuid_mod
        config.client_id_b = f"alfieprime-musiciser-{_uuid_mod.uuid4().hex[:8]}"
        config.save()
    if dj_mode == "dual_sendspin" and config.sendspin_enabled:
        viz_b = AudioVisualizer()
        receiver_b = SendSpinReceiver(
            None, viz_b,
            server_url=None,
            listen_port=config.listen_port + 1,
            client_name=f"{_hostname} Source 2",
            config=config,
        )
        # Use the dedicated second UUID so queues are preserved separately
        receiver_b._client_id = config.client_id_b
        receiver_b._dj_feed_channel = "b"
        logger.info("Dual SendSpin DJ: second receiver on port %d", config.listen_port + 1)
    elif dj_mode == "dual_airplay" and _HAS_AIRPLAY:
        from alfieprime_musiciser.airplay.receiver import AirPlayReceiver as _AP2
        viz_b = AudioVisualizer()
        ap_name_b = airplay_name or f"MusiciserSource2@{_hostname}"
        if airplay_name:
            ap_name_b = f"{airplay_name} Source 2"
        receiver_b = _AP2(
            None, viz_b,
            device_name=ap_name_b,
            port=airplay_port + 1,
            config=config,
        )
        receiver_b._dj_feed_channel = "b"
        logger.info("Dual AirPlay DJ: second receiver '%s' on port %d", ap_name_b, airplay_port + 1)

    # Store second receiver's state on TUI so DJ screen can read track info
    tui._dj_receiver_b = receiver_b

    # Source switch callback — mute/unmute audio handlers when user switches
    def _on_source_switch(new_source: str) -> None:
        logger.warning("Source switch → %s, handler=%s", new_source, receiver._audio_handler is not None)
        if receiver._audio_handler is not None:
            if new_source == "sendspin":
                ss_vol, ss_muted = tui.state.get_source_volume("sendspin")
                logger.warning("Source switch: restoring SS vol=%d muted=%s", ss_vol, ss_muted)
                receiver._audio_handler.set_volume(ss_vol, muted=ss_muted)
            else:
                receiver._audio_handler.set_volume(0, muted=True)
        # Mute/unmute AirPlay sink based on active source
        if airplay_receiver is not None:
            airplay_receiver.set_sink_muted(new_source != "airplay")
        # Spotify has no native sink to mute (pipe backend)
        # Sync visualizer pause state with the new source's play state
        visualizer.set_paused(not tui.state.is_playing)

    tui._source_switch_callback = _on_source_switch

    # DJ mode activation callback — mute native audio, connect mixer to receivers
    def _on_dj_activate(active: bool, mixer) -> None:
        if active and mixer is not None:
            # Save sendspin volume before muting so we can restore it on DJ exit
            ss_vol, ss_muted = tui.state.get_source_volume("sendspin")
            tui.state._source_volumes["sendspin"] = {"volume": ss_vol, "muted": ss_muted}
            logger.warning("DJ enter: saved SS vol=%d muted=%s, handler=%s",
                           ss_vol, ss_muted, receiver._audio_handler is not None)
            # Flush native handler's audio queue so it doesn't accumulate
            # stale buffered audio while the DJ mixer owns playback.
            if receiver._audio_handler is not None:
                receiver._audio_handler.clear_queue()
                receiver._audio_handler.set_volume(0, muted=True)
            # Wire mixer to the correct receivers based on DJ source mode
            _dj = config.dj_source_mode
            if _dj == "dual_sendspin":
                # A = primary SendSpin, B = second SendSpin
                receiver._dj_mixer = mixer
                if receiver_b is not None:
                    receiver_b._dj_mixer = mixer
            elif _dj == "dual_airplay":
                # A = primary AirPlay, B = second AirPlay
                if airplay_receiver is not None:
                    airplay_receiver._dj_feed_channel = "a"
                    airplay_receiver._dj_mixer = mixer
                if receiver_b is not None:
                    receiver_b._dj_mixer = mixer
            elif _dj == "spotify_sendspin":
                # A = SendSpin, B = Spotify
                receiver._dj_mixer = mixer
                if spotify_receiver is not None:
                    spotify_receiver._dj_feed_channel = "b"
                    spotify_receiver._dj_mixer = mixer
            elif _dj == "spotify_airplay":
                # A = AirPlay, B = Spotify
                if airplay_receiver is not None:
                    airplay_receiver._dj_feed_channel = "a"
                    airplay_receiver._dj_mixer = mixer
                if spotify_receiver is not None:
                    spotify_receiver._dj_feed_channel = "b"
                    spotify_receiver._dj_mixer = mixer
            elif _dj == "dual_spotify":
                # A = Spotify (only one instance, feeds channel a)
                if spotify_receiver is not None:
                    spotify_receiver._dj_feed_channel = "a"
                    spotify_receiver._dj_mixer = mixer
            else:
                # Mixed: A = SendSpin, B = AirPlay (default)
                receiver._dj_mixer = mixer
                if airplay_receiver is not None:
                    airplay_receiver._dj_mixer = mixer
            logger.info("DJ mode: native audio muted, mixer connected (%s)", _dj)
        else:
            # Restore native audio — clear mixer on all receivers
            receiver._dj_mixer = None
            if airplay_receiver is not None:
                airplay_receiver._dj_feed_channel = "b"  # restore default
                airplay_receiver._dj_mixer = None
                # Only unmute AirPlay sink if AirPlay is the active source;
                # otherwise keep it muted so it doesn't bleed through.
                if tui.state.active_source == "airplay":
                    airplay_receiver.set_sink_muted(False)
                else:
                    airplay_receiver.set_sink_muted(True)
            if spotify_receiver is not None:
                spotify_receiver._dj_feed_channel = "a"  # restore default
                spotify_receiver._dj_mixer = None
            if receiver_b is not None:
                receiver_b._dj_mixer = None
            # Snap progress to current interpolated value so the boombox
            # screen doesn't jump when it picks up rendering.
            tui.state.progress_ms = tui.state.get_interpolated_progress()
            tui.state.progress_update_time = time.monotonic()
            # Flush native handler's buffered audio so it starts fresh from the
            # current incoming stream position — prevents audible skip on exit.
            if receiver._audio_handler is not None:
                receiver._audio_handler.clear_queue()
            logger.warning("DJ exit: handler=%s, source_vols=%s, active_source=%s",
                           receiver._audio_handler is not None,
                           tui.state._source_volumes, tui.state.active_source)
            if receiver._audio_handler is not None:
                if tui.state.active_source == "sendspin":
                    sv = tui.state._source_volumes.get("sendspin")
                    if sv is not None:
                        ss_vol = sv["volume"]
                        ss_muted = sv["muted"]
                    else:
                        ss_vol = tui.state.volume if tui.state.volume > 0 else 100
                        ss_muted = False
                    logger.warning("DJ exit: restoring SendSpin audio vol=%d muted=%s", ss_vol, ss_muted)
                    receiver._audio_handler.set_volume(ss_vol, muted=ss_muted)
                    tui.state.volume = ss_vol
                    tui.state.muted = ss_muted
                else:
                    # AirPlay is active — keep SendSpin handler muted
                    receiver._audio_handler.set_volume(0, muted=True)
            else:
                logger.warning("DJ exit: NO audio handler — cannot restore volume!")
            # Ensure the master visualizer is unpaused for the boombox screen
            tui._visualizer.set_paused(False)
            logger.info("DJ mode: native audio restored")

    tui._dj_activate_callback = _on_dj_activate
    tui._sendspin_command_callback = receiver._on_transport_command
    if airplay_receiver is not None:
        tui._airplay_dj_play_pause = airplay_receiver.dj_play_pause
    if spotify_receiver is not None:
        tui._spotify_dj_play_pause = spotify_receiver.dj_play_pause
        tui._spotify_command_callback = spotify_receiver._on_transport_command

    loop = asyncio.get_running_loop()

    def _cleanup_receivers():
        receiver.stop()
        if airplay_receiver:
            airplay_receiver.stop()
        if spotify_receiver:
            spotify_receiver.stop()
        if receiver_b is not None:
            receiver_b.stop()
        tui._cleanup_done.set()

    tui._cleanup_fn = _cleanup_receivers

    def _stop_all():
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
    if spotify_receiver:
        tasks.append(spotify_receiver.start())
    if receiver_b is not None:
        tasks.append(receiver_b.start())
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
    parser.add_argument("--spotify-name", default=None, help="Spotify Connect device name (default: client name)")
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
    config = Config.load()

    ran_setup = False
    if config is None or args.setup or (config is not None and config.run_setup):
        # First run, --setup flag, or re-run requested from settings
        config = run_setup_wizard(console, existing=config)
        ran_setup = True
        # Clear the re-run flag so it doesn't loop
        if config.run_setup:
            config.run_setup = False
            config.save()

    if not ran_setup:
        # Connection test + retry loop (skip after setup — go straight to launch)
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
                    config = run_setup_wizard(console, existing=config)
                    break  # after manual setup retry, launch directly
                else:
                    return

    # Play intro animation (setup has its own, so only play on normal start)
    if not ran_setup:
        play_intro_animation()

    # Run!
    try:
        asyncio.run(_run_with_config(
            config, gui=args.gui,
            airplay_name=args.airplay_name,
            airplay_port=args.airplay_port,
            spotify_name=args.spotify_name,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
