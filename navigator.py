#!/usr/bin/env python3
"""
HACKER FILE NAVIGATOR
─────────────────────────────────────────────────────────────────────────────
A keyboard-driven terminal file manager with a live plugin system.

Plugin dirs (both scanned on startup):
  ./plugins/              — bundled plugins, next to this script
  ~/.navigator/plugins/   — user-installed plugins

Each plugin must expose:  register(api)  — receives a PluginAPI instance.
See docs/PLUGIN_DEV.md for the full authoring guide.
─────────────────────────────────────────────────────────────────────────────
"""

import os
import sys
import curses
import shutil
import shlex
import signal
import datetime
import zipfile
import importlib.util
import traceback

# ══════════════════════════════════════════════════════════════════════════════
#  PATHS
# ══════════════════════════════════════════════════════════════════════════════

RECYCLE_BIN = os.path.expanduser("~/recycle_bin")
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIRS = [
    os.path.join(SCRIPT_DIR, "plugins"),
    os.path.expanduser("~/.navigator/plugins"),
]
PLUGIN_LOG  = os.path.expanduser("~/.navigator/plugin_errors.log")

# ══════════════════════════════════════════════════════════════════════════════
#  KEY CODES  (Ctrl+<letter> = ord(letter) - 64)
# ══════════════════════════════════════════════════════════════════════════════

CTRL_C = 3   # copy
CTRL_D = 4   # delete
CTRL_F = 6   # new folder
CTRL_N = 14  # new file
CTRL_R = 18  # rename
CTRL_T = 20  # context menu
CTRL_V = 22  # paste
CTRL_X = 24  # cut

# ══════════════════════════════════════════════════════════════════════════════
#  COLOR PAIR IDs
# ══════════════════════════════════════════════════════════════════════════════

C_NORMAL = 1   # green — general text
C_DIR    = 2   # cyan  — directories
C_FILE   = 3   # white — files
C_SELECT = 4   # black on green — selected row
C_STATUS = 5   # yellow — status / info
C_ERROR  = 6   # red   — errors
C_TITLE  = 7   # green bold — title bar
C_POPUP  = 8   # black on cyan — popup windows
C_PSEP   = 9   # white on cyan — popup separators
C_CUT    = 10  # magenta — cut indicator
C_TAG    = 11  # yellow — plugin status tags

# ══════════════════════════════════════════════════════════════════════════════
#  MENUS
# ══════════════════════════════════════════════════════════════════════════════

CORE_FOOTER = (
    " Ctrl+T menu | ↑↓/WS nav | Enter open | "
    "^N file | ^F folder | ^C copy | ^X cut | ^V paste | "
    "^D delete | ^R rename | / search | Esc back | Q quit"
)

CORE_MENU = [
    ("Ctrl+N", "New File"),
    ("Ctrl+F", "New Folder"),
    ("──────", "──────────────"),
    ("Ctrl+C", "Copy"),
    ("Ctrl+X", "Cut / Move"),
    ("Ctrl+V", "Paste"),
    ("──────", "──────────────"),
    ("Ctrl+R", "Rename"),
    ("Ctrl+D", "Delete → Recycle"),
    ("──────", "──────────────"),
    ("/",      "Search"),
    ("Esc",    "Go Up"),
    ("Q",      "Quit"),
]

CORE_KEY_MAP = {
    "Ctrl+N": CTRL_N, "Ctrl+F": CTRL_F,
    "Ctrl+C": CTRL_C, "Ctrl+X": CTRL_X,
    "Ctrl+V": CTRL_V, "Ctrl+R": CTRL_R,
    "Ctrl+D": CTRL_D,
    "/": ord("/"), "Esc": 27, "Q": ord("q"),
}

# ══════════════════════════════════════════════════════════════════════════════
#  META CACHE  — avoids calling os.stat() on every draw frame
# ══════════════════════════════════════════════════════════════════════════════

_meta_cache: dict = {}
_META_CACHE_MAX  = 400
_META_CACHE_TRIM = 200   # trim down to this when limit hit

def file_meta(path: str, name: str) -> str:
    """Return a short 'size  date' string for a file/dir entry."""
    full = os.path.join(path, name.rstrip("/"))
    try:
        st    = os.stat(full)
        mtime = st.st_mtime
        key   = (path, name)
        cached = _meta_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        size = st.st_size
        ts   = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        s = (f"{size/1_048_576:.1f}MB" if size >= 1_048_576
             else f"{size/1024:.1f}KB"  if size >= 1024
             else f"{size}B")
        result = f"  {s}  {ts}"
        if len(_meta_cache) >= _META_CACHE_MAX:
            # trim oldest half rather than dropping one entry at a time
            keep = dict(list(_meta_cache.items())[_META_CACHE_TRIM:])
            _meta_cache.clear()
            _meta_cache.update(keep)
        _meta_cache[key] = (mtime, result)
        return result
    except Exception:
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  NAVIGATOR STATE
# ══════════════════════════════════════════════════════════════════════════════

class NavState:
    """All mutable runtime state in one place. Plugins never touch this directly."""

    def __init__(self):
        self.path          = os.getcwd()
        self.search_query  = ""
        self.is_search     = False
        self.top           = 0
        self.selected      = 0
        self.clipboard     = None   # absolute path
        self.is_cut        = False
        self.status        = ""     # one-shot message, cleared after one draw
        self.stdscr        = None
        self._items_cache  = []     # current visible item list, shared with API

    def reset_nav(self, new_path: str = None):
        """Atomically reset selection + search, optionally cd to new_path."""
        if new_path is not None:
            self.path = new_path
        self.selected     = 0
        self.top          = 0
        self.search_query = ""
        self._items_cache = []

# ══════════════════════════════════════════════════════════════════════════════
#  PLUGIN ERROR LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def _log_plugin_error(context: str, exc: Exception):
    """Append plugin runtime errors to ~/.navigator/plugin_errors.log."""
    try:
        os.makedirs(os.path.dirname(PLUGIN_LOG), exist_ok=True)
        ts  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"[{ts}] {context}: {exc}\n{traceback.format_exc()}\n"
        with open(PLUGIN_LOG, "a") as f:
            f.write(msg)
    except Exception:
        pass   # logging must never crash the app

# ══════════════════════════════════════════════════════════════════════════════
#  PLUGIN API
# ══════════════════════════════════════════════════════════════════════════════

class PluginAPI:
    """
    The ONLY interface plugins are allowed to use.

    Plugins must never:
      • Import or access NavState directly
      • Call curses functions themselves
      • Access any private attribute (anything starting with _)

    See docs/PLUGIN_DEV.md for the full guide.
    """

    def __init__(self, state: NavState):
        self._state         = state
        self._keybinds      = {}   # key_int → (label, shortcut_str, callback)
        self._menu_entries  = []   # (shortcut_str, label) — appended to Ctrl+T
        self._hover_hooks   = []   # fn(api, path, item) → str tag or None
        self._open_hooks    = []   # fn(api, path, item) → bool
        self._startup_hooks = []   # fn()
        self._status_hooks  = []   # fn(api, path, item) → str or None

    # ── Registration ──────────────────────────────────────────────────────────

    def add_keybind(self, key_str: str, label: str, callback):
        """
        Register a Ctrl+<letter> shortcut.
        Automatically added to the Ctrl+T context menu.
        callback(api, path, selected_item)

        Reserved by core:  C  D  F  N  R  T  V  X
        Safe to use:       B  E  G  H  I  J  K  L  O  P  U  Y  Z
        Note: W, S, Q are navigation keys — avoid using them even though
              they're not Ctrl-prefixed, to prevent confusion.
        """
        key_int = self._parse_key(key_str)
        if key_int is None:
            return
        self._keybinds[key_int] = (label, key_str, callback)
        self._menu_entries.append((key_str, label))

    def on_file_hover(self, fn):
        """
        fn(api, path, item) → tag string (≤8 chars) or None.
        Shown in brackets next to the selected filename on every frame.
        Keep this fast — avoid slow I/O here; use caching.
        """
        self._hover_hooks.append(fn)

    def on_file_open(self, fn):
        """
        fn(api, path, item) → True if your plugin handled the open.
        Returning True skips the default editor. Return False to fall through.
        """
        self._open_hooks.append(fn)

    def on_startup(self, fn):
        """fn() — called once after all plugins have been loaded."""
        self._startup_hooks.append(fn)

    def on_status(self, fn):
        """
        fn(api, path, item) → str or None.
        Text appended to the status bar for the currently selected file.
        Called once per draw frame — keep it fast.
        """
        self._status_hooks.append(fn)

    # ── Read state ────────────────────────────────────────────────────────────

    def get_current_path(self) -> str:
        """Returns the absolute path of the currently open directory."""
        return self._state.path

    def get_selected_item(self):
        """
        Returns the currently highlighted filename (with trailing / for dirs)
        or None if the selection is on a placeholder row.
        """
        items = self._state._items_cache
        idx   = self._state.selected
        if 0 <= idx < len(items) and not is_placeholder(items[idx]):
            return items[idx]
        return None

    def get_clipboard(self):
        """Returns (path, is_cut). path is None if clipboard is empty."""
        return self._state.clipboard, self._state.is_cut

    # ── Write state ───────────────────────────────────────────────────────────

    def set_clipboard(self, path: str, is_cut: bool = False):
        """Put a path on the clipboard. is_cut=True marks it for move."""
        self._state.clipboard = path
        self._state.is_cut    = is_cut

    def navigate_to(self, path: str):
        """Navigate to a different directory. Resets selection and search."""
        if os.path.isdir(path):
            self._state.reset_nav(path)

    def show_status(self, msg: str, is_error: bool = False):
        """
        Show a message in the status bar for one frame.
        is_error=True renders in red.
        """
        self._state.status = ("ERROR: " + msg) if is_error else msg

    # ── UI helpers ────────────────────────────────────────────────────────────

    def prompt(self, msg: str) -> str:
        """
        Show an inline text input at the bottom of the screen.
        Returns the entered string, or '' if the user cancelled.
        """
        return _prompt(self._state.stdscr, msg)

    def show_popup(self, title: str, lines: list):
        """
        Show a blocking info popup. Waits for any key to close.

        lines: list of (left_col, right_col) tuples.
               A left_col starting with '─' renders as a separator line.

        Example:
            api.show_popup("My Plugin", [
                ("File",  "README.md"),
                ("Size",  "4.2 KB"),
                ("──────", ""),
                ("",      "Press any key to close"),
            ])
        """
        _draw_popup(self._state.stdscr, title, lines)
        _safe_getch(self._state.stdscr)
        self._state.stdscr.touchwin()
        self._state.stdscr.refresh()

    def refresh(self):
        """Force an immediate screen redraw. Rarely needed."""
        if self._state.stdscr:
            self._state.stdscr.refresh()

    # ── Internals (not part of the public API) ────────────────────────────────

    def _parse_key(self, key_str: str):
        s = key_str.strip()
        if s.startswith("Ctrl+"):
            ch = s[5:].upper()
            if len(ch) == 1 and 'A' <= ch <= 'Z':
                return ord(ch) - 64
        return None

    def _build_menu(self):
        menu    = list(CORE_MENU)
        key_map = dict(CORE_KEY_MAP)
        if self._menu_entries:
            menu.append(("──────", "──────────────"))
            for short, label in self._menu_entries:
                menu.append((short, label))
                ki = self._parse_key(short)
                if ki:
                    key_map[short] = ki
        return menu, key_map

    def _run_hover_hooks(self, path: str, item: str) -> str:
        if is_placeholder(item):
            return ""
        tags = []
        for fn in self._hover_hooks:
            try:
                t = fn(self, path, item)
                if t:
                    tags.append(str(t)[:8])
            except Exception as e:
                _log_plugin_error(f"on_file_hover in {fn.__module__}", e)
        return "  ".join(tags)

    def _run_open_hooks(self, path: str, item: str) -> bool:
        for fn in self._open_hooks:
            try:
                if fn(self, path, item):
                    return True
            except Exception as e:
                _log_plugin_error(f"on_file_open in {fn.__module__}", e)
        return False

    def _run_status_hooks(self, path: str, item: str) -> str:
        if is_placeholder(item):
            return ""
        parts = []
        for fn in self._status_hooks:
            try:
                t = fn(self, path, item)
                if t:
                    parts.append(str(t))
            except Exception as e:
                _log_plugin_error(f"on_status in {fn.__module__}", e)
        return "  |  ".join(parts)

    def _run_startup_hooks(self):
        for fn in self._startup_hooks:
            try:
                fn()
            except Exception as e:
                _log_plugin_error(f"on_startup in {fn.__module__}", e)

    def _dispatch_keybind(self, key_int: int) -> bool:
        if key_int not in self._keybinds:
            return False
        _, _, cb = self._keybinds[key_int]
        try:
            item = self.get_selected_item()
            cb(self, self._state.path, item)
        except Exception as e:
            self._state.status = f"ERROR (plugin): {e}"
            _log_plugin_error(f"keybind callback {cb.__name__}", e)
        return True

# ══════════════════════════════════════════════════════════════════════════════
#  PLUGIN LOADER
# ══════════════════════════════════════════════════════════════════════════════

class PluginLoader:

    def __init__(self, api: PluginAPI):
        self.api    = api
        self.loaded = []   # list of (name, version, description)
        self.failed = []   # list of (filename, short_error_str)

    def load_all(self):
        seen = set()
        for directory in PLUGIN_DIRS:
            os.makedirs(directory, exist_ok=True)
            try:
                fnames = sorted(os.listdir(directory))
            except Exception:
                continue
            for fname in fnames:
                if not fname.endswith(".py") or fname.startswith("_"):
                    continue
                if fname in seen:
                    continue    # bundled takes priority; skip user duplicate
                seen.add(fname)
                self._load_one(fname, os.path.join(directory, fname))
        self.api._run_startup_hooks()

    def _load_one(self, fname: str, fpath: str):
        try:
            spec   = importlib.util.spec_from_file_location(fname[:-3], fpath)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not hasattr(module, "register"):
                raise AttributeError(
                    f"{fname} is missing a register(api) function. "
                    "See docs/PLUGIN_DEV.md."
                )
            module.register(self.api)
            self.loaded.append((
                getattr(module, "NAME",        fname[:-3]),
                getattr(module, "VERSION",     "?"),
                getattr(module, "DESCRIPTION", ""),
            ))
        except Exception:
            short = traceback.format_exc().strip().splitlines()[-1][:60]
            self.failed.append((fname, short))
            _log_plugin_error(f"loading {fname}", Exception(short))

    def startup_report(self) -> list:
        lines = []
        for name, ver, desc in self.loaded:
            lines.append((f"v{ver}", f"✓ {name}  —  {desc}"))
        for fname, err in self.failed:
            lines.append(("FAIL", f"✗ {fname}: {err}"))
        if not lines:
            lines = [("—", "No plugins found.  Drop .py files into ./plugins/")]
        if self.failed:
            lines.append(("──────", "──────────────────────────────────"))
            lines.append(("log", f"Errors logged → {PLUGIN_LOG}"))
        lines.append(("──────", "──────────────────────────────────"))
        lines.append(("", "Press any key to start..."))
        return lines

# ══════════════════════════════════════════════════════════════════════════════
#  COLORS
# ══════════════════════════════════════════════════════════════════════════════

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_NORMAL, curses.COLOR_GREEN,   curses.COLOR_BLACK)
    curses.init_pair(C_DIR,    curses.COLOR_CYAN,    curses.COLOR_BLACK)
    curses.init_pair(C_FILE,   curses.COLOR_WHITE,   curses.COLOR_BLACK)
    curses.init_pair(C_SELECT, curses.COLOR_BLACK,   curses.COLOR_GREEN)
    curses.init_pair(C_STATUS, curses.COLOR_YELLOW,  curses.COLOR_BLACK)
    curses.init_pair(C_ERROR,  curses.COLOR_RED,     curses.COLOR_BLACK)
    curses.init_pair(C_TITLE,  curses.COLOR_GREEN,   curses.COLOR_BLACK)
    curses.init_pair(C_POPUP,  curses.COLOR_BLACK,   curses.COLOR_CYAN)
    curses.init_pair(C_PSEP,   curses.COLOR_WHITE,   curses.COLOR_CYAN)
    curses.init_pair(C_CUT,    curses.COLOR_MAGENTA, curses.COLOR_BLACK)
    curses.init_pair(C_TAG,    curses.COLOR_YELLOW,  curses.COLOR_BLACK)

# ══════════════════════════════════════════════════════════════════════════════
#  FILE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_items(path: str) -> list:
    try:
        entries = os.listdir(path)
    except PermissionError:
        return ["(permission denied)"]
    except Exception as e:
        return [f"(error: {e})"]
    dirs  = sorted(d + "/" for d in entries if os.path.isdir(os.path.join(path, d)))
    files = sorted(f for f in entries if not os.path.isdir(os.path.join(path, f)))
    return dirs + files or ["(empty folder)"]

def filter_items(items: list, query: str) -> list:
    if not query:
        return items
    filtered = [i for i in items if query.lower() in i.lower()]
    return filtered or ["(no match)"]

def is_placeholder(item: str) -> bool:
    """True for any synthetic list entry like (empty folder), (no match), etc."""
    return item.startswith("(") and item.endswith(")")

# ══════════════════════════════════════════════════════════════════════════════
#  CURSES HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _safe_getch(stdscr) -> int:
    """
    getch() wrapper that never raises KeyboardInterrupt.
    With curses.raw() active Ctrl+C is already keycode 3, but this acts
    as a safety net for Termux and other non-standard terminals.
    """
    try:
        return stdscr.getch()
    except KeyboardInterrupt:
        return CTRL_C

def _safe_addstr(stdscr, y: int, x: int, text: str, attr=0):
    """addstr that silently drops the write if it would overflow the screen."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    text = text[:w - x]
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass   # writing to bottom-right corner raises on some terminals

# ══════════════════════════════════════════════════════════════════════════════
#  DRAWING — MAIN VIEW
# ══════════════════════════════════════════════════════════════════════════════

def draw_main(stdscr, state: NavState, items: list, api: PluginAPI):
    stdscr.erase()   # erase() is cheaper than clear() — avoids full repaint
    h, w = stdscr.getmaxyx()

    # ── Title ──
    _safe_addstr(stdscr, 0, 0,
                 " ◈ HACKER FILE NAVIGATOR ◈ ".center(w - 1),
                 curses.color_pair(C_TITLE) | curses.A_BOLD)

    # ── Path ──
    _safe_addstr(stdscr, 1, 0,
                 f" PATH: {state.path}"[:w - 1],
                 curses.color_pair(C_NORMAL))

    # ── Clipboard indicator ──
    row = 2
    if state.clipboard:
        label    = "✂ CUT" if state.is_cut else "⎘ COPY"
        basename = os.path.basename(state.clipboard)
        _safe_addstr(stdscr, row, 0,
                     f" {label}: {basename} "[:w - 1],
                     curses.color_pair(C_CUT if state.is_cut else C_STATUS) | curses.A_BOLD)
        row = 3

    # ── File list ──
    list_start = row
    max_rows   = h - list_start - 3

    for vis_i in range(max_rows):
        idx = state.top + vis_i
        if idx >= len(items):
            break
        item   = items[idx]
        is_dir = item.endswith("/")
        prefix = "[DIR] " if is_dir else "[   ] "
        meta   = file_meta(state.path, item) if not is_placeholder(item) else ""

        tag = ""
        if idx == state.selected and not is_placeholder(item):
            raw = api._run_hover_hooks(state.path, item)
            if raw:
                tag = f"  [{raw}]"

        label = f"{prefix}{item.rstrip('/')}{meta}{tag}"
        r     = list_start + vis_i

        if idx == state.selected:
            _safe_addstr(stdscr, r, 0,
                         label[:w - 1].ljust(w - 1),
                         curses.color_pair(C_SELECT) | curses.A_BOLD)
        elif is_placeholder(item):
            _safe_addstr(stdscr, r, 0, label[:w - 1],
                         curses.color_pair(C_STATUS) | curses.A_DIM)
        elif is_dir:
            _safe_addstr(stdscr, r, 0, label[:w - 1],
                         curses.color_pair(C_DIR) | curses.A_BOLD)
        else:
            _safe_addstr(stdscr, r, 0, label[:w - 1],
                         curses.color_pair(C_FILE))

    # ── Status bar ──
    if state.status:
        color = C_ERROR if state.status.startswith("ERROR") else C_STATUS
        _safe_addstr(stdscr, h - 2, 0,
                     state.status[:w - 1].ljust(w - 1),
                     curses.color_pair(color) | curses.A_BOLD)
    else:
        sel = items[state.selected] if 0 <= state.selected < len(items) else None
        if sel and not is_placeholder(sel):
            extra = api._run_status_hooks(state.path, sel)
            if extra:
                _safe_addstr(stdscr, h - 2, 0,
                             extra[:w - 1].ljust(w - 1),
                             curses.color_pair(C_TAG))

    # ── Footer / search ──
    if state.is_search or state.search_query:
        _safe_addstr(stdscr, h - 1, 0,
                     f" SEARCH: {state.search_query}_"[:w - 1],
                     curses.color_pair(C_STATUS) | curses.A_BOLD)
    else:
        _safe_addstr(stdscr, h - 1, 0,
                     CORE_FOOTER[:w - 1],
                     curses.color_pair(C_NORMAL) | curses.A_DIM)

    stdscr.refresh()

# ══════════════════════════════════════════════════════════════════════════════
#  DRAWING — POPUP
# ══════════════════════════════════════════════════════════════════════════════

def _draw_popup(stdscr, title: str, lines: list, selected_idx: int = None):
    h, w = stdscr.getmaxyx()
    if not lines:
        lines = [("", "")]
    inner_w = max((len(str(l)) + len(str(r)) + 6) for l, r in lines) + 2
    box_w   = min(w - 4, max(inner_w, len(title) + 6))
    box_h   = min(h - 2, len(lines) + 4)
    by      = max(0, (h - box_h) // 2)
    bx      = max(0, (w - box_w) // 2)

    try:
        win = curses.newwin(box_h, box_w, by, bx)
    except curses.error:
        return None   # terminal too small

    win.bkgd(" ", curses.color_pair(C_POPUP))
    win.box()
    win.addstr(1, max(1, (box_w - len(title)) // 2),
               title[:box_w - 2],
               curses.color_pair(C_POPUP) | curses.A_BOLD)
    win.addstr(2, 1, "─" * (box_w - 2), curses.color_pair(C_PSEP))

    for i, (left, right) in enumerate(lines):
        r = i + 3
        if r >= box_h - 1:
            break
        left, right = str(left), str(right)
        if left.startswith("─"):
            win.addstr(r, 1, "─" * (box_w - 2),
                       curses.color_pair(C_PSEP) | curses.A_DIM)
        elif i == selected_idx:
            row_text = f"  {left:<10} {right}  "[:box_w - 2].ljust(box_w - 2)
            win.addstr(r, 1, row_text,
                       curses.color_pair(C_SELECT) | curses.A_BOLD)
        else:
            win.addstr(r, 2, f"{left:<10}"[:box_w - 3],
                       curses.color_pair(C_POPUP) | curses.A_DIM)
            win.addstr(r, 13, right[:box_w - 15],
                       curses.color_pair(C_POPUP) | curses.A_BOLD)

    win.refresh()
    return win

# ══════════════════════════════════════════════════════════════════════════════
#  CONTEXT MENU  (Ctrl+T)
# ══════════════════════════════════════════════════════════════════════════════

def show_context_menu(stdscr, api: PluginAPI):
    menu, key_map = api._build_menu()
    selectable    = [i for i, (l, _) in enumerate(menu) if not l.startswith("─")]
    sel_pos       = 0

    while True:
        real_idx = selectable[sel_pos]
        _draw_popup(stdscr, "  ◈ ACTIONS ◈  ", menu, selected_idx=real_idx)
        key = _safe_getch(stdscr)

        if key in (27, CTRL_T):
            return None
        elif key in (curses.KEY_UP, ord("w"), ord("W")):
            sel_pos = (sel_pos - 1) % len(selectable)
        elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
            sel_pos = (sel_pos + 1) % len(selectable)
        elif key in (10, curses.KEY_ENTER):
            short = menu[real_idx][0]
            return key_map.get(short)

# ══════════════════════════════════════════════════════════════════════════════
#  DESTINATION PICKER
# ══════════════════════════════════════════════════════════════════════════════

def pick_destination(stdscr, start_path: str, title: str = "SELECT DESTINATION"):
    """Full-screen directory browser. Returns chosen path or None if cancelled."""
    path     = start_path
    selected = 0
    top      = 0

    while True:
        try:
            entries = sorted(os.listdir(path))
        except Exception:
            entries = []
        dirs  = [d + "/" for d in entries if os.path.isdir(os.path.join(path, d))]
        items = dirs or ["(no sub-folders)"]
        h, w  = stdscr.getmaxyx()
        selected = max(0, min(selected, len(items) - 1))
        top      = max(0, min(top, selected))

        stdscr.erase()
        _safe_addstr(stdscr, 0, 0,
                     f" ◈ {title} ◈ ".center(w - 1),
                     curses.color_pair(C_TITLE) | curses.A_BOLD)
        _safe_addstr(stdscr, 1, 0,
                     f" CURRENT: {path}"[:w - 1],
                     curses.color_pair(C_STATUS))
        _safe_addstr(stdscr, 2, 0, "─" * (w - 1),
                     curses.color_pair(C_NORMAL) | curses.A_DIM)

        max_rows = h - 7
        for vis_i in range(max_rows):
            idx = top + vis_i
            if idx >= len(items):
                break
            item  = items[idx]
            label = f"  [DIR] {item.rstrip('/')}"
            if idx == selected:
                _safe_addstr(stdscr, 3 + vis_i, 0,
                             label[:w - 1].ljust(w - 1),
                             curses.color_pair(C_SELECT) | curses.A_BOLD)
            else:
                _safe_addstr(stdscr, 3 + vis_i, 0,
                             label[:w - 1],
                             curses.color_pair(C_DIR) | curses.A_BOLD)

        _safe_addstr(stdscr, h - 3, 0, "─" * (w - 1),
                     curses.color_pair(C_NORMAL) | curses.A_DIM)
        _safe_addstr(stdscr, h - 2, 0,
                     f" Move here: {path} "[:w - 1],
                     curses.color_pair(C_STATUS) | curses.A_BOLD)
        _safe_addstr(stdscr, h - 1, 0,
                     " ↑↓/WS nav | Enter go into | Space select | B back | Esc cancel "[:w - 1],
                     curses.color_pair(C_NORMAL) | curses.A_DIM)
        stdscr.refresh()

        key = _safe_getch(stdscr)
        if key == 27:
            return None
        elif key == ord(" "):
            return path
        elif key in (curses.KEY_UP, ord("w"), ord("W")):
            if selected > 0:
                selected -= 1
                if selected < top:
                    top = selected
        elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
            if selected < len(items) - 1:
                selected += 1
                if selected >= top + max_rows:
                    top = selected - max_rows + 1
        elif key in (10, curses.KEY_ENTER):
            if items[selected] != "(no sub-folders)":
                path = os.path.join(path, items[selected][:-1])
                selected = top = 0
        elif key in (ord("b"), ord("B")):
            parent = os.path.dirname(path)
            if parent and parent != path:
                path = parent
                selected = top = 0

# ══════════════════════════════════════════════════════════════════════════════
#  INPUT / EDITOR
# ══════════════════════════════════════════════════════════════════════════════

def _prompt(stdscr, msg: str) -> str:
    """Inline single-line input drawn at the bottom of the screen."""
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, h - 2, 0,
                 (msg + " ")[:w - 1].ljust(w - 1),
                 curses.color_pair(C_STATUS))
    stdscr.refresh()
    curses.echo()
    curses.curs_set(1)
    try:
        raw = stdscr.getstr(h - 2, len(msg) + 1, w - len(msg) - 2)
        inp = raw.decode("utf-8", errors="ignore").strip()
    except KeyboardInterrupt:
        inp = ""
    finally:
        curses.noecho()
        curses.curs_set(0)
    _safe_addstr(stdscr, h - 2, 0, " " * (w - 1))
    return inp


def _resolve_editor() -> str:
    """Return the best available editor. Errors if none found."""
    for candidate in ["micro", os.environ.get("EDITOR", ""), "nano", "vi"]:
        if candidate and shutil.which(candidate):
            return candidate
    return ""


def open_editor(state: NavState, filepath: str):
    """
    Suspend curses, open the file in an editor, then restore.
    Does NOT call curses.initscr() — reuses the existing window.
    """
    editor = _resolve_editor()
    if not editor:
        state.status = "ERROR: No editor found. Set $EDITOR or install nano/micro."
        return

    curses.endwin()
    try:
        os.system(f"{editor} {shlex.quote(filepath)}")
    finally:
        # Restore terminal state without creating a new window object
        state.stdscr.keypad(True)
        curses.raw()
        init_colors()
        state.stdscr.refresh()

# ══════════════════════════════════════════════════════════════════════════════
#  FILE OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

def recycle(src: str):
    try:
        os.makedirs(RECYCLE_BIN, exist_ok=True)
        name = os.path.basename(src)
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        arc  = os.path.join(RECYCLE_BIN, f"{name}_{ts}.zip")
        with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as zf:
            if os.path.isfile(src):
                zf.write(src, arcname=name)
            else:
                for root, _, files in os.walk(src):
                    for f in files:
                        fp = os.path.join(root, f)
                        zf.write(fp, arcname=os.path.relpath(fp, os.path.dirname(src)))
        if os.path.isdir(src):
            shutil.rmtree(src)
        else:
            os.remove(src)
        return True, f"Recycled → {arc}"
    except Exception as e:
        return False, f"ERROR: {e}"


def paste_item(clipboard: str, is_cut: bool, dest_dir: str):
    if not clipboard or not os.path.exists(clipboard):
        return False, "ERROR: Clipboard source missing."
    try:
        name = os.path.basename(clipboard)
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            base, ext = (os.path.splitext(name) if not os.path.isdir(clipboard)
                         else (name, ""))
            dest = os.path.join(dest_dir, f"{base}(copy){ext}")
        if is_cut:
            shutil.move(clipboard, dest)
        elif os.path.isdir(clipboard):
            shutil.copytree(clipboard, dest)
        else:
            shutil.copy2(clipboard, dest)
        return True, f"{'Moved' if is_cut else 'Pasted'} → {dest}"
    except Exception as e:
        return False, f"ERROR: {e}"


def create_new(path: str, name: str, is_dir: bool):
    if not name:
        return False, "ERROR: Name cannot be empty."
    target = os.path.join(path, name)
    if os.path.exists(target):
        return False, f"ERROR: '{name}' already exists."
    try:
        if is_dir:
            os.makedirs(target)
        else:
            with open(target, "w"):
                pass
        return True, f"Created: {target}"
    except Exception as e:
        return False, f"ERROR: {e}"


def rename_item(path: str, old_name: str, new_name: str):
    if not new_name:
        return False, "ERROR: Name cannot be empty."
    src  = os.path.join(path, old_name.rstrip("/"))
    dest = os.path.join(path, new_name)
    if os.path.exists(dest):
        return False, f"ERROR: '{new_name}' already exists."
    try:
        os.rename(src, dest)
        return True, f"Renamed → {new_name}"
    except Exception as e:
        return False, f"ERROR: {e}"

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def main(stdscr):
    curses.raw()          # suppress SIGINT — Ctrl+C becomes keycode 3
    curses.curs_set(0)
    init_colors()
    stdscr.keypad(True)

    state        = NavState()
    state.stdscr = stdscr

    api    = PluginAPI(state)
    loader = PluginLoader(api)
    loader.load_all()

    _draw_popup(stdscr, "  ◈ NAVIGATOR ◈  ", loader.startup_report())
    _safe_getch(stdscr)

    while True:
        stdscr = state.stdscr   # may be refreshed by open_editor

        all_items = get_items(state.path)
        items     = filter_items(all_items, state.search_query)
        state.selected     = max(0, min(state.selected, len(items) - 1))
        state.top          = max(0, min(state.top, state.selected))
        state._items_cache = items   # share with PluginAPI.get_selected_item()

        draw_main(stdscr, state, items, api)
        state.status = ""

        key = _safe_getch(stdscr)

        # ── Search mode ──
        if state.is_search:
            if key == 27:
                state.is_search = False
                state.search_query = ""
                state.selected = state.top = 0
            elif key in (10, curses.KEY_ENTER):
                state.is_search = False
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                state.search_query = state.search_query[:-1]
            elif 32 <= key <= 126:
                state.search_query += chr(key)
            continue

        # ── Context menu ──
        if key == CTRL_T:
            chosen = show_context_menu(stdscr, api)
            if chosen is None:
                continue
            key = chosen

        # ── Plugin keybinds (before core — plugins can use any free Ctrl key) ──
        if api._dispatch_keybind(key):
            continue

        # ── Navigation ──
        if key in (curses.KEY_UP, ord("w"), ord("W")):
            if state.selected > 0:
                state.selected -= 1
                if state.selected < state.top:
                    state.top = state.selected

        elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
            if state.selected < len(items) - 1:
                state.selected += 1
                vis_rows = stdscr.getmaxyx()[0] - (4 if state.clipboard else 3) - 3
                if state.selected >= state.top + vis_rows:
                    state.top = state.selected - vis_rows + 1

        elif key in (10, curses.KEY_ENTER):
            if not is_placeholder(items[state.selected]):
                choice = items[state.selected]
                if choice.endswith("/"):
                    state.reset_nav(os.path.join(state.path, choice[:-1]))
                else:
                    full = os.path.join(state.path, choice)
                    if not api._run_open_hooks(state.path, choice):
                        open_editor(state, full)

        elif key == 27:
            parent = os.path.dirname(state.path)
            if parent and parent != state.path:
                state.reset_nav(parent)

        elif key in (ord("q"), ord("Q")):
            return

        elif key == ord("/"):
            state.is_search    = True
            state.search_query = ""
            state.selected = state.top = 0

        # ── File operations ──
        elif key == CTRL_N:
            name = _prompt(stdscr, "New file name:")
            _, state.status = create_new(state.path, name, is_dir=False)

        elif key == CTRL_F:
            name = _prompt(stdscr, "New folder name:")
            _, state.status = create_new(state.path, name, is_dir=True)

        elif key == CTRL_C:
            if not is_placeholder(items[state.selected]):
                state.clipboard = os.path.join(state.path, items[state.selected].rstrip("/"))
                state.is_cut    = False
                state.status    = f"⎘ Copied: {os.path.basename(state.clipboard)}"

        elif key == CTRL_X:
            if not is_placeholder(items[state.selected]):
                state.clipboard = os.path.join(state.path, items[state.selected].rstrip("/"))
                state.is_cut    = True
                state.status    = f"✂ Cut: {os.path.basename(state.clipboard)}"

        elif key == CTRL_V:
            if state.clipboard:
                if state.is_cut:
                    dest = pick_destination(stdscr, state.path,
                                            "MOVE TO — SELECT DESTINATION")
                    if dest:
                        ok, state.status = paste_item(state.clipboard, True, dest)
                        if ok:
                            state.clipboard = None
                            state.is_cut    = False
                        state.selected = 0
                    else:
                        state.status = "Move cancelled."
                else:
                    ok, state.status = paste_item(state.clipboard, False, state.path)
                    if ok:
                        state.clipboard = None
                    state.selected = 0
            else:
                state.status = "ERROR: Clipboard is empty."

        elif key == CTRL_D:
            if not is_placeholder(items[state.selected]):
                choice = items[state.selected]
                ans = _prompt(stdscr, f"Recycle '{choice.rstrip('/')}'? (y/n):")
                if ans.lower() == "y":
                    _, state.status = recycle(
                        os.path.join(state.path, choice.rstrip("/")))
                    state.selected = max(0, state.selected - 1)

        elif key == CTRL_R:
            if not is_placeholder(items[state.selected]):
                old = items[state.selected]
                new = _prompt(stdscr, f"Rename '{old.rstrip('/')}' to:")
                _, state.status = rename_item(state.path, old, new)
                state.selected  = max(0, state.selected - 1)


if __name__ == "__main__":
    curses.wrapper(main)
