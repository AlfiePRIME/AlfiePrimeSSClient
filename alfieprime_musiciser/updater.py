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
import time
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

logger = logging.getLogger(__name__)

_REPO_URL = "https://github.com/AlfiePRIME/AlfiePRIME-Musiciser.git"
_VERSION_URL = (
    "https://raw.githubusercontent.com/AlfiePRIME/AlfiePRIME-Musiciser"
    "/main/alfieprime_musiciser/__init__.py"
)
# Only check once per day at most
_CHECK_INTERVAL = 86400  # seconds


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '1.2.3' into a comparable tuple."""
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _get_local_version() -> str:
    """Return the currently installed version."""
    try:
        from alfieprime_musiciser import __version__
        return __version__
    except (ImportError, AttributeError):
        return "0.0.0"


def _fetch_remote_version() -> str | None:
    """Fetch the latest __version__ from GitHub (quick HTTP GET)."""
    try:
        from urllib.request import urlopen, Request
        req = Request(_VERSION_URL, headers={"User-Agent": "AlfiePRIME-Musiciser"})
        with urlopen(req, timeout=8) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _last_check_file() -> Path:
    """Path to the timestamp file for throttling update checks."""
    config_dir = Path.home() / ".config" / "alfieprime-musiciser"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / ".last_update_check"


def _should_check() -> bool:
    """Return True if enough time has passed since the last check.

    Also re-checks if the local version changed (e.g. after an update),
    since there may be another newer version available.
    """
    check_file = _last_check_file()
    if not check_file.exists():
        return True
    try:
        content = check_file.read_text().strip()
        parts = content.split("|", 1)
        last = float(parts[0])
        checked_version = parts[1] if len(parts) > 1 else ""
        # Re-check if local version changed since last check
        if checked_version != _get_local_version():
            return True
        return (time.time() - last) >= _CHECK_INTERVAL
    except (ValueError, OSError):
        return True


def _record_check() -> None:
    """Record that we just checked for updates (with current version)."""
    try:
        _last_check_file().write_text(f"{time.time()}|{_get_local_version()}")
    except OSError:
        pass


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
    if not _should_check():
        return

    local_version = _get_local_version()
    console.print("[dim]Checking for updates...[/]", highlight=False)

    remote_version = _fetch_remote_version()
    _record_check()

    if remote_version is None:
        # Network error — skip silently
        return

    local_tuple = _parse_version(local_version)
    remote_tuple = _parse_version(remote_version)

    if remote_tuple <= local_tuple:
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
