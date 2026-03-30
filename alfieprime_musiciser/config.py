from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

# ─── Config ──────────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".config" / "alfieprime-musiciser"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    """Persistent configuration."""

    client_name: str = ""  # empty = use hostname
    mode: str = "listen"  # "listen" (mDNS) or "connect" (explicit URL)
    server_url: str = ""  # only used when mode == "connect"
    listen_port: int = 8928  # only used when mode == "listen"
    client_id: str = ""  # stable ID so Music Assistant remembers this device
    client_id_b: str = ""  # stable ID for second receiver (dual DJ modes)
    # UI state (persisted across restarts)
    art_mode: bool = False
    art_calm: bool = False
    # Settings
    auto_play: bool = False
    auto_volume: int = -1  # -1 = disabled, 0-100 = set volume on connect
    fps_limit: int = 30  # 5-120
    show_artwork: bool = True  # show braille art in normal mode
    use_art_colors: bool = True  # dynamic album art colours
    static_color: str = ""  # hex color override when art colours disabled
    brightness: int = 110  # terminal brightness percentage (50-150)
    # Cached theme from last session (restored on startup for intro animation)
    cached_theme: dict = field(default_factory=dict)
    # Protocol settings
    airplay_enabled: bool = True
    sendspin_enabled: bool = True
    swap_prompt: bool = True  # show Y/N when a second device connects
    swap_auto_action: str = "deny"  # "accept" or "deny" when prompt is off
    # Devices the user has previously accepted via the swap prompt
    accepted_devices: list[str] = field(default_factory=list)
    # Clear AirPlay pairing data on close so devices must re-pair
    forget_airplay_devices: bool = False
    # Spotify Connect settings
    spotify_enabled: bool = True
    spotify_client_id: str = ""  # Spotify Web API client ID (for PKCE OAuth)
    spotify_device_name: str = ""  # custom librespot device name
    spotify_bitrate: int = 320  # 160 or 320
    spotify_username: str = ""  # for librespot auth
    # DJ source mode: "mixed" (SS+AP), "dual_sendspin", "dual_airplay",
    # "spotify_sendspin" (SS+SP), "spotify_airplay" (AP+SP), "dual_spotify"
    dj_source_mode: str = "mixed"

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls) -> Config | None:
        if not CONFIG_FILE.exists():
            return None
        try:
            data = json.loads(CONFIG_FILE.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError, OSError):
            return None


def run_setup(console: Console, existing: Config | None = None) -> Config:
    """Interactive first-run setup (or reconfigure)."""
    console.print()
    console.print(Panel(
        "[bold bright_magenta]A L F I E P R I M E   M U S I C I Z E R   S E T U P[/]",
        border_style="bright_cyan",
    ))
    console.print()

    defaults = existing or Config()

    # Client name
    import socket as _socket
    _default_name = defaults.client_name or _socket.gethostname()
    client_name = Prompt.ask(
        "[bright_cyan]Client name[/] (how this player appears in Music Assistant)",
        default=_default_name,
        console=console,
    )

    # Connection mode
    console.print()
    console.print("[bold]Connection mode:[/]")
    console.print("  [bright_green]1[/] - Listen (mDNS) — server discovers and connects to us [dim](recommended)[/dim]")
    console.print("  [bright_green]2[/] - Connect — we connect to a specific server URL")
    console.print()

    default_mode_num = "1" if defaults.mode == "listen" else "2"
    mode_choice = Prompt.ask(
        "[bright_cyan]Choose mode[/]",
        choices=["1", "2"],
        default=default_mode_num,
        console=console,
    )

    mode = "listen" if mode_choice == "1" else "connect"
    server_url = ""
    listen_port = defaults.listen_port

    if mode == "connect":
        console.print()
        console.print("[dim]Enter the SendSpin/Music Assistant WebSocket URL.[/]")
        console.print("[dim]Examples: ws://192.168.1.100:8097/sendspin  or  ws://homeassistant.local:8097/sendspin[/]")
        console.print()
        server_url = Prompt.ask(
            "[bright_cyan]Server URL[/]",
            default=defaults.server_url or "",
            console=console,
        )
        # Normalise: add ws:// if missing
        if server_url and not server_url.startswith(("ws://", "wss://")):
            if ":" in server_url and "/" in server_url:
                server_url = "ws://" + server_url
            else:
                # Bare IP/hostname — add default sendspin port+path
                server_url = f"ws://{server_url}:8097/sendspin"
            console.print(f"[dim]Using URL: {server_url}[/]")
    else:
        console.print()
        listen_port = int(Prompt.ask(
            "[bright_cyan]Listen port[/]",
            default=str(defaults.listen_port),
            console=console,
        ))

    config = Config(
        client_name=client_name,
        mode=mode,
        server_url=server_url,
        listen_port=listen_port,
    )
    config.save()

    console.print()
    console.print(f"[bright_green]Config saved to {CONFIG_FILE}[/]")
    console.print()
    return config


def _test_connection(config: Config, console: Console) -> str | None:
    """Try a quick connection to validate the config. Returns error string or None on success."""
    if config.mode == "listen":
        # For listen mode, just check the port is bindable
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", config.listen_port))
            return None
        except OSError as e:
            return f"Cannot bind to port {config.listen_port}: {e}"

    # For connect mode, try a quick WebSocket handshake
    if not config.server_url:
        return "No server URL configured"

    import asyncio

    async def _try_connect() -> str | None:
        try:
            from aiohttp import ClientSession, ClientError, WSMsgType
            timeout_s = 5
            async with ClientSession() as session:
                async with session.ws_connect(config.server_url, timeout=timeout_s) as ws:
                    await ws.close()
            return None
        except (TimeoutError, OSError, ClientError) as e:
            return f"{type(e).__name__}: {e}"
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    return asyncio.run(_try_connect())
