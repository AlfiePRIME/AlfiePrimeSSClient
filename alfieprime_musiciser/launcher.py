"""GUI launcher for AlfiePRIME Musiciser.

On Windows this is invoked via pythonw.exe (gui_scripts entry point) so no
console window appears.  It runs the app directly in a standalone tkinter
window — no terminal emulator needed.
"""

from __future__ import annotations

import sys


def main() -> None:
    # Inject --gui so the app opens in its own tkinter window
    if "--gui" not in sys.argv:
        sys.argv.append("--gui")

    from alfieprime_musiciser.main import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
