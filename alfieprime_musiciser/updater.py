"""Auto-update checker for AlfiePRIME Musiciser.

Compares the installed version against the latest version published on
GitHub.  Works regardless of install method (git clone, pipx, pip).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

logger = logging.getLogger(__name__)

_REPO_URL = "https://github.com/AlfiePRIME/AlfiePRIME-Musiciser.git"
_VERSION_URL = (
    "https://api.github.com/repos/AlfiePRIME/AlfiePRIME-Musiciser"
    "/contents/alfieprime_musiciser/__init__.py?ref=main"
)


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '1.2.3' into a comparable tuple."""
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _get_local_version() -> str:
    """Return the currently installed version.

    Uses the source-tree ``__version__`` which is the version the app
    is actually running as.
    """
    try:
        from alfieprime_musiciser import __version__
        return __version__
    except (ImportError, AttributeError):
        return "0.0.0"


def _fetch_remote_version() -> str | None:
    """Fetch the latest __version__ from GitHub (quick HTTP GET)."""
    try:
        from urllib.request import urlopen, Request
        req = Request(_VERSION_URL, headers={
            "User-Agent": "AlfiePRIME-Musiciser",
            "Accept": "application/vnd.github.v3.raw",
        })
        with urlopen(req, timeout=8) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _find_git_dir() -> Path | None:
    """Find the git repo root if installed from a git clone."""
    path = Path(__file__).resolve().parent
    for _ in range(5):
        if (path / ".git").is_dir():
            return path
        path = path.parent
    return None


def check_for_updates(console: Console) -> None:
    """Check for updates and prompt user to upgrade if available."""
    local_version = _get_local_version()
    console.print(f"[dim]Checking for updates (current: v{local_version})...[/]", highlight=False)

    remote_version = _fetch_remote_version()

    if remote_version is None:
        console.print("[dim]Could not reach GitHub, skipping update check.[/]")
        return

    local_tuple = _parse_version(local_version)
    remote_tuple = _parse_version(remote_version)

    console.print(f"[dim]Latest: v{remote_version}[/]", highlight=False)

    if remote_tuple <= local_tuple:
        console.print("[dim]Already up to date.[/]")
        return

    console.print()
    console.print(
        f"[bold bright_cyan]Update available![/]  "
        f"[dim]{local_version}[/] → [bold bright_green]{remote_version}[/]"
    )
    console.print()

    try:
        update = Confirm.ask(
            "[bright_cyan]Update now?[/]",
            default=True,
            console=console,
        )
    except (EOFError, KeyboardInterrupt):
        return

    if not update:
        return

    _do_update(console, local_version, remote_version)


def _do_update(console: Console, old_ver: str, new_ver: str) -> None:
    """Pull latest changes and reinstall."""
    console.print()
    console.print(f"[bold]Updating {old_ver} → {new_ver}...[/]")

    git_dir = _find_git_dir()

    # Strategy 1: git pull if we're in a clone
    if git_dir is not None:
        console.print("[dim]  Pulling latest changes...[/]")
        try:
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=str(git_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                console.print(f"[green]  {result.stdout.strip()}[/]")
            else:
                console.print("[yellow]  Git pull failed, trying reinstall from remote...[/]")
                git_dir = None  # fall through to remote install
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            git_dir = None

    # Reinstall
    console.print("[dim]  Reinstalling...[/]")
    install_source = str(git_dir) if git_dir else f"git+{_REPO_URL}"
    reinstall_ok = False

    # Try pipx first
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pipx", "install", "--force", install_source],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            reinstall_ok = True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Fall back to pip
    if not reinstall_ok:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", install_source],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode == 0:
                reinstall_ok = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    if reinstall_ok:
        console.print(f"[bold bright_green]Updated to {new_ver}![/]")
        console.print("[dim]Restarting...[/]")
        console.print()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        console.print("[bold yellow]  Update failed. Try manually:[/]")
        console.print(f"[dim]    pipx install --force git+{_REPO_URL}[/]")
        console.print()
