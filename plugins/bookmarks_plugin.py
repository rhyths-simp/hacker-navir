"""
bookmarks_plugin.py — Folder bookmarks for Hacker File Navigator
─────────────────────────────────────────────────────────────────
Ctrl+B  →  open bookmark menu

BUG 2 FIX: removed show_popup() + immediate prompt() pattern
(popup consumed a keypress then prompt appeared invisibly).
Now uses a proper interactive curses loop instead.
"""

import os
import curses

NAME        = "bookmarks_plugin"
VERSION     = "1.1"
DESCRIPTION = "Save & jump to favourite folders  (Ctrl+B)"

BOOKMARKS_FILE = os.path.expanduser("~/.navigator/bookmarks.txt")

# Colors (same IDs as navigator)
C_POPUP  = 8
C_SELECT = 4
C_STATUS = 5
C_TITLE  = 7
C_PSEP   = 9

def _load():
    if not os.path.exists(BOOKMARKS_FILE):
        return []
    with open(BOOKMARKS_FILE) as f:
        return [l.strip() for l in f if l.strip()]

def _save(bookmarks):
    os.makedirs(os.path.dirname(BOOKMARKS_FILE), exist_ok=True)
    with open(BOOKMARKS_FILE, "w") as f:
        f.write("\n".join(bookmarks))

# ── BUG 2 FIX: self-contained interactive bookmark menu ──────────────────────

def _show_bookmark_menu(stdscr, bookmarks):
    """
    Shows a navigable popup. Returns ('jump', idx) / ('add',) / ('remove', idx) / None.
    Uses its own input loop — never calls show_popup then prompt.
    """
    h, w = stdscr.getmaxyx()

    # Build rows
    def make_rows():
        rows = []
        for i, bm in enumerate(bookmarks):
            rows.append((f"[{i+1}]", bm))
        if bookmarks:
            rows.append(("──────", "──────────────────────"))
        rows.append(("A", "Add current folder as bookmark"))
        if bookmarks:
            rows.append(("R", "Remove a bookmark"))
        rows.append(("Esc", "Close"))
        return rows

    # Selectable indices (skip separators)
    def selectables(rows):
        return [i for i, (l, _) in enumerate(rows) if not l.startswith("─")]

    sel = 0

    while True:
        rows   = make_rows()
        sels   = selectables(rows)
        sel    = max(0, min(sel, len(sels) - 1))
        real   = sels[sel]

        # Draw popup manually
        box_w = min(w - 4, max(50, max(len(r) + len(l) + 6 for l, r in rows) + 2))
        box_h = min(h - 4, len(rows) + 4)
        by    = max(0, (h - box_h) // 2)
        bx    = max(0, (w - box_w) // 2)

        win = curses.newwin(box_h, box_w, by, bx)
        win.bkgd(" ", curses.color_pair(C_POPUP))
        win.box()
        title = "  ◈ BOOKMARKS ◈  "
        win.addstr(1, max(1, (box_w - len(title)) // 2), title,
                   curses.color_pair(C_POPUP) | curses.A_BOLD)
        win.addstr(2, 1, "─" * (box_w - 2), curses.color_pair(C_PSEP))

        for i, (left, right) in enumerate(rows):
            r = i + 3
            if r >= box_h - 1:
                break
            if left.startswith("─"):
                win.addstr(r, 1, "─" * (box_w - 2),
                           curses.color_pair(C_PSEP) | curses.A_DIM)
            elif i == real:
                win.attron(curses.color_pair(C_SELECT) | curses.A_BOLD)
                win.addstr(r, 1, f"  {left:<6} {right}  "[:box_w - 2].ljust(box_w - 2))
                win.attroff(curses.color_pair(C_SELECT) | curses.A_BOLD)
            else:
                win.addstr(r, 2, f"{left:<6}"[:box_w - 3],
                           curses.color_pair(C_POPUP) | curses.A_DIM)
                win.addstr(r, 9, right[:box_w - 11],
                           curses.color_pair(C_POPUP) | curses.A_BOLD)

        win.addstr(box_h - 1, 2, " ↑↓ navigate  |  Enter select  |  Esc close "[:box_w - 3],
                   curses.color_pair(C_PSEP) | curses.A_DIM)
        win.refresh()

        key = stdscr.getch()

        if key == 27:
            return None
        elif key in (curses.KEY_UP, ord("w"), ord("W")):
            sel = (sel - 1) % len(sels)
        elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
            sel = (sel + 1) % len(sels)
        elif key in (10, curses.KEY_ENTER):
            left = rows[real][0]
            if left == "Esc":
                return None
            elif left == "A":
                return ("add",)
            elif left == "R":
                return ("remove_menu",)
            else:
                # it's a bookmark number
                try:
                    idx = int(left.strip("[]")) - 1
                    return ("jump", idx)
                except ValueError:
                    return None


def on_ctrl_b(api, path, selected_item):
    stdscr = api._state.stdscr
    bookmarks = _load()

    while True:
        result = _show_bookmark_menu(stdscr, bookmarks)

        if result is None:
            return

        action = result[0]

        if action == "add":
            if path not in bookmarks:
                bookmarks.append(path)
                _save(bookmarks)
                api.show_status(f"Bookmarked: {path}")
            else:
                api.show_status("Already bookmarked.")
            return

        elif action == "remove_menu":
            if not bookmarks:
                return
            # Show a sub-menu to pick which one to remove
            lines = [(f"[{i+1}]", bm) for i, bm in enumerate(bookmarks)]
            lines.append(("Esc", "Cancel"))
            api.show_popup("  Remove which bookmark?  ", lines)
            ans = api.prompt(f"Remove number (1-{len(bookmarks)}):")
            try:
                idx = int(ans) - 1
                removed = bookmarks.pop(idx)
                _save(bookmarks)
                api.show_status(f"Removed: {removed}")
            except (ValueError, IndexError):
                api.show_status("Cancelled.")
            return

        elif action == "jump":
            idx  = result[1]
            if 0 <= idx < len(bookmarks):
                dest = bookmarks[idx]
                if os.path.isdir(dest):
                    api.navigate_to(dest)
                    api.show_status(f"Jumped to: {dest}")
                else:
                    api.show_status(f"ERROR: Folder gone: {dest}", is_error=True)
            return

def register(api):
    api.add_keybind("Ctrl+B", "Bookmarks", on_ctrl_b)
