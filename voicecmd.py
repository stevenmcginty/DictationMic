"""Voice commands ("hot words") for DictationMic.

If a WHOLE spoken phrase matches one of the patterns in commands.json,
the pill runs a task — open a terminal, start Claude Code, open a file
or web page — instead of typing the words. A phrase spoken as part of a
longer dictation never fires: the utterance has to be exactly the
command, so ordinary typing is safe.

Matching is forgiving: casing and punctuation are ignored, a leading or
trailing "please" is allowed, and common mis-hearings of "claude"
("clawed", "cloud", "clod"...) are folded to "claude" before matching.
The spoken text itself is never rewritten — if nothing matches, what
you said is typed exactly as transcribed.

A pattern may contain one {folder} wildcard. The captured words are
matched against the real folders inside the command's "dir" — spoken
"folder one" finds "folder1", "Folder One" or "folder-one".

commands.json fields per command:
    say      list of phrases that trigger it (may contain {folder})
    terminal command line run in a NEW terminal window ("" = just
             open the terminal)
    tab      true = open a new TAB in the terminal window you already
             have (most recent one) instead of a new window
    open     a file, app or web address to open instead of a terminal
    dir      working directory (~ = home; may contain {folder})
    toast    the little message the pill shows ({folder} filled in)

The file is re-read whenever it changes on disk — edit and save, no
restart needed.
"""

import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser

# Mis-hearings folded before MATCHING only — typed text is untouched.
WORD_ALIASES = {
    "clawed": "claude", "claud": "claude", "clod": "claude",
    "clawd": "claude", "cloud": "claude", "clored": "claude",
}

NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16",
    "seventeen": "17", "eighteen": "18", "nineteen": "19",
    "twenty": "20",
}

DEFAULT_COMMANDS = {
    "_help": [
        "Voice commands: say a whole phrase from a 'say' list while",
        "dictating and DictationMic runs the task instead of typing it.",
        "{folder} captures spoken words and finds a matching real folder",
        "inside 'dir' (saying 'folder one' finds 'folder1').",
        "Fields: say = trigger phrases | terminal = command to run in a",
        "new terminal window ('' = just open one) | tab = true opens a",
        "new TAB in your existing terminal window instead | open =",
        "file/app/web address to open instead | dir = starting folder",
        "(~ = home) | toast = the message the pill shows.",
        "Edit and save this file freely - it reloads automatically.",
    ],
    "commands": [
        {
            "say": ["open claude in a terminal",
                    "open claude in the terminal",
                    "open a terminal and open claude",
                    "open a new terminal and open claude",
                    "open terminal and open claude",
                    "open claude code"],
            "terminal": "claude",
            "dir": "~/Desktop",
            "toast": "Starting Claude Code on the Desktop",
        },
        {
            "say": ["open up {folder} in claude code",
                    "open {folder} in claude code",
                    "open up {folder} in claude",
                    "open {folder} in claude"],
            "terminal": "claude",
            "dir": "~/Desktop/{folder}",
            "toast": "Starting Claude Code in {folder}",
        },
        {
            "say": ["open a new tab", "open new tab",
                    "open a new tab with claude",
                    "open claude in a new tab"],
            "terminal": "claude",
            "tab": True,
            "dir": "~/Desktop",
            "toast": "New tab — starting Claude Code",
        },
        {
            "say": ["open up {folder} in a new tab",
                    "open {folder} in a new tab"],
            "terminal": "claude",
            "tab": True,
            "dir": "~/Desktop/{folder}",
            "toast": "New tab — Claude Code in {folder}",
        },
        {
            "say": ["open the terminal", "open a terminal",
                    "open a new terminal"],
            "terminal": "",
            "dir": "~/Desktop",
            "toast": "Opening a terminal on the Desktop",
        },
    ],
}


def normalize(text):
    """Lowercase, drop punctuation, fold mis-hearings, strip 'please'."""
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    words = [WORD_ALIASES.get(w, w) for w in words]
    if words and words[0] == "please":
        words = words[1:]
    if words and words[-1] == "please":
        words = words[:-1]
    return " ".join(words)


def fold_name(s):
    """'Folder One' / 'folder-one' / 'folder1' all become 'folder1'."""
    parts = re.split(r"[^a-z0-9]+", s.lower())
    return "".join(NUMBER_WORDS.get(p, p) for p in parts if p)


def resolve_folder(parent, spoken):
    """Find the real sub-folder of parent the spoken words meant."""
    want = fold_name(spoken)
    if not want:
        return None
    try:
        names = [n for n in os.listdir(parent)
                 if os.path.isdir(os.path.join(parent, n))]
    except OSError:
        return None
    folded = {}
    for n in names:
        folded.setdefault(fold_name(n), n)
    if want in folded:
        return folded[want]

    def digits_agree(cand):
        # "folder9" must never fuzzy-match "folder1" — numbers are exact
        return re.findall(r"\d+", cand) == re.findall(r"\d+", want)

    starts = [n for f, n in folded.items()
              if (f.startswith(want) or want.startswith(f))
              and digits_agree(f)]
    if len(starts) == 1:
        return starts[0]
    close = difflib.get_close_matches(want, list(folded), n=3, cutoff=0.75)
    for c in close:
        if digits_agree(c):
            return folded[c]
    return None


def compile_pattern(pat):
    """'open {folder} in claude' -> regex over normalized utterances."""
    pieces = [normalize(p) for p in pat.split("{folder}")]
    if len(pieces) == 1:
        return re.compile(re.escape(pieces[0]) + r"\Z")
    left, right = pieces[0], pieces[-1]
    rx = (re.escape(left) + " ") if left else ""
    rx += r"(?P<folder>.+?)" if right else r"(?P<folder>.+)"
    if right:
        rx += " " + re.escape(right)
    return re.compile(rx + r"\Z")


def launch_terminal(workdir, command, tab=False):
    """New terminal window at workdir — or, with tab=True, a new TAB in
    the most recently used Windows Terminal window (a window is created
    if none is open). Optionally runs a command and stays open."""
    wt = shutil.which("wt")
    if wt:
        where = ["-w", "last", "new-tab"] if tab else ["-w", "new"]
        args = [wt] + where + ["-d", workdir]
        if command:
            args += ["cmd", "/k", command]
        subprocess.Popen(args)
    else:
        # plain consoles have no tabs — a fresh window is the best we can do
        args = ["cmd", "/k", command] if command else ["cmd"]
        subprocess.Popen(args, cwd=workdir,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)


# ---------------------------------------------------------------------------
# "Hey Mike" — wake word + the executor for the brain's actions
# ---------------------------------------------------------------------------

def wake_regex(wake_words):
    """One regex matching any wake phrase, tolerant of 'Hey, Mike!'."""
    alts = []
    for phrase in wake_words:
        words = normalize(phrase).split()
        if words:
            alts.append(r"[\W_]+".join(re.escape(w) for w in words))
    if not alts:
        return None
    return re.compile(r"(?:^|[\W_])(?:%s)(?:[\W_]+|$)" % "|".join(alts),
                      re.IGNORECASE)


def split_wake(text, wake_words):
    """None if no wake word; else (before, after) around the FIRST one —
    'that's done. Hey Mike, open chrome' -> ("that's done. ", "open chrome")."""
    rx = wake_regex(wake_words)
    if rx is None:
        return None
    m = rx.search(text)
    if m is None:
        return None
    return text[:m.start()], text[m.end():]


# apps whose real exe name isn't what you'd say
APP_ALIASES = {
    "chrome": "chrome", "google chrome": "chrome",
    "edge": "msedge", "microsoft edge": "msedge",
    "notepad": "notepad", "notebook": "notepad",
    "calculator": "calc", "paint": "mspaint",
    "word": "winword", "microsoft word": "winword",
    "excel": "excel", "powerpoint": "powerpnt",
    "explorer": "explorer", "file explorer": "explorer",
    "task manager": "taskmgr", "control panel": "control",
    "settings": "ms-settings:", "windows settings": "ms-settings:",
    "terminal": "wt", "windows terminal": "wt",
}

_app_index = None    # fold_name(shortcut stem) -> .lnk path, built once


def _start_menu_index():
    global _app_index
    if _app_index is not None:
        return _app_index
    roots = [
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                     r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
    ]
    index = {}
    for root in roots:
        for ext in ("*.lnk", "*.url"):
            for p in glob.glob(os.path.join(root, "**", ext), recursive=True):
                stem = os.path.splitext(os.path.basename(p))[0]
                index.setdefault(fold_name(stem), p)
    _app_index = index
    return index


def resolve_app(name):
    """Spoken app name -> something os.startfile can open, or None."""
    said = normalize(name)
    tries = []
    alias = APP_ALIASES.get(said)
    if alias:
        if alias.endswith(":"):          # a settings-style URI
            return alias
        tries.append(alias)
    tries.append(said)
    for t in tries:
        found = shutil.which(t)
        if found:
            return found
    index = _start_menu_index()
    want = fold_name(said)
    if not want:
        return None
    if want in index:
        return index[want]
    starts = [p for f, p in index.items()
              if f.startswith(want) or want.startswith(f)]
    if not starts and len(want) >= 4:
        # "chrome" lives inside "Google Chrome" — substring as a fallback
        starts = [p for f, p in index.items() if want in f]
    if starts:
        return sorted(starts, key=lambda p: len(os.path.basename(p)))[0]
    close = difflib.get_close_matches(want, list(index), n=1, cutoff=0.8)
    return index[close[0]] if close else None


def execute_actions(actions, say, dbg=lambda m: None):
    """Run the brain's actions. Returns (fired, toast) — fired False means
    nothing happened (stay in command mode and let Steve try again)."""
    desktop = os.path.expanduser("~/Desktop").replace("/", os.sep)
    done, failed = 0, []
    for a in (actions or [])[:6]:
        kind = a.get("kind")
        try:
            if kind == "open_app":
                target = resolve_app(a.get("target") or "")
                if target is None:
                    failed.append(f"couldn't find “{a.get('target')}”")
                else:
                    os.startfile(target)
                    done += 1
            elif kind == "open_url":
                url = (a.get("target") or "").strip()
                if url:
                    if "://" not in url:
                        url = "https://" + url
                    webbrowser.open(url)
                    done += 1
            elif kind == "open_terminal":
                tabs = max(1, min(int(a.get("tabs") or 1), 8))
                raw = (a.get("dir") or "").strip()
                if raw and os.path.isabs(raw) and os.path.isdir(raw):
                    workdir = raw
                elif raw:
                    folder = resolve_folder(desktop, raw)
                    if folder is None:
                        failed.append(f"no folder like “{raw}”")
                        continue
                    workdir = os.path.join(desktop, folder)
                else:
                    workdir = desktop
                for i in range(tabs):
                    launch_terminal(workdir, a.get("run") or "", tab=True)
                    if i < tabs - 1:
                        time.sleep(0.35)   # let wt process tabs in order
                done += 1
            elif kind == "open_folder":
                raw = (a.get("target") or "").strip()
                if os.path.isdir(raw):
                    os.startfile(raw)
                    done += 1
                else:
                    folder = resolve_folder(desktop, raw)
                    if folder is None:
                        failed.append(f"no folder like “{raw}”")
                    else:
                        os.startfile(os.path.join(desktop, folder))
                        done += 1
        except Exception as ex:
            dbg(f"action {kind} failed: {ex!r}")
            failed.append(f"{kind} failed")
    if done:
        toast = "⚡ " + (say or "Done")
        if failed:
            toast += " — but " + "; ".join(failed)
        return True, toast
    if failed:
        return False, "🤔 " + "; ".join(failed) + " — try again"
    return False, None


class VoiceCommands:
    def __init__(self, path, dbg=lambda m: None):
        self.path = path
        self.dbg = dbg
        self._mtime = None
        self._compiled = []       # (regex, command-dict) in file order
        if not os.path.exists(path):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_COMMANDS, f, indent=2)
            except OSError:
                pass
        self._reload_if_changed()

    def _reload_if_changed(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        compiled = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for cmd in data.get("commands", []):
                for phrase in cmd.get("say", []):
                    compiled.append((compile_pattern(phrase), cmd))
        except Exception as ex:
            # a half-saved edit mustn't kill dictation — keep the old set
            self.dbg(f"commands.json not loaded: {ex!r}")
            return
        self._compiled = compiled
        self.dbg(f"voice commands loaded: {len(compiled)} phrases")

    def try_run(self, text):
        """Return None if text is ordinary dictation, else the toast to
        show (the task has been launched, or the error explained)."""
        self._reload_if_changed()
        utterance = normalize(text)
        if not utterance:
            return None
        for rx, cmd in self._compiled:
            m = rx.fullmatch(utterance)
            if m:
                return self._execute(cmd, m.groupdict().get("folder"))
        return None

    def _execute(self, cmd, spoken_folder):
        workdir = os.path.expanduser(cmd.get("dir", "~"))
        workdir = workdir.replace("/", os.sep)
        folder = None
        if "{folder}" in workdir:
            parent = os.path.dirname(workdir.split("{folder}")[0] + "x")
            folder = resolve_folder(parent, spoken_folder or "")
            if folder is None:
                return (f"Couldn't find a folder like "
                        f"“{spoken_folder}” in {parent}")
            workdir = workdir.replace("{folder}", folder)
        if not os.path.isdir(workdir):
            return f"That folder doesn't exist: {workdir}"
        toast = cmd.get("toast") or "Running your voice command"
        toast = toast.replace("{folder}", folder or spoken_folder or "")
        try:
            if cmd.get("open"):
                os.startfile(cmd["open"].replace("{folder}", folder or ""))
            elif "terminal" in cmd:
                launch_terminal(workdir, cmd.get("terminal", ""),
                                tab=bool(cmd.get("tab")))
            else:
                return None     # a command with nothing to do — ignore it
        except Exception as ex:
            self.dbg(f"voice command failed: {ex!r}")
            return "That command failed to start — check commands.json"
        return "⚡ " + toast
