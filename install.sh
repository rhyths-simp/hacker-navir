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
python3 $NAV_PY "\$@"
EOF

chmod +x "$BIN_DIR/navir"
echo "  ✓ navir command created at $BIN_DIR/navir"

# ── 5. Create user plugin dir ─────────────────────────────────
mkdir -p "$HOME/.navigator/plugins"
echo "  ✓ Plugin folder ready at ~/.navigator/plugins/"

# ── Done ──────────────────────────────────────────────────────
echo ""
echo "  ✓ All done! Type  navir  to launch."
echo ""
echo "  To add a plugin:"
echo "    Drop any .py file into ~/.navigator/plugins/"
echo ""
echo "  To update later:"
echo "    Run this installer again — it will pull latest changes."
echo ""
