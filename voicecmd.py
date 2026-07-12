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
    open     a file, app or web address to open instead of a terminal
    dir      working directory (~ = home; may contain {folder})
    toast    the little message the pill shows ({folder} filled in)

The file is re-read whenever it changes on disk — edit and save, no
restart needed.
"""

import difflib
import json
import os
import re
import shutil
import subprocess

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
        "new terminal window ('' = just open one) | open = file/app/web",
        "address to open instead | dir = starting folder (~ = home) |",
        "toast = the message the pill shows.",
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


def launch_terminal(workdir, command):
    """New terminal window at workdir, optionally running a command and
    staying open. Prefers Windows Terminal, falls back to a console."""
    wt = shutil.which("wt")
    if wt:
        args = [wt, "-w", "new", "-d", workdir]
        if command:
            args += ["cmd", "/k", command]
        subprocess.Popen(args)
    else:
        args = ["cmd", "/k", command] if command else ["cmd"]
        subprocess.Popen(args, cwd=workdir,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)


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
                launch_terminal(workdir, cmd.get("terminal", ""))
            else:
                return None     # a command with nothing to do — ignore it
        except Exception as ex:
            self.dbg(f"voice command failed: {ex!r}")
            return "That command failed to start — check commands.json"
        return "⚡ " + toast
