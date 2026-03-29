#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Hacker File Navigator — Installer
#  Usage:  curl -fsSL https://raw.githubusercontent.com/rhyths-simp/hacker-navir/main/install.sh | bash
# ─────────────────────────────────────────────────────────────

set -e   # stop on any error

REPO="https://github.com/rhyths-simp/hacker-navir.git"
INSTALL_DIR="$HOME/.navigator/app"

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
    git clone --quiet "$REPO" "$INSTALL_DIR"
fi

echo "  ✓ Files ready at $INSTALL_DIR"

# ── 4. Create navir command ───────────────────────────────────
NAV_PY="$INSTALL_DIR/navigator.py"

# Detect bin dir (Termux vs standard Linux)
if [ -d "/data/data/com.termux/files/usr/bin" ]; then
    BIN_DIR="/data/data/com.termux/files/usr/bin"
elif [ -d "$HOME/.local/bin" ]; then
    BIN_DIR="$HOME/.local/bin"
else
    BIN_DIR="/usr/local/bin"
fi

cat > "$BIN_DIR/navir" << EOF
#!/bin/bash
# ── Hacker File Navigator launcher ──────────────────────────
INSTALL_DIR="\$HOME/.navigator/app"
NAV_PY="$NAV_PY"
VERSION_FILE="\$INSTALL_DIR/version.txt"

case "\$1" in

  --version|-v)
    if [ -f "\$VERSION_FILE" ]; then
      echo "navir \$(cat \$VERSION_FILE)"
    else
      echo "navir (version unknown)"
    fi
    ;;

  --update|-u)
    echo ""
    echo "  ◈ Hacker File Navigator — Updater"
    echo "  ────────────────────────────────────"
    if [ ! -d "\$INSTALL_DIR/.git" ]; then
      echo "  ERROR: Install directory not found."
      echo "  Run the installer again to reinstall."
      exit 1
    fi
    echo "  ↻ Checking for updates..."
    BEFORE=\$(git -C "\$INSTALL_DIR" rev-parse HEAD)
    git -C "$INSTALL_DIR" pull --quiet
        AFTER=$(git -C "$INSTALL_DIR" rev-parse HEAD)
        # always refresh version.txt after pull
        git -C "$INSTALL_DIR" describe --tags --abbrev=0 2>/dev/null > "$VERSION_FILE" || true
        if [ "$BEFORE" = "$AFTER" ]; then
          echo "  ✓ Already up to date."
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
    ;;

  *)
    python3 "\$NAV_PY" "\$@"
    ;;

esac
EOF

chmod +x "$BIN_DIR/navir"
echo "  ✓ navir command created at $BIN_DIR/navir"

# ── 5. Write version file ─────────────────────────────────────
# Pull version from the latest git tag, fallback to v1.0.0
VERSION=$(git -C "$INSTALL_DIR" describe --tags --abbrev=0 2>/dev/null || echo "v1.0.0")
echo "$VERSION" > "$INSTALL_DIR/version.txt"
echo "  ✓ Version: $VERSION"

# ── 6. Create user plugin dir ─────────────────────────────────
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
