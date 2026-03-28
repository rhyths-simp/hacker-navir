#!/bin/bash
# ─────────────────────────────────────────────
#  Run this ONCE from inside your navigator folder
#  It creates the "navir" command globally
# ─────────────────────────────────────────────

# Get the full path of navigator.py (wherever you run this from)
NAV_PATH="$(cd "$(dirname "$0")" && pwd)/navigator.py"

# Check navigator.py actually exists here
if [ ! -f "$NAV_PATH" ]; then
    echo "ERROR: navigator.py not found in this folder."
    echo "Make sure you run this script from inside the navigator folder."
    exit 1
fi

# Figure out where to put the navir command
# Termux uses a different bin path than normal Linux
if [ -d "/data/data/com.termux/files/usr/bin" ]; then
    BIN_DIR="/data/data/com.termux/files/usr/bin"
else
    BIN_DIR="/usr/local/bin"
fi

# Write the navir launcher
cat > "$BIN_DIR/navir" << EOF
#!/bin/bash
python3 $NAV_PATH "\$@"
EOF

# Make it executable
chmod +x "$BIN_DIR/navir"

echo ""
echo "  Done! navir is ready."
echo "  Navigator is at: $NAV_PATH"
echo "  Command created: $BIN_DIR/navir"
echo ""
echo "  Just type:  navir"
echo "  From anywhere in your terminal."
echo ""
