"""
git_plugin.py — Git integration for Hacker File Navigator
──────────────────────────────────────────────────────────
Ctrl+G  →  show git status popup for current folder
Status bar shows current branch name.
Hovered file shows [git:M] / [git:?] etc tag.

BUG 4 FIX: git subprocess results are cached per (path, mtime)
so we don't shell out on every single keypress.
"""

import os
import subprocess

NAME        = "git_plugin"
VERSION     = "1.1"
DESCRIPTION = "Git status tags + branch in status bar  (Ctrl+G)"

# ── Cache: avoid running git on every draw frame (BUG 4 FIX) ─────────────────
_cache = {
    "path":       None,
    "mtime":      None,
    "is_repo":    False,
    "branch":     "",
    "status_map": {},
}

def _cache_key(path):
    try:
        return os.stat(path).st_mtime
    except Exception:
        return None

def _refresh_cache(path):
    mtime = _cache_key(path)
    if path == _cache["path"] and mtime == _cache["mtime"]:
        return   # still fresh

    _cache["path"]   = path
    _cache["mtime"]  = mtime

    ok, _, _ = _run_git(["git", "rev-parse", "--is-inside-work-tree"], path)
    _cache["is_repo"] = ok
    if not ok:
        _cache["branch"]     = ""
        _cache["status_map"] = {}
        return

    ok2, branch, _ = _run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    _cache["branch"] = branch if ok2 else ""

    ok3, out, _ = _run_git(["git", "status", "--porcelain"], path)
    smap = {}
    if ok3 and out:
        for line in out.splitlines():
            if len(line) >= 3:
                code = line[:2].strip()
                name = line[3:].split(" -> ")[-1].strip().rstrip("/")
                smap[os.path.basename(name)] = code
    _cache["status_map"] = smap

def _run_git(cmd, cwd):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=2)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception:
        return False, "", ""

def _status_label(code):
    if "M" in code: return "M"
    if "A" in code: return "A"
    if "D" in code: return "D"
    if "R" in code: return "R"
    if "?" in code: return "?"
    return code[:1]

# ── Hooks ─────────────────────────────────────────────────────────────────────

def hover_tag(api, path, item):
    _refresh_cache(path)
    if not _cache["is_repo"]:
        return None
    code = _cache["status_map"].get(item.rstrip("/"))
    return f"git:{_status_label(code)}" if code else None

def status_bar(api, path, item):
    _refresh_cache(path)
    if not _cache["is_repo"] or not _cache["branch"]:
        return None
    return f"⎇ {_cache['branch']}"

def on_ctrl_g(api, path, selected_item):
    _refresh_cache(path)
    if not _cache["is_repo"]:
        api.show_status("Not a git repo.", is_error=True)
        return

    ok, out, err = _run_git(["git", "status", "--short", "--branch"], path)
    if not ok:
        api.show_status(f"git error: {err}", is_error=True)
        return

    lines = []
    for line in (out.splitlines() or ["(clean)"]):
        code  = line[:2].strip() if len(line) > 2 else ""
        fname = line[3:] if len(line) > 3 else line
        lines.append((code or "·", fname))

    api.show_popup("  ◈ GIT STATUS ◈  ", lines)

def register(api):
    api.on_file_hover(hover_tag)
    api.on_status(status_bar)
    api.add_keybind("Ctrl+G", "Git Status", on_ctrl_g)
