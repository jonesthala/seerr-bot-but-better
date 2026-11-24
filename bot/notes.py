"""To-do page of the admin panel: checklists and ideas from Markdown notes, e.g. an Obsidian vault.

Only reads. Items with a checkbox ("- [ ]", "- [x]") are tasks; other list items are ideas or notes. "✅" in front of
an item or a section like "## ✅ Umgesetzt" counts as done, "⭐" in front as a favourite. Indented items belong to the
item above. Obsidian links become plain words.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

MAX_NOTE_BYTES = 1_000_000
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM_RE = re.compile(r"^(\s*)[-*+]\s+(?:\[([ xX])\]\s+)?(.*)$")
_WIKI_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_MDLINK_RE = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")
_DONE_SECTION_RE = re.compile(r"✅|umgesetzt|erledigt|\bdone\b", re.IGNORECASE)
_IMPORTANT_RE = re.compile(r"🔴|wichtig|important", re.IGNORECASE)
_MARKERS = ("✅", "⭐")


class NotesError(Exception):
    """A message for the panel as it is."""


def note_text(text: str) -> str:
    """[[Discord-Bot]] → Discord-Bot, [[media|Seerr]] → Seerr, [[Ordner/Notiz#Teil]] → Notiz, [x](url) → x."""
    text = _WIKI_RE.sub(lambda m: m.group(2) or m.group(1).split("/")[-1].split("#")[0], text)
    return _MDLINK_RE.sub(r"\1", text).strip()


def _strip_markers(body: str) -> tuple[str, bool, bool]:
    done = star = False
    while body.startswith(_MARKERS):
        done = done or body.startswith("✅")
        star = star or body.startswith("⭐")
        body = body[1:].lstrip("️").strip()
    return body, done, star


def parse_note(text: str) -> dict[str, Any]:
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[0].strip() == "---":  # front matter
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
        lines = lines[end + 1:]
    title = ""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_code = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            name = note_text(heading.group(2))
            if len(heading.group(1)) == 1 and not title:
                title = name
            else:
                current = {"title": name, "done": bool(_DONE_SECTION_RE.search(name)),
                           "important": bool(_IMPORTANT_RE.search(name)), "items": []}
                sections.append(current)
            continue
        item = _ITEM_RE.match(line)
        if not item or not item.group(3).strip():
            continue
        body, done, star = _strip_markers(item.group(3).strip())
        entry = {"text": note_text(body), "task": item.group(2) is not None,
                 "done": done or item.group(2) in ("x", "X"), "star": star, "children": []}
        if current is None:
            current = {"title": "", "done": False, "important": False, "items": []}
            sections.append(current)
        if len(item.group(1).expandtabs(4)) >= 2 and current["items"]:
            current["items"][-1]["children"].append(entry)
        else:
            current["items"].append(entry)
    sections = [s for s in sections if s["items"]]
    counts = {"open": 0, "done": 0, "ideas": 0, "important": 0}
    for section in sections:
        for entry in section["items"]:
            for one in [entry] + entry["children"]:
                finished = one["done"] or section["done"]
                if one["task"]:
                    counts["open" if not finished else "done"] += 1
                    counts["important"] += section["important"] and not finished
                elif one is entry and not finished:
                    counts["ideas"] += 1
    return {"title": title, "sections": sections, **counts}


def obsidian_link(vault: str, path: str) -> str | None:
    """A link that opens the note in the Obsidian app; None without a vault name."""
    if not vault:
        return None
    name = path[:-3] if path.endswith(".md") else path
    return f"obsidian://open?vault={quote(vault, safe='')}&file={quote(name, safe='')}"


def read_notes(folder: str, files: tuple[str, ...], vault: str = "") -> dict[str, Any]:
    """The configured notes, parsed, plus when the folder was last fetched if it is a git checkout."""
    root = Path(folder)
    if not root.is_dir():
        raise NotesError("Die Kopie der Notizen fehlt noch (PANEL_TODO_DIR).")
    base = root.resolve()
    notes = []
    for name in files:
        path = (root / name).resolve()
        if base not in path.parents:
            continue  # only inside the folder
        entry: dict[str, Any] = {"path": name, "obsidian": obsidian_link(vault, name), "missing": False}
        try:
            if path.stat().st_size > MAX_NOTE_BYTES:
                raise OSError("too large")
            entry.update(parse_note(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            entry.update({"title": "", "sections": [], "open": 0, "done": 0, "ideas": 0, "important": 0,
                          "missing": True})
        entry["title"] = entry["title"] or Path(name).stem
        notes.append(entry)
    fetched = None
    for marker in (root / ".git" / "FETCH_HEAD", root / ".git" / "HEAD"):
        if marker.is_file():
            fetched = marker.stat().st_mtime
            break
    return {"notes": notes, "fetched": fetched}
