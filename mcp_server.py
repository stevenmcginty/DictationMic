"""
mcp_server — DictationMic's notes, served over MCP (stdio).

Gives Claude Code (or any MCP client) eyes on the notes\\ folder: list,
search and read notes, grab the one you just dictated, and drop new notes
in. Everything goes through plain .txt files — the pill's own scan() adopts
anything we create and syncs it to the phone, so this server never touches
the sync index (.sync-state.json is read purely for ids and stars) and can
run whether or not the pill is up.

Register it once with:  claude mcp add dictationmic -- \
    <this venv's python.exe> <path to this file>
…or just run Claude Code inside the DictationMic folder (.mcp.json).

The voice flow this enables: dictate into the pill (or phone), then tell
Claude "read my latest note and do what it says".
"""

import json
import os
import re
import time

from mcp.server.fastmcp import FastMCP

from notestore import note_title_from, sanitize_title

APP_DIR = os.path.dirname(os.path.abspath(__file__))
NOTES_DIR = os.path.join(APP_DIR, "notes")
INDEX_PATH = os.path.join(NOTES_DIR, ".sync-state.json")

mcp = FastMCP(
    "dictationmic",
    instructions=(
        "DictationMic notes: everything Steve dictates (desktop pill or "
        "phone) lands here as a note. latest_note is the freshest "
        "dictation; create_note pushes text to all his devices."),
)


def _index():
    """id/star metadata the pill maintains — read-only, never written."""
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("notes", {}) or {}
    except Exception:
        return {}


def _notes(include_files=True):
    """Newest-first snapshot of notes\\*.txt as dicts."""
    meta = {}
    for i, e in _index().items():
        if not e.get("deletedLocally"):
            meta[e.get("filename", "").lower()] = (i, e)
    out = []
    try:
        names = [n for n in os.listdir(NOTES_DIR) if n.lower().endswith(".txt")]
    except OSError:
        return []
    for name in names:
        path = os.path.join(NOTES_DIR, name)
        try:
            st = os.stat(path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
        except OSError:
            continue
        note_id, entry = meta.get(name.lower(), ("", {}))
        is_file = body.startswith("data:")
        if is_file and not include_files:
            continue
        out.append({
            "title": os.path.splitext(name)[0],
            "id": note_id,
            "body": body,
            "updated": st.st_mtime,
            "starred": bool(entry.get("starred")),
            "kind": "file" if is_file else "text",
            "file": entry.get("file", ""),
        })
    out.sort(key=lambda n: n["updated"], reverse=True)
    return out


def _stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _line(n):
    star = "★ " if n["starred"] else ""
    if n["kind"] == "file":
        what = f"[file: {n['file'] or 'attachment'}]"
    else:
        first = " ".join(n["body"].split())
        what = (first[:100] + "…") if len(first) > 100 else first
    return f"{star}{n['title']}  ({_stamp(n['updated'])})\n    {what}"


@mcp.tool()
def list_notes(limit: int = 20, starred_only: bool = False) -> str:
    """List notes, newest first, with a one-line preview each."""
    notes = [n for n in _notes() if n["starred"] or not starred_only]
    if not notes:
        return "No notes yet."
    lines = [_line(n) for n in notes[:max(1, limit)]]
    more = len(notes) - len(lines)
    if more > 0:
        lines.append(f"…and {more} more (raise limit or search).")
    return "\n".join(lines)


@mcp.tool()
def search_notes(query: str, limit: int = 10) -> str:
    """Find notes whose title or text contains the query
    (case-insensitive). Returns titles with a snippet around the match."""
    q = query.strip().lower()
    if not q:
        return "Give me something to search for."
    hits = []
    for n in _notes():
        hay = (n["title"] + "\n" + ("" if n["kind"] == "file" else n["body"]))
        at = hay.lower().find(q)
        if at < 0:
            continue
        snip = " ".join(hay[max(0, at - 60):at + len(q) + 60].split())
        hits.append(f"{'★ ' if n['starred'] else ''}{n['title']}  "
                    f"({_stamp(n['updated'])})\n    …{snip}…")
        if len(hits) >= max(1, limit):
            break
    return "\n".join(hits) or f"Nothing mentions “{query}”."


@mcp.tool()
def read_note(title: str) -> str:
    """Read a note's full text by its title (as shown by list/search;
    the .txt extension is optional)."""
    want = title.strip().lower().removesuffix(".txt")
    for n in _notes():
        if n["title"].lower() == want or (n["id"] and n["id"] == title.strip()):
            if n["kind"] == "file":
                real = n["file"] or "an attachment"
                return (f"“{n['title']}” is a file note ({real}) — the real "
                        f"file lives in notes\\files.")
            return f"# {n['title']}  ({_stamp(n['updated'])})\n\n{n['body']}"
    return f"No note called “{title}”. Try search_notes."


@mcp.tool()
def latest_note(max_age_minutes: int = 0) -> str:
    """The most recent text note — i.e. the thing Steve just dictated.
    max_age_minutes > 0 refuses anything older (0 = whatever is newest)."""
    for n in _notes(include_files=False):
        age = (time.time() - n["updated"]) / 60
        if max_age_minutes and age > max_age_minutes:
            return (f"Newest note “{n['title']}” is {age:.0f} min old — "
                    f"older than the {max_age_minutes} min you asked for.")
        return f"# {n['title']}  ({_stamp(n['updated'])})\n\n{n['body']}"
    return "No text notes yet."


@mcp.tool()
def create_note(text: str, title: str = "") -> str:
    """Save a new note. It appears in the pill's My Notes right away and —
    when sync is on — on the phone within moments. Plain .txt on disk; the
    running pill adopts and syncs it, nothing here touches the sync index."""
    body = (text or "").strip()
    if not body:
        return "The note would be empty — give me some text."
    name = sanitize_title(title) or note_title_from(body)
    os.makedirs(NOTES_DIR, exist_ok=True)
    filename, n = name + ".txt", 2
    while os.path.exists(os.path.join(NOTES_DIR, filename)):
        filename = f"{name} ({n}).txt"
        n += 1
    path = os.path.join(NOTES_DIR, filename)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)
    return f"Saved “{os.path.splitext(filename)[0]}”."


if __name__ == "__main__":
    mcp.run()
