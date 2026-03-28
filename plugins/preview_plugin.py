"""
preview_plugin.py — File preview for Hacker File Navigator
────────────────────────────────────────────────────────────
Ctrl+P  →  preview selected file in a popup
           • text files: shows first 30 lines
           • images:     shows dimensions + file size
           • others:     shows file type info + size

Status bar hook: shows MIME type hint next to hovered file.

Drop into ~/.navigator/plugins/ to activate.
"""

import os
import struct
import mimetypes

NAME        = "preview_plugin"
VERSION     = "1.0"
DESCRIPTION = "Preview files with Ctrl+P  (text / image metadata)"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mime(filepath):
    mime, _ = mimetypes.guess_type(filepath)
    return mime or "application/octet-stream"


def _png_dimensions(filepath):
    try:
        with open(filepath, "rb") as f:
            f.seek(16)
            w, h = struct.unpack(">II", f.read(8))
            return w, h
    except Exception:
        return None, None


def _jpeg_dimensions(filepath):
    try:
        with open(filepath, "rb") as f:
            f.read(2)  # SOI marker
            while True:
                marker, = struct.unpack(">H", f.read(2))
                length, = struct.unpack(">H", f.read(2))
                if marker in (0xFFC0, 0xFFC2):
                    f.read(1)
                    h, w = struct.unpack(">HH", f.read(4))
                    return w, h
                f.read(length - 2)
    except Exception:
        return None, None


def _image_info(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".png":
        w, h = _png_dimensions(filepath)
    elif ext in (".jpg", ".jpeg"):
        w, h = _jpeg_dimensions(filepath)
    else:
        w, h = None, None
    return w, h


def _human_size(n):
    if n >= 1_048_576: return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:      return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ── Status bar hint ──────────────────────────────────────────────────────────

def status_hint(api, path, item):
    if item.endswith("/"):
        return None
    full = os.path.join(path, item)
    if not os.path.isfile(full):
        return None
    mime = _mime(full)
    return f"type: {mime}"


# ── Keybind Ctrl+P: preview popup ────────────────────────────────────────────

def on_ctrl_p(api, path, selected_item):
    if not selected_item or selected_item.endswith("/"):
        api.show_status("Select a file to preview.", is_error=True)
        return

    full = os.path.join(path, selected_item)
    if not os.path.isfile(full):
        api.show_status("ERROR: Not a file.", is_error=True)
        return

    size = os.path.getsize(full)
    mime = _mime(full)
    lines = [("Size", _human_size(size)), ("Type", mime)]

    # ── Image preview ──
    if mime and mime.startswith("image/"):
        w, h = _image_info(full)
        if w and h:
            lines.append(("Dims", f"{w} × {h} px"))
        lines.append(("──────", "──────────────────────────"))
        lines.append(("", "(binary — no text preview)"))
        api.show_popup(f"  ◈ PREVIEW: {selected_item[:30]} ◈  ", lines)
        return

    # ── Text preview ──
    if mime and (mime.startswith("text/") or mime in (
            "application/json", "application/xml",
            "application/javascript", "application/x-sh")):
        lines.append(("──────", "──────────────────────────"))
        try:
            with open(full, "r", errors="replace") as f:
                content_lines = f.readlines()
            for i, ln in enumerate(content_lines[:28]):
                lines.append((f"{i+1:>3}", ln.rstrip("\n")[:60]))
            if len(content_lines) > 28:
                lines.append(("…", f"({len(content_lines) - 28} more lines)"))
        except Exception as e:
            lines.append(("ERR", str(e)))
        api.show_popup(f"  ◈ PREVIEW: {selected_item[:30]} ◈  ", lines)
        return

    # ── Binary / unknown ──
    lines.append(("──────", "──────────────────────────"))
    lines.append(("", "(binary file — no preview)"))
    api.show_popup(f"  ◈ PREVIEW: {selected_item[:30]} ◈  ", lines)


# ── Register ─────────────────────────────────────────────────────────────────

def register(api):
    api.on_status(status_hint)
    api.add_keybind("Ctrl+P", "Preview File", on_ctrl_p)
