"""
bookmarks_plugin.py — Folder bookmarks for Hacker File Navigator
─────────────────────────────────────────────────────────────────
Ctrl+B  →  open bookmark menu

Rewritten to use api.show_menu() — no more raw curses / private
state access. The old version reached into api._state.stdscr and
redrew the popup by hand because there was no API for an interactive,
navigable menu. There is now.
"""

import os

NAME        = "bookmarks_plugin"
VERSION     = "1.2"
DESCRIPTION = "Save & jump to favourite folders  (Ctrl+B)"

BOOKMARKS_FILE = os.path.expanduser("~/.navigator/bookmarks.txt")


def _load():
    if not os.path.exists(BOOKMARKS_FILE):
        return []
    with open(BOOKMARKS_FILE) as f:
        return [l.strip() for l in f if l.strip()]


def _save(bookmarks):
    os.makedirs(os.path.dirname(BOOKMARKS_FILE), exist_ok=True)
    with open(BOOKMARKS_FILE, "w") as f:
        f.write("\n".join(bookmarks))


def _build_rows(bookmarks):
    rows = [(f"[{i+1}]", bm) for i, bm in enumerate(bookmarks)]
    if bookmarks:
        rows.append(("──────", "──────────────────────"))
    rows.append(("A", "Add current folder as bookmark"))
    if bookmarks:
        rows.append(("R", "Remove a bookmark"))
    rows.append(("Esc", "Close"))
    return rows


def on_ctrl_b(api, path, selected_item):
    bookmarks = _load()

    while True:
        rows   = _build_rows(bookmarks)
        choice = api.show_menu("  ◈ BOOKMARKS ◈  ", rows)
        if choice is None:
            return

        label = rows[choice][0]

        if label == "A":
            if path not in bookmarks:
                bookmarks.append(path)
                _save(bookmarks)
                api.show_status(f"Bookmarked: {path}")
            else:
                api.show_status("Already bookmarked.")
            return

        elif label == "R":
            remove_rows = [(f"[{i+1}]", bm) for i, bm in enumerate(bookmarks)]
            remove_rows.append(("Esc", "Cancel"))
            picked = api.show_menu("  Remove which bookmark?  ", remove_rows)
            if picked is not None and remove_rows[picked][0] != "Esc":
                removed = bookmarks.pop(picked)
                _save(bookmarks)
                api.show_status(f"Removed: {removed}")
            else:
                api.show_status("Cancelled.")
            return

        elif label == "Esc":
            return

        else:
            # it's a bookmark number — jump to it
            idx  = int(label.strip("[]")) - 1
            dest = bookmarks[idx]
            if os.path.isdir(dest):
                api.navigate_to(dest)
                api.show_status(f"Jumped to: {dest}")
            else:
                api.show_status(f"ERROR: Folder gone: {dest}", is_error=True)
            return


def register(api):
    api.add_keybind("Ctrl+B", "Bookmarks", on_ctrl_b)
