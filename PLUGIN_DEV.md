# Plugin Development Guide
> Hacker File Navigator — complete reference for plugin authors

---

## Quick start

Create a `.py` file in `plugins/` (bundled) or `~/.navigator/plugins/` (user-installed):

```python
NAME        = "my_plugin"
VERSION     = "1.0"
DESCRIPTION = "One-line description shown at startup"

def register(api):
    api.add_keybind("Ctrl+E", "My Action", on_ctrl_e)

def on_ctrl_e(api, path, selected_item):
    api.show_status(f"Hello from {path}")
```

Restart the navigator. Your plugin appears in the startup report and in the `Ctrl+T` menu automatically.

---

## Plugin file structure

| Field         | Required | Description                          |
|---------------|----------|--------------------------------------|
| `NAME`        | no       | Display name (defaults to filename)  |
| `VERSION`     | no       | Shown in startup popup               |
| `DESCRIPTION` | no       | One-line summary shown at startup    |
| `register(api)` | **yes** | Called once at load — wire hooks here |

---

## Full API reference

### `api.add_keybind(key_str, label, callback)`

Register a keyboard shortcut and add it to the `Ctrl+T` context menu.

```python
api.add_keybind("Ctrl+E", "Open in Editor", my_callback)
# callback signature:
def my_callback(api, path, selected_item):
    ...
```

**`path`** — absolute path of the current directory  
**`selected_item`** — filename with trailing `/` for dirs, or `None` if on a placeholder row

**Reserved keys (core):** `C  D  F  N  R  T  V  X`  
**Safe to use:** `B  E  G  H  I  J  K  L  O  P  U  Y  Z`  
**Avoid:** `W  S  Q` — used for navigation (up / down / quit), even without Ctrl

---

### `api.on_file_hover(fn)`

Show a short tag next to the selected filename on every draw frame.

```python
def my_hover(api, path, item):
    if item.endswith(".py"):
        return "py"    # shown as  [py]  next to the filename
    return None        # show nothing

api.on_file_hover(my_hover)
```

**Return:** string up to 8 characters, or `None`  
**⚠ Keep this fast** — it runs every frame. Cache any slow I/O.

---

### `api.on_file_open(fn)`

Intercept file opens. Return `True` to handle it yourself and skip the default editor.

```python
import os, shutil

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

def open_image(api, path, item):
    ext = os.path.splitext(item)[1].lower()
    if ext in IMAGE_EXTS and shutil.which("feh"):
        full = os.path.join(path, item)
        os.system(f"feh {full!r} &")
        return True   # handled — skip default editor
    return False      # fall through to default

api.on_file_open(open_image)
```

---

### `api.on_status(fn)`

Add extra info to the status bar for the currently selected file.

```python
def show_mime(api, path, item):
    import mimetypes
    full = os.path.join(path, item.rstrip("/"))
    mime, _ = mimetypes.guess_type(full)
    return f"type: {mime}" if mime else None

api.on_status(show_mime)
```

**Return:** string or `None`  
**⚠ Keep this fast** — runs every frame.

---

### `api.on_startup(fn)`

Called once after all plugins have loaded. Use for initialisation.

```python
def on_startup():
    os.makedirs(MY_DATA_DIR, exist_ok=True)

api.on_startup(on_startup)
```

---

### Read state

```python
api.get_current_path()      # → str   current directory (absolute)
api.get_selected_item()     # → str | None   filename (trailing / for dirs)
api.get_clipboard()         # → (path, is_cut)  path is None if empty
```

---

### Write state

```python
api.set_clipboard(path, is_cut=False)   # put something on the clipboard
api.navigate_to(path)                   # cd to a different directory
api.show_status("message")              # yellow status bar (one frame)
api.show_status("oops", is_error=True)  # red error message
```

---

### UI helpers

```python
# Inline text input — returns what the user typed, or '' if cancelled
name = api.prompt("Enter file name:")

# Blocking popup — waits for any key to close
api.show_popup("My Plugin", [
    ("Key",    "Value"),
    ("──────", ""),        # separator line
    ("",       "Press any key..."),
])

# Force redraw (rarely needed)
api.refresh()
```

---

## Rules — what plugins must NOT do

Breaking these will crash or corrupt the navigator.

```python
# ✗ NEVER access NavState directly
api._state.path = "/tmp"     # don't do this

# ✗ NEVER call curses functions yourself
import curses
curses.endwin()              # don't do this

# ✗ NEVER call sys.exit() or os._exit()
sys.exit(0)                  # don't do this

# ✗ NEVER block the main thread for more than ~100ms
time.sleep(5)                # don't do this in a hook

# ✗ NEVER catch and swallow all exceptions silently in hooks
try:
    do_work()
except Exception:
    pass    # errors go unreported — use api.show_status() instead
```

---

## Debugging your plugin

When a plugin hook raises an exception, the navigator:
1. Silently recovers (never crashes the app)
2. Logs the full traceback to `~/.navigator/plugin_errors.log`

To debug:

```bash
# In a second terminal, watch the log live:
tail -f ~/.navigator/plugin_errors.log

# Or dump it after a session:
cat ~/.navigator/plugin_errors.log
```

For errors during `register()` itself (load-time failures), the startup
popup shows a `FAIL` line with a short message.

---

## Performance guidelines

| Hook           | Max time budget | Notes                            |
|----------------|-----------------|----------------------------------|
| `on_file_hover`| < 1 ms          | Runs every keypress              |
| `on_status`    | < 1 ms          | Runs every keypress              |
| `on_file_open` | any             | Only on Enter                    |
| keybind callback | any           | Only on keypress                 |
| `on_startup`   | any             | Once at startup                  |

For hover/status hooks that need slow I/O (git, network, disk):
**cache the result** keyed on `(path, mtime)`. See `git_plugin.py` for
a worked example.

---

## Worked example — word count in status bar

```python
NAME        = "wordcount"
VERSION     = "1.0"
DESCRIPTION = "Show word count for text files in status bar"

import os

TEXT_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml"}
_wc_cache  = {}   # (path, mtime) → count

def word_count(api, path, item):
    if item.endswith("/"):
        return None
    ext = os.path.splitext(item)[1].lower()
    if ext not in TEXT_EXTS:
        return None

    full = os.path.join(path, item)
    try:
        mtime = os.stat(full).st_mtime
        key   = (full, mtime)
        if key not in _wc_cache:
            with open(full, errors="ignore") as f:
                _wc_cache[key] = len(f.read().split())
        return f"{_wc_cache[key]:,} words"
    except Exception:
        return None

def register(api):
    api.on_status(word_count)
```

---

## Worked example — open images in feh

```python
NAME        = "image_viewer"
VERSION     = "1.0"
DESCRIPTION = "Open images in feh instead of editor"

import os, shutil

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}

def open_hook(api, path, item):
    ext = os.path.splitext(item)[1].lower()
    if ext not in IMAGE_EXTS:
        return False
    if not shutil.which("feh"):
        api.show_status("feh not installed.", is_error=True)
        return True   # still claim it so the editor doesn't open
    full = os.path.join(path, item)
    os.system(f"feh {shlex.quote(full)} &")
    return True

def register(api):
    api.on_file_open(open_hook)
```

---

## Plugin template

Copy this to get started:

```python
"""
your_plugin.py — Short description
────────────────────────────────────
Ctrl+?  →  what it does

Drop into ~/.navigator/plugins/ to install.
"""

import os

NAME        = "your_plugin"
VERSION     = "1.0"
DESCRIPTION = "Short description"


# ── Hooks ─────────────────────────────────────────────────────────────────────

def on_hover(api, path, item):
    """Return a tag string or None."""
    return None

def on_status(api, path, item):
    """Return extra status bar text or None."""
    return None

def on_open(api, path, item):
    """Return True if you handled the open, False to fall through."""
    return False

def on_my_key(api, path, selected_item):
    """Called when Ctrl+? is pressed."""
    api.show_status("Plugin fired!")


# ── Register ──────────────────────────────────────────────────────────────────

def register(api):
    # Remove the lines you don't need
    api.add_keybind("Ctrl+?", "My Action", on_my_key)
    api.on_file_hover(on_hover)
    api.on_status(on_status)
    api.on_file_open(on_open)
```

---

## Included plugins

| File                    | Key    | What it does                                |
|-------------------------|--------|---------------------------------------------|
| `git_plugin.py`         | Ctrl+G | Git status popup + branch in status bar     |
| `bookmarks_plugin.py`   | Ctrl+B | Save/jump to favourite folders              |
| `preview_plugin.py`     | Ctrl+P | Preview text files & image metadata         |
