"""Auto-update checker for AlfiePRIME Musiciser.

Compares the installed version's git commit against the remote repository.
If updates are available, prompts the user to upgrade automatically.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

logger = logging.getLogger(__name__)

_REPO_URL = "https://github.com/AlfiePRIME/AlfiePRIME-Musiciser.git"
# Only check once per day at most
_CHECK_INTERVAL = 86400  # seconds


def _get_install_dir() -> Path | None:
    """Find the source directory if installed from a git clone."""
    # Walk up from this file to find a .git directory
    path = Path(__file__).resolve().parent
    for _ in range(5):
        if (path / ".git").is_dir():
            return path
        path = path.parent
    return None


def _last_check_file() -> Path:
    """Path to the timestamp file for throttling update checks."""
    config_dir = Path.home() / ".config" / "alfieprime-musiciser"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / ".last_update_check"


def _should_check() -> bool:
    """Return True if enough time has passed since the last check."""
    check_file = _last_check_file()
    if not check_file.exists():
        return True
    try:
        last = float(check_file.read_text().strip())
        return (time.time() - last) >= _CHECK_INTERVAL
    except (ValueError, OSError):
        return True


def _record_check() -> None:
    """Record that we just checked for updates."""
    try:
        _last_check_file().write_text(str(time.time()))
    except OSError:
        pass


def _run_git(args: list[str], cwd: Path, timeout: int = 10) -> str | None:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def check_for_updates(console: Console) -> None:
    """Check for updates and prompt user to upgrade if available."""
    if not _should_check():
        return

    install_dir = _get_install_dir()
    if install_dir is None:
        # Not installed from git clone (e.g. pip install from PyPI)
        # Try pipx-based check instead
        _check_pipx_updates(console)
        return

    # Fetch latest from remote (quick, non-blocking)
    console.print("[dim]Checking for updates...[/]", highlight=False)
    fetch_result = _run_git(["fetch", "--quiet"], cwd=install_dir, timeout=15)
    _record_check()

    if fetch_result is None:
        # Network error or no git — skip silently
        return

    # Compare local HEAD with remote
    local_hash = _run_git(["rev-parse", "HEAD"], cwd=install_dir)
    remote_hash = _run_git(["rev-parse", "@{upstream}"], cwd=install_dir)

    if not local_hash or not remote_hash:
        return

    if local_hash == remote_hash:
        return

    # Count commits behind
    behind = _run_git(
        ["rev-list", "--count", f"HEAD..@{{upstream}}"],
        cwd=install_dir,
    )
    behind_count = int(behind) if behind and behind.isdigit() else 0

    if behind_count == 0:
        return

    # Get summary of changes
    log_summary = _run_git(
        ["log", "--oneline", f"HEAD..@{{upstream}}", "--max-count=5"],
        cwd=install_dir,
    )

    console.print()
    console.print(f"[bold bright_cyan]Update available![/] ({behind_count} new commit{'s' if behind_count != 1 else ''})")
    if log_summary:
        for line in log_summary.split("\n"):
            console.print(f"  [dim]{line}[/]")
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

    _do_update(console, install_dir)


def _do_update(console: Console, install_dir: Path) -> None:
    """Pull latest changes and reinstall."""
    console.print()
    console.print("[bold]Updating...[/]")

    # Git pull
    console.print("[dim]  Pulling latest changes...[/]")
    pull_result = _run_git(["pull", "--ff-only"], cwd=install_dir, timeout=30)
    if pull_result is None:
        console.print("[bold red]  Git pull failed.[/] Try manually: git pull")
        console.print()
        return
    console.print(f"[green]  {pull_result}[/]")

    # Reinstall via pipx or pip
    console.print("[dim]  Reinstalling...[/]")
    reinstall_ok = False

    # Try pipx first
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pipx", "install", "--force", str(install_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            reinstall_ok = True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Fall back to pip
    if not reinstall_ok:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", str(install_dir)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                reinstall_ok = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    if reinstall_ok:
        console.print("[bold bright_green]Update complete![/]")
        console.print("[dim]Restarting...[/]")
        console.print()
        # Re-exec the process so we run the new code
        import os
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        console.print("[bold yellow]  Reinstall had issues. The source was updated —[/]")
        console.print("[bold yellow]  restart the app to use the new version.[/]")
        console.print()


def _check_pipx_updates(console: Console) -> None:
    """Check for updates via pipx when not installed from a git clone."""
    _record_check()
    # pipx doesn't have a clean "check if upgrade available" command,
    # so we skip this case — the user can run `pipx upgrade` manually
    pass
