#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Hacker File Navigator — Installer
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/rhyths-simp/hacker-navir/main/install.sh -o install.sh && bash install.sh
# ─────────────────────────────────────────────────────────────

# NOTE: no set -e here — we handle errors ourselves so the script
# never crashes silently mid-install

REPO="https://github.com/rhyths-simp/hacker-navir.git"
INSTALL_DIR="$HOME/.navigator/app"
VERSION_FILE="$INSTALL_DIR/version.txt"

echo ""
echo "  ◈ Hacker File Navigator — Installer"
echo "  ─────────────────────────────────────"

# ── 1. Check Python ───────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo ""
    echo "  ERROR: python3 not found."
    echo "  On Termux run:   pkg install python"
    echo "  On Ubuntu run:   sudo apt install python3"
    exit 1
fi

PYVER=$(python3 -c 'import sys; print(sys.version_info[:2] >= (3,9))')
if [ "$PYVER" != "True" ]; then
    echo "  ERROR: Python 3.9 or newer is required."
    exit 1
fi

echo "  ✓ Python OK"

# ── 2. Check git ──────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    echo ""
    echo "  ERROR: git not found."
    echo "  On Termux run:   pkg install git"
    echo "  On Ubuntu run:   sudo apt install git"
    exit 1
fi

echo "  ✓ git OK"

# ── 3. Clone or update ────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  ↻ Updating existing install..."
    git -C "$INSTALL_DIR" pull --quiet
else
    echo "  ↓ Cloning repository..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    if ! git clone --quiet "$REPO" "$INSTALL_DIR"; then
        echo "  ERROR: Failed to clone repository."
        echo "  Check your internet connection and try again."
        exit 1
    fi
fi

echo "  ✓ Files ready at $INSTALL_DIR"

# ── 4. Write version file ─────────────────────────────────────
# Always refresh version.txt from latest git tag
VERSION=$(git -C "$INSTALL_DIR" describe --tags --abbrev=0 2>/dev/null || echo "v1.0.0")
echo "$VERSION" > "$VERSION_FILE"
echo "  ✓ Version: $VERSION"

# ── 5. Detect bin dir (Termux vs Linux) ───────────────────────
if [ -d "/data/data/com.termux/files/usr/bin" ]; then
    BIN_DIR="/data/data/com.termux/files/usr/bin"
elif [ -d "$HOME/.local/bin" ]; then
    BIN_DIR="$HOME/.local/bin"
else
    BIN_DIR="/usr/local/bin"
fi

# ── 6. Create navir launcher ──────────────────────────────────
# IMPORTANT: INSTALL_DIR and VERSION_FILE are resolved at runtime
# inside the launcher (using $HOME), not hardcoded at install time.
# This means the launcher keeps working even if the home path changes.

cat > "$BIN_DIR/navir" << 'LAUNCHEREOF'
#!/bin/bash
# ── Hacker File Navigator launcher ───────────────────────────
INSTALL_DIR="$HOME/.navigator/app"
NAV_PY="$INSTALL_DIR/navigator.py"
VERSION_FILE="$INSTALL_DIR/version.txt"

_write_version() {
    git -C "$INSTALL_DIR" describe --tags --abbrev=0 2>/dev/null \
        > "$VERSION_FILE" || echo "v1.0.0" > "$VERSION_FILE"
}

case "$1" in

  --version|-v)
    if [ -f "$VERSION_FILE" ]; then
        echo "navir $(cat $VERSION_FILE)"
    else
        echo "navir (version unknown)"
    fi
    ;;

  --update|-u)
    echo ""
    echo "  ◈ Hacker File Navigator — Updater"
    echo "  ────────────────────────────────────"
    if [ ! -d "$INSTALL_DIR/.git" ]; then
        echo "  ERROR: Install directory not found."
        echo "  Run the installer again to reinstall:"
        echo "  curl -fsSL https://raw.githubusercontent.com/rhyths-simp/hacker-navir/main/install.sh -o install.sh && bash install.sh"
        exit 1
    fi
    echo "  ↻ Checking for updates..."
    BEFORE=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null)
    git -C "$INSTALL_DIR" pull --quiet
    AFTER=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null)
    # always refresh version.txt after pull
    _write_version
    if [ "$BEFORE" = "$AFTER" ]; then
        echo "  ✓ Already up to date. ($(cat $VERSION_FILE))"
    else
        echo "  ✓ Updated successfully!"
        echo "  ✓ Now on version: $(cat $VERSION_FILE)"
    fi
    echo ""
    ;;

  --help|-h)
    echo ""
    echo "  navir              Launch the file navigator"
    echo "  navir --update     Update to the latest version"
    echo "  navir --version    Show current version"
    echo "  navir --help       Show this help"
    echo ""
    echo "  To add a plugin:"
    echo "    Drop any .py file into ~/.navigator/plugins/"
    echo ""
    ;;

  *)
    if [ ! -f "$NAV_PY" ]; then
        echo "  ERROR: navigator.py not found at $NAV_PY"
        echo "  Try running: navir --update"
        exit 1
    fi
    python3 "$NAV_PY" "$@"
    ;;

esac
LAUNCHEREOF

chmod +x "$BIN_DIR/navir"
echo "  ✓ navir command created at $BIN_DIR/navir"

# ── 7. Create user plugin dir ─────────────────────────────────
mkdir -p "$HOME/.navigator/plugins"
echo "  ✓ Plugin folder ready at ~/.navigator/plugins/"

# ── Done ──────────────────────────────────────────────────────
echo ""
echo "  ✓ All done!"
echo ""
echo "  navir              → launch"
echo "  navir --update     → update to latest version"
echo "  navir --version    → show version"
echo "  navir --help       → show all commands"
echo ""
echo "  To add a plugin:"
echo "    Drop any .py file into ~/.navigator/plugins/"
echo ""
