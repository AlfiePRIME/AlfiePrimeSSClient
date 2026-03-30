#!/usr/bin/env bash
# ============================================================================
#  AlfiePRIME Musiciser - One-Line Linux/macOS Installer
# ============================================================================
#
#  Usage (one-liner):
#    curl -fsSL https://raw.githubusercontent.com/AlfiePRIME/AlfiePRIME-Musiciser/main/install.sh | bash
#    wget -qO- https://raw.githubusercontent.com/AlfiePRIME/AlfiePRIME-Musiciser/main/install.sh | bash
#
#  What this does:
#    1. Checks for Python 3.12+, git, and pipx
#    2. Clones the repo (or pulls if already cloned)
#    3. Installs via pipx
#
# ============================================================================

set -euo pipefail

REPO_URL="https://github.com/AlfiePRIME/AlfiePRIME-Musiciser.git"
INSTALL_DIR="$HOME/.local/share/alfieprime-musiciser-src"

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${CYAN}${BOLD}  ============================================================${NC}"
echo -e "${CYAN}${BOLD}    A L F I E P R I M E   M U S I C I S E R   I N S T A L L${NC}"
echo -e "${CYAN}${BOLD}  ============================================================${NC}"
echo ""

# --- Check for Python 3.12+ ---
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PY_VER=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        if [ -n "$PY_VER" ]; then
            PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
            PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
            if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 12 ]; then
                PYTHON_CMD="$cmd"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}  [X] Python 3.12 or newer is required but was not found.${NC}"
    echo ""
    echo "      Install Python 3.12+:"
    echo "        Arch:   sudo pacman -S python"
    echo "        Ubuntu: sudo apt install python3.12"
    echo "        Fedora: sudo dnf install python3.12"
    echo "        macOS:  brew install python@3.12"
    echo ""
    exit 1
fi
echo -e "${GREEN}  [OK]${NC} Found Python $PY_VER ($PYTHON_CMD)"

# --- Check for git ---
if ! command -v git &>/dev/null; then
    echo -e "${RED}  [X] git is required but was not found.${NC}"
    echo ""
    echo "      Install git:"
    echo "        Arch:   sudo pacman -S git"
    echo "        Ubuntu: sudo apt install git"
    echo "        Fedora: sudo dnf install git"
    echo "        macOS:  xcode-select --install"
    echo ""
    exit 1
fi
echo -e "${GREEN}  [OK]${NC} Found git"

# --- Check for pipx ---
if ! command -v pipx &>/dev/null; then
    echo -e "${YELLOW}  [!]${NC} pipx not found, installing..."
    "$PYTHON_CMD" -m pip install --user pipx 2>/dev/null || {
        echo -e "${RED}  [X] Failed to install pipx.${NC}"
        echo "      Try: $PYTHON_CMD -m pip install --user pipx"
        exit 1
    }
    "$PYTHON_CMD" -m pipx ensurepath 2>/dev/null || true
    # Re-check after install
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v pipx &>/dev/null; then
        echo -e "${YELLOW}  [!]${NC} pipx installed but not on PATH yet."
        echo "      Restart your terminal and re-run this script."
        exit 1
    fi
fi
echo -e "${GREEN}  [OK]${NC} Found pipx"
echo ""

# --- Clone or update repo ---
echo -e "  ${BOLD}[1/2]${NC} Getting source code..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "        Updating existing clone..."
    git -C "$INSTALL_DIR" pull --quiet 2>/dev/null || true
else
    echo "        Cloning repository..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
fi
echo -e "${GREEN}  [OK]${NC} Source ready"
echo ""

# --- Install via pipx ---
echo -e "  ${BOLD}[2/2]${NC} Installing AlfiePRIME Musiciser (this may take a minute)..."
pipx install --force "$INSTALL_DIR" 2>&1 || {
    echo ""
    echo -e "${RED}  [X] Installation failed. Check the errors above.${NC}"
    echo "      Common fixes:"
    echo "        - Install build deps: sudo apt install python3-dev libasound2-dev"
    echo "        - Try: pip install --user '$INSTALL_DIR'"
    exit 1
}
echo ""
echo -e "${GREEN}  [OK]${NC} Installation complete!"

# --- Optional: install Spotify deps ---
if command -v librespot &>/dev/null; then
    echo ""
    echo -e "  ${BOLD}[+]${NC} librespot detected — Spotify Connect will be available"
fi

echo ""
echo -e "${CYAN}${BOLD}  ============================================================${NC}"
echo -e "${CYAN}${BOLD}    INSTALLATION COMPLETE!${NC}"
echo -e "${CYAN}${BOLD}  ============================================================${NC}"
echo ""
echo "    Run with:     alfieprime-musiciser"
echo "    Demo mode:    alfieprime-musiciser --demo"
echo "    Update:       pipx upgrade alfieprime-musiciser"
echo "    Uninstall:    pipx uninstall alfieprime-musiciser"
echo ""
