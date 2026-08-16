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
import subprocess
import collections
import pty
import tty
import termios
import select
import fcntl
import struct

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

    Need a navigable list/menu (pick a bookmark, pick from history, etc.)?
    Use show_menu() below instead of reaching for curses — that's exactly
    what it's for.

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

        If `lines` is longer than the screen can fit, the popup scrolls
        instead of silently cutting content off — ↑/↓ (or W/S) and
        PageUp/PageDown move through it, and any other key closes it.
        Short content that already fits still closes on any key, same
        as before.

        Example:
            api.show_popup("My Plugin", [
                ("File",  "README.md"),
                ("Size",  "4.2 KB"),
                ("──────", ""),
                ("",      "Press any key to close"),
            ])
        """
        _show_popup_blocking(self._state.stdscr, title, lines)

    def show_menu(self, title: str, rows: list, start_idx: int = 0):
        """
        Show an interactive popup menu the user can navigate with ↑/↓ (or W/S)
        and pick from with Enter. Esc cancels.

        This is the building block for "do something, then show an updated
        list, then let the user act on it again" flows — exactly the pattern
        bookmarks/favorites/history-style plugins need. Call it again in a
        loop (e.g. after adding/removing an entry) to refresh what's shown.

        rows: list of (left_col, right_col) tuples — same format as
              show_popup(). A left_col starting with '─' renders as a
              non-selectable separator and is skipped during navigation.
        start_idx: which selectable row (0-based, separators don't count)
                   should be highlighted when the menu first opens.

        Single-letter row labels (e.g. "A", "R") also work as direct
        shortcut keys — pressing that letter picks the row immediately,
        no need to arrow down to it first. Numbered labels like "[1]"
        are unaffected since they aren't a single character.

        Returns the index into `rows` of the chosen entry, or None if the
        user cancelled with Esc. Re-render rows yourself based on that index
        — show_menu never mutates anything for you.

        Example:
            rows = [
                ("[1]", "/home/user/projects"),
                ("[2]", "/home/user/notes"),
                ("──────", "──────────────────"),
                ("A", "Add current folder"),
            ]
            choice = api.show_menu("  ◈ BOOKMARKS ◈  ", rows)
            if choice is None:
                return                      # user pressed Esc
            label = rows[choice][0]
            if label == "A":
                ...
            else:
                dest = rows[choice][1]
                api.navigate_to(dest)
        """
        stdscr     = self._state.stdscr
        selectable = [i for i, (l, _) in enumerate(rows) if not str(l).startswith("─")]
        if not selectable:
            return None
        pos        = max(0, min(start_idx, len(selectable) - 1))
        scroll_top = 0

        # Single-letter labels double as direct shortcut keys (case-insensitive).
        # W/S/Esc/Enter stay reserved for navigation, so they're excluded even
        # if a row happened to use one of those letters as its label.
        shortcuts = {
            str(rows[i][0]).upper(): i
            for i in selectable
            if len(str(rows[i][0])) == 1 and str(rows[i][0]).upper() not in ("W", "S")
        }

        while True:
            _check_resize(stdscr)
            real_idx = selectable[pos]
            visible_rows = _popup_visible_rows(stdscr, rows, title)
            if real_idx < scroll_top:
                scroll_top = real_idx
            elif real_idx >= scroll_top + visible_rows:
                scroll_top = real_idx - visible_rows + 1
            _draw_popup(stdscr, title, rows, selected_idx=real_idx, scroll_top=scroll_top)
            key = _safe_getch(stdscr)

            if key == 27:
                return None
            elif key in (curses.KEY_UP, ord("w"), ord("W")):
                pos = (pos - 1) % len(selectable)
            elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
                pos = (pos + 1) % len(selectable)
            elif key in (10, curses.KEY_ENTER):
                return real_idx
            elif 0 <= key < 256 and chr(key).upper() in shortcuts:
                return shortcuts[chr(key).upper()]

    def run_interactive(self, cmd: list, cwd: str = None):
        """
        Hand the real terminal to `cmd` via a pseudo-terminal — the child
        gets full control: it draws directly to the screen and updates
        live, reads keyboard input directly (it sees a real tty, so
        curses/color/interactive-prompt code all behave normally), and
        Ctrl+C behaves like a normal terminal SIGINT (kills the child)
        instead of navir's copy shortcut. Control returns to navir
        automatically once the child exits or is killed. Everything the
        child writes is captured as it happens, at the same time it's
        shown live — nothing is sacrificed either way.

        Use this for anything that needs to run and update on screen in
        real time. For a one-shot command where you just want the output
        (no live view needed), use run_captured() instead — this suspends
        the whole UI, so it's not free.

        cmd: argv list, e.g. ["python3", "/full/path/script.py"].
             No shell parsing — pass the interpreter and file separately.
        cwd: working directory for the child (default: navir's own cwd).

        Returns (exit_code, was_interrupted, captured_output).
            exit_code:        int, or None if it couldn't be determined.
                               Follows shell convention for signal deaths
                               (128 + signal number, e.g. 130 for SIGINT).
            was_interrupted:  True if the user pressed Ctrl+C to force-kill it.
            captured_output:  str — everything the child wrote to its
                               terminal, decoded as UTF-8 (invalid bytes
                               replaced). Capped at 2MB; a truncation note
                               is appended if it was hit. Line endings are
                               real-terminal style (\\r\\n) — normalize
                               before saving to a plain-text log if that
                               matters for your use case.

        Raises FileNotFoundError if cmd[0] can't be found/run — same as
        calling subprocess directly, so handle it the way you'd handle any
        missing-executable error.

        Example:
            code, killed, output = api.run_interactive(["python3", full_path])
            if killed:
                api.show_status("Force-killed with Ctrl+C.")
            elif code != 0:
                api.show_status(f"Exited with code {code}.", is_error=True)
                api.show_log("Crash", output)   # full traceback, not just the code
            else:
                api.show_status(f"Exited with code {code}.")
        """
        return _run_interactive(self._state, cmd, cwd)

    def run_captured(self, cmd: list, cwd: str = None, timeout: float = None):
        """
        Run `cmd` non-interactively and get its output back — for anything
        you want the result of, not a live view of (compiling, `git status`,
        checking a version, etc.). Doesn't touch the screen at all.

        cmd: argv list, e.g. ["git", "status", "--short"].
        cwd: working directory for the child (default: navir's own cwd).
        timeout: seconds before the process is killed and treated as timed
                 out (default: no timeout — only set this if the command
                 could plausibly hang, e.g. waiting on stdin).

        Returns a CapturedResult with fields:
            .returncode  int, or None if it timed out
            .stdout      str
            .stderr      str
            .timed_out   bool

        Raises FileNotFoundError if cmd[0] can't be found/run.

        Example:
            r = api.run_captured(["git", "status", "--short"], cwd=path)
            if r.returncode == 0:
                api.show_status(r.stdout.strip() or "clean")
        """
        return _run_captured(cmd, cwd, timeout)

    def which(self, exe: str):
        """
        Resolve an executable name against PATH, cached for the session.
        Returns the full path, or None if it isn't installed.

        Example:
            if api.which("rustc"):
                ...
        """
        return _cached_which(exe)

    def show_log(self, title: str, text: str):
        """
        Show `text` in a full-width, scrollable, blocking viewer — for
        content that's a document rather than a short two-column popup
        (crash logs, long command output, file previews).

        \u2191/\u2193 (or W/S) and PageUp/PageDown scroll. C copies the full,
        unwrapped text to the system clipboard (see copy_to_clipboard()).
        Esc, Enter, or Q closes it.

        Example:
            api.show_log("cube.c — compile failed", full_error_text)
        """
        _show_log_blocking(self._state.stdscr, title, text)

    def copy_to_clipboard(self, text: str) -> bool:
        """
        Copy `text` to the system clipboard, trying whichever tool is
        available: termux-clipboard-set, xclip, xsel, wl-copy, pbcopy.

        Returns True if a tool was found and the copy succeeded, False if
        none of them are installed (common on a bare Termux/server install
        — nothing to fall back to, so tell the user via show_status()).

        Example:
            if not api.copy_to_clipboard(text):
                api.show_status("No clipboard tool found.", is_error=True)
        """
        ok, _tool = _copy_to_system_clipboard(text)
        return ok

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


_resize_pending = False


def _on_winch(signum, frame):
    global _resize_pending
    _resize_pending = True


def _check_resize(stdscr) -> bool:
    """
    Call at the top of any blocking curses input loop (main loop, menus,
    popups, the log viewer, the destination picker — anywhere that blocks
    on getch()). Curses doesn't automatically notice the terminal's actual
    size changing just because a SIGWINCH fired; without this, every
    on-screen box keeps being drawn against whatever size was current at
    curses.wrapper()'s startup, which is exactly what breaks the layout
    when Termux is resized or zoomed. If a resize happened since the last
    check, this re-syncs curses with the terminal's real current size and
    returns True — callers can use that to force a redraw, though most
    already redraw every loop iteration regardless, so simply calling this
    before that draw is enough. Multiple resizes in quick succession (e.g.
    dragging a window's edge) coalesce into a single re-sync.
    """
    global _resize_pending
    if not _resize_pending:
        return False
    _resize_pending = False
    curses.endwin()
    stdscr.keypad(True)
    curses.raw()
    curses.curs_set(0)
    init_colors()
    stdscr.refresh()
    return True

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

def _popup_visible_rows(stdscr, lines: list, title: str) -> int:
    """How many content rows a popup for `lines`/`title` can show at once,
    given the current terminal size. Used by callers to keep a selection
    in view or to clamp manual scrolling."""
    h, _ = stdscr.getmaxyx()
    if not lines:
        lines = [("", "")]
    box_h = min(h - 2, len(lines) + 4)
    return max(1, box_h - 4)


def _draw_popup(stdscr, title: str, lines: list, selected_idx: int = None,
                 scroll_top: int = 0):
    """
    scroll_top: index into `lines` of the first row to display. Content
    beyond the visible area is not shown — the caller is responsible for
    keeping scroll_top pointed at whatever should be visible (see
    _popup_visible_rows). When content overflows, a small "a-b/N" counter
    is added to the title so it's never silently truncated with no sign
    there's more.
    """
    h, w = stdscr.getmaxyx()
    if not lines:
        lines = [("", "")]
    inner_w = max((len(str(l)) + len(str(r)) + 6) for l, r in lines) + 2
    box_w   = min(w - 4, max(inner_w, len(title) + 6))
    box_h   = min(h - 2, len(lines) + 4)
    by      = max(0, (h - box_h) // 2)
    bx      = max(0, (w - box_w) // 2)

    visible_rows = max(1, box_h - 4)
    max_scroll   = max(0, len(lines) - visible_rows)
    scroll_top   = max(0, min(scroll_top, max_scroll))
    window       = lines[scroll_top:scroll_top + visible_rows]

    try:
        win = curses.newwin(box_h, box_w, by, bx)
    except curses.error:
        return None   # terminal too small

    win.bkgd(" ", curses.color_pair(C_POPUP))
    win.box()

    disp_title = title
    if len(lines) > visible_rows:
        disp_title = (f"{title} "
                      f"[{scroll_top + 1}-{min(scroll_top + visible_rows, len(lines))}"
                      f"/{len(lines)}]")
    win.addstr(1, max(1, (box_w - len(disp_title)) // 2),
               disp_title[:box_w - 2],
               curses.color_pair(C_POPUP) | curses.A_BOLD)
    win.addstr(2, 1, "─" * (box_w - 2), curses.color_pair(C_PSEP))

    for i, (left, right) in enumerate(window):
        r = i + 3
        if r >= box_h - 1:
            break
        real_i = scroll_top + i
        left, right = str(left), str(right)
        if left.startswith("─"):
            win.addstr(r, 1, "─" * (box_w - 2),
                       curses.color_pair(C_PSEP) | curses.A_DIM)
        elif real_i == selected_idx:
            row_text = f"  {left:<10} {right}  "[:box_w - 2].ljust(box_w - 2)
            win.addstr(r, 1, row_text,
                       curses.color_pair(C_SELECT) | curses.A_BOLD)
        else:
            if left:
                win.addstr(r, 2, f"{left:<10}"[:box_w - 3],
                           curses.color_pair(C_POPUP) | curses.A_DIM)
                win.addstr(r, 13, right[:box_w - 15],
                           curses.color_pair(C_POPUP) | curses.A_BOLD)
            else:
                # No label column (common for log/output rows, e.g. ("", line))
                # — use the full row width instead of reserving 13 columns
                # for a label that isn't there.
                win.addstr(r, 2, right[:box_w - 3],
                           curses.color_pair(C_POPUP) | curses.A_BOLD)

    if len(lines) > visible_rows:
        hint = "\u2191\u2193 scroll"
        win.addstr(box_h - 1, max(1, box_w - len(hint) - 2), hint,
                   curses.color_pair(C_PSEP) | curses.A_DIM)

    win.refresh()
    return win


def _show_popup_blocking(stdscr, title: str, lines: list):
    """
    Draw a popup and block until the user closes it.

    Short content (fits on screen): any key closes it — identical to the
    old show_popup() behaviour, so existing plugins are unaffected.
    Overflowing content: \u2191/\u2193 (or W/S) and PageUp/PageDown scroll;
    any other key closes it.
    """
    scroll_top = 0
    while True:
        _check_resize(stdscr)
        visible_rows = _popup_visible_rows(stdscr, lines, title)
        max_scroll   = max(0, len(lines) - visible_rows)
        scroll_top   = max(0, min(scroll_top, max_scroll))
        _draw_popup(stdscr, title, lines, scroll_top=scroll_top)
        key = _safe_getch(stdscr)

        if max_scroll == 0:
            break
        if key in (curses.KEY_UP, ord("w"), ord("W")):
            scroll_top = max(0, scroll_top - 1)
        elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
            scroll_top = min(max_scroll, scroll_top + 1)
        elif key == curses.KEY_NPAGE:
            scroll_top = min(max_scroll, scroll_top + visible_rows)
        elif key == curses.KEY_PPAGE:
            scroll_top = max(0, scroll_top - visible_rows)
        else:
            break

    stdscr.touchwin()
    stdscr.refresh()

# ══════════════════════════════════════════════════════════════════════════════
#  CONTEXT MENU  (Ctrl+T)
# ══════════════════════════════════════════════════════════════════════════════

def show_context_menu(stdscr, api: PluginAPI):
    menu, key_map = api._build_menu()
    selectable    = [i for i, (l, _) in enumerate(menu) if not l.startswith("─")]
    sel_pos       = 0
    scroll_top    = 0
    title         = "  \u25c8 ACTIONS \u25c8  "

    while True:
        _check_resize(stdscr)
        real_idx = selectable[sel_pos]
        visible_rows = _popup_visible_rows(stdscr, menu, title)
        if real_idx < scroll_top:
            scroll_top = real_idx
        elif real_idx >= scroll_top + visible_rows:
            scroll_top = real_idx - visible_rows + 1
        _draw_popup(stdscr, title, menu, selected_idx=real_idx, scroll_top=scroll_top)
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
        _check_resize(stdscr)
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


_PTY_CHUNK   = 4096
_MAX_CAPTURE = 2 * 1024 * 1024   # 2MB cap — plenty for any real crash log,
                                   # bounds memory for a runaway/looping program


def _write_all(fd, data):
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def _pty_exec(cmd: list, cwd: str = None):
    """
    Run `cmd` under a pseudo-terminal (pty) instead of plain pipes or plain
    inheritance: the child gets a real tty (curses/color/interactive-prompt
    code all behave normally — isatty() is True, unlike a piped subprocess),
    its output is relayed to the real screen live AND captured at the same
    time, and keystrokes typed on the real terminal are forwarded straight
    into the child — including Ctrl+C, which reaches it as a real SIGINT
    because the pty's own line discipline (not us) generates the signal.

    Must be called with curses already suspended — see _run_interactive(),
    which wraps this with the curses handover.

    Returns (exit_code, was_interrupted, captured_output). exit_code follows
    shell convention for signal deaths (128 + signal number).
    Propagates FileNotFoundError if cmd[0] isn't runnable, checked before
    anything is spawned.
    """
    exe = cmd[0]
    if os.sep in exe or (os.altsep and os.altsep in exe):
        runnable = os.path.isfile(exe) and os.access(exe, os.X_OK)
    else:
        runnable = shutil.which(exe) is not None
    if not runnable:
        raise FileNotFoundError(f"[Errno 2] No such file or directory: {exe!r}")

    stdin_fd = sys.stdin.fileno()
    try:
        old_tattr = termios.tcgetattr(stdin_fd)
    except (termios.error, ValueError, OSError):
        old_tattr = None

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: pty.fork() already made the slave its controlling
        # terminal and dup'd it onto 0/1/2. Just exec.
        try:
            if cwd:
                os.chdir(cwd)
            os.execvp(exe, cmd)
        except Exception:
            os._exit(127)
        os._exit(127)   # unreachable, safety net

    # Parent: read our own real stdin raw so control characters (Ctrl+C
    # included) pass through as literal bytes instead of being interpreted
    # by our terminal — the *child's* pty (in normal cooked mode) is what
    # should interpret them.
    if old_tattr is not None:
        try:
            tty.setraw(stdin_fd)
        except termios.error:
            pass

    def _resize():
        try:
            cols, rows = shutil.get_terminal_size()
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    _resize()
    old_winch = None
    if hasattr(signal, "SIGWINCH"):
        old_winch = signal.signal(signal.SIGWINCH, lambda *_a: _resize())

    capture     = bytearray()
    truncated   = False
    interrupted = False
    watch_stdin = True

    try:
        while True:
            fds = [master_fd] + ([stdin_fd] if watch_stdin else [])
            try:
                ready, _, _ = select.select(fds, [], [])
            except InterruptedError:
                continue
            except (OSError, ValueError):
                break

            if master_fd in ready:
                try:
                    data = os.read(master_fd, _PTY_CHUNK)
                except OSError:
                    data = b""
                if not data:
                    break   # child closed its side — it's exiting
                if not truncated:
                    room = _MAX_CAPTURE - len(capture)
                    if room > 0:
                        capture.extend(data[:room])
                    if len(capture) >= _MAX_CAPTURE:
                        truncated = True
                try:
                    _write_all(1, data)
                except OSError:
                    pass

            if watch_stdin and stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, _PTY_CHUNK)
                except OSError:
                    data = b""
                if not data:
                    watch_stdin = False   # our stdin closed — stop polling it
                else:
                    if b"\x03" in data:
                        interrupted = True
                    try:
                        _write_all(master_fd, data)
                    except OSError:
                        pass
    finally:
        if old_winch is not None:
            signal.signal(signal.SIGWINCH, old_winch)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if old_tattr is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tattr)
            except termios.error:
                pass

    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = None

    if status is None:
        exit_code = None
    elif os.WIFEXITED(status):
        exit_code = os.WEXITSTATUS(status)
    elif os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        exit_code = 128 + sig
        if sig == signal.SIGINT:
            interrupted = True
    else:
        exit_code = None

    text = capture.decode("utf-8", "replace")
    if truncated:
        text += f"\n\u2026 [output truncated at {_MAX_CAPTURE // (1024 * 1024)}MB]\n"

    return exit_code, interrupted, text


def _run_interactive(state: NavState, cmd: list, cwd: str = None):
    """
    Suspend curses and hand the real terminal to `cmd` via a pseudo-terminal
    (see _pty_exec()) — same handover pattern as open_editor(), but for an
    arbitrary command and with output captured as it happens instead of
    lost to the live screen.

    Returns (exit_code, was_interrupted, captured_output).
    Propagates FileNotFoundError if cmd[0] isn't runnable, same as calling
    subprocess directly — callers handle it like open_editor's
    missing-editor case.
    """
    curses.endwin()
    try:
        exit_code, interrupted, output = _pty_exec(cmd, cwd)
    except BaseException:
        state.stdscr.keypad(True)
        curses.raw()
        init_colors()
        state.stdscr.refresh()
        raise

    note = "force-killed with Ctrl+C \u2014 " if interrupted else ""
    print(f"\n[navir] {note}press Enter to return...")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass

    state.stdscr.keypad(True)
    curses.raw()
    init_colors()
    state.stdscr.refresh()

    return exit_code, interrupted, output


CapturedResult = collections.namedtuple(
    "CapturedResult", ["returncode", "stdout", "stderr", "timed_out"]
)


def _run_captured(cmd: list, cwd: str = None, timeout: float = None,
                   input: str = None) -> CapturedResult:
    """
    Run `cmd` non-interactively and capture its output — the sane-defaults
    subprocess wrapper plugins were each hand-rolling (stdin closed so it
    can't hang waiting for input, text mode, optional timeout).

    input: text to feed the process's stdin (e.g. piping into a clipboard
           tool). If omitted, stdin is closed (DEVNULL) so nothing can
           block waiting to read from it.

    Returns a CapturedResult(returncode, stdout, stderr, timed_out).
    On timeout, returncode is None, timed_out is True, and stdout/stderr
    hold whatever the process had produced before it was killed.

    Propagates FileNotFoundError if cmd[0] isn't runnable, same as calling
    subprocess directly.
    """
    kwargs = dict(cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if input is not None:
        kwargs["input"] = input
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    try:
        result = subprocess.run(cmd, **kwargs)
        return CapturedResult(result.returncode, result.stdout, result.stderr, False)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "ignore")
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "ignore")
        return CapturedResult(None, out or "", err or "", True)


_which_cache: dict = {}


def _cached_which(exe: str):
    """shutil.which(), memoized — PATH doesn't change mid-session, and
    plugins checking tool availability shouldn't each keep their own cache."""
    if exe not in _which_cache:
        _which_cache[exe] = shutil.which(exe)
    return _which_cache[exe]


# System clipboard tools, tried in order — covers Termux, X11, Wayland, macOS.
_CLIPBOARD_TOOLS = [
    ("termux-clipboard-set", []),
    ("xclip",   ["-selection", "clipboard"]),
    ("xsel",    ["--clipboard", "--input"]),
    ("wl-copy", []),
    ("pbcopy",  []),
]


def _copy_to_system_clipboard(text: str):
    """
    Try each known clipboard tool in turn, piping `text` into its stdin.
    Returns (success: bool, tool_name: str | None).
    """
    for exe, args in _CLIPBOARD_TOOLS:
        if not _cached_which(exe):
            continue
        try:
            r = _run_captured([exe, *args], input=text, timeout=5)
        except FileNotFoundError:
            continue
        if not r.timed_out and r.returncode == 0:
            return True, exe
    return False, None


def _wrap_text(text: str, width: int) -> list:
    """Hard-wrap `text` into a flat list of display lines, each up to
    `width` chars. Blank lines are preserved. Simple width-based wrap
    (no word-breaking smarts) — good enough for logs and compiler output,
    which are usually already line-oriented."""
    width = max(10, width)
    out = []
    for raw_line in text.split("\n"):
        if raw_line == "":
            out.append("")
            continue
        while len(raw_line) > width:
            out.append(raw_line[:width])
            raw_line = raw_line[width:]
        out.append(raw_line)
    return out


def _draw_log_viewer(stdscr, title: str, wrapped_lines: list, scroll_top: int,
                      flash: str = None):
    """Full-width/height scrollable text viewer — for content that's better
    read as a document than a two-column popup (crash logs, long output)."""
    h, w = stdscr.getmaxyx()
    box_h, box_w = max(5, h - 2), max(20, w - 2)
    visible_rows = max(1, box_h - 3)

    try:
        win = curses.newwin(box_h, box_w, 1, 1)
    except curses.error:
        return

    win.bkgd(" ", curses.color_pair(C_POPUP))
    win.box()

    disp_title = f" {title} "
    if len(wrapped_lines) > visible_rows:
        end = min(scroll_top + visible_rows, len(wrapped_lines))
        disp_title = f" {title} [{scroll_top + 1}-{end}/{len(wrapped_lines)}] "
    win.addstr(0, max(1, (box_w - len(disp_title)) // 2),
               disp_title[:box_w - 2], curses.color_pair(C_POPUP) | curses.A_BOLD)

    for i, line in enumerate(wrapped_lines[scroll_top:scroll_top + visible_rows]):
        win.addstr(1 + i, 2, line[:box_w - 3], curses.color_pair(C_POPUP))

    footer = flash if flash else "\u2191\u2193/PgUp/PgDn scroll   C copy   Esc/Enter/Q close"
    win.addstr(box_h - 2, 2, footer[:box_w - 3],
               curses.color_pair(C_PSEP) | curses.A_DIM)

    win.refresh()


def _show_log_blocking(stdscr, title: str, text: str):
    """
    Block until closed, showing `text` in a full-width scrollable viewer.
    \u2191/\u2193 (or W/S) and PageUp/PageDown scroll. C copies the full
    (unwrapped) text to the system clipboard. Esc, Enter, or Q closes.
    """
    scroll_top = 0
    flash      = None
    while True:
        _check_resize(stdscr)
        h, w = stdscr.getmaxyx()
        content_width = max(10, w - 2 - 3)
        wrapped       = _wrap_text(text, content_width)
        visible_rows  = max(1, (h - 2) - 3)
        max_scroll    = max(0, len(wrapped) - visible_rows)
        scroll_top    = max(0, min(scroll_top, max_scroll))

        _draw_log_viewer(stdscr, title, wrapped, scroll_top, flash)
        flash = None
        key = _safe_getch(stdscr)

        if key in (curses.KEY_UP, ord("w"), ord("W")):
            scroll_top = max(0, scroll_top - 1)
        elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
            scroll_top = min(max_scroll, scroll_top + 1)
        elif key == curses.KEY_NPAGE:
            scroll_top = min(max_scroll, scroll_top + visible_rows)
        elif key == curses.KEY_PPAGE:
            scroll_top = max(0, scroll_top - visible_rows)
        elif key in (ord("c"), ord("C")):
            ok, tool = _copy_to_system_clipboard(text)
            flash = f"Copied via {tool}" if ok else "Copy failed \u2014 no clipboard tool found (tried termux-clipboard-set/xclip/xsel/wl-copy/pbcopy)"
        elif key in (27, 10, curses.KEY_ENTER, ord("q"), ord("Q")):
            break

    stdscr.touchwin()
    stdscr.refresh()

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

    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, _on_winch)

    state        = NavState()
    state.stdscr = stdscr

    api    = PluginAPI(state)
    loader = PluginLoader(api)
    loader.load_all()

    stdscr.erase()
    stdscr.refresh()
    _show_popup_blocking(stdscr, "  ◈ NAVIGATOR ◈  ", loader.startup_report())

    while True:
        stdscr = state.stdscr   # may be refreshed by open_editor
        _check_resize(stdscr)

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
