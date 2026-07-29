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

After the "Hey Mike" wake word, matching gets far more forgiving:
parse_launch understands however you happen to word a launch — "open
Claude Code", "open up a new instance of claude", "four new tabs of
claude code", "fire up a terminal in folder one". That looseness is
deliberately kept behind the wake word, so dictating "open claude code"
into a chat still types the words. Terminals always run PowerShell, so
`claude` starts exactly as if it had been typed at a prompt.

Terminals open as TABS by default — say "open up four new instances of
Claude Code" and you get four tabs in the one Windows Terminal window,
not four windows scattered over the screen. Only actually asking for a
"window" (or "separate windows") opens separate windows.

commands.json fields per command:
    say      list of phrases that trigger it (may contain {folder})
    terminal command line run in the terminal ("" = just open one)
    tab      tabs in the window you already have. TRUE by default —
             set "tab": false for a separate window instead
    count    how many tabs/windows to open (1-8, default 1)
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
        "Fields: say = trigger phrases | terminal = command to run ('' =",
        "just open one) | tab = TABS in the terminal you already have,",
        "true by default - set false for a separate window | count = how",
        "many tabs (1-8) | open = file/app/web address to open",
        "instead | dir = starting folder (~ = home) | toast = the",
        "message the pill shows.",
        "Terminals open as TABS in ONE window. 'Four new instances of",
        "claude code' = four tabs, not four windows. Say 'window' or",
        "'separate windows' if you really want separate windows.",
        "Say 'Hey Mike' first and you don't need a phrase here at all -",
        "any wording starts Claude Code or a terminal ('open up four new",
        "instances of claude', 'four new tabs of claude code', 'fire up",
        "a terminal in folder one'). Without 'Hey Mike' only the exact",
        "phrases below fire, so dictation is never launched by",
        "accident. Terminals run PowerShell.",
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


_shell = None


def shell_command():
    """The shell voice-launched terminals run in: PowerShell 7 (pwsh) when
    it's installed, else Windows PowerShell. Never cmd."""
    global _shell
    if _shell is None:
        _shell = (shutil.which("pwsh") or shutil.which("powershell")
                  or "powershell.exe")
    return _shell


def launch_terminal(workdir, command, tab=True, count=1):
    """Open `count` PowerShell terminals at workdir, optionally running a
    command and staying open afterwards.

    TABS ARE THE DEFAULT: they all land as new tabs in the Windows
    Terminal window already open (the most recently used one; a window is
    created if none is open). tab=False is the deliberate exception —
    each one gets its own separate window.

    Every tab is asked for in ONE `wt` call. Running `wt` several times in
    a row races itself: the second call looks for "the last window" before
    the first has finished registering, so the tabs scatter across
    separate windows — exactly what asking for tabs was meant to avoid.

    The shell is PowerShell, so `claude` starts exactly as if it had been
    typed at a PowerShell prompt, and the prompt stays afterwards."""
    ps = shell_command()
    count = max(1, min(int(count or 1), 8))
    # -NoExit keeps the session alive after the command finishes, so the
    # window behaves like one Steve opened and typed into himself.
    run = [ps, "-NoExit"] + (["-Command", command] if command else [])
    wt = shutil.which("wt")
    if not wt:
        # plain consoles have no tabs — fresh windows are the best we can do
        for i in range(count):
            subprocess.Popen(run, cwd=workdir,
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
            if i < count - 1:
                time.sleep(0.35)
        return
    if tab:
        # wt -w last new-tab -d dir <shell> ; new-tab -d dir <shell> ; ...
        # a bare ";" argument is how wt separates one sub-command from the
        # next, and one call means one window, always.
        args = [wt, "-w", "last"]
        for i in range(count):
            if i:
                args.append(";")
            args += ["new-tab", "-d", workdir] + run
        subprocess.Popen(args)
    else:
        for i in range(count):
            subprocess.Popen([wt, "-w", "new", "-d", workdir] + run)
            if i < count - 1:
                time.sleep(0.35)   # let each window claim itself first


# ---------------------------------------------------------------------------
# The loose launcher: "open Claude Code", however you happen to say it
# ---------------------------------------------------------------------------
#
# The exact phrase lists above are unforgiving — a single stray "up" or
# "of" and nothing fires. This second pass understands ANY arrangement of
# the words below, so "open Claude Code", "open up a new instance of
# claude", "four new tabs of claude code" and "fire up two claude windows
# in folder one" all work without listing every wording.
#
# It only runs AFTER the wake word (try_run's loose=True) — nothing said
# to Mike was ever going to be typed, so being generous there is free.
# Even then, EVERY word of the utterance has to be one of these known
# words (the only exception is a folder name straight after "in"/"at",
# and that folder has to really exist). One unknown word and it isn't a
# command.

LAUNCH_VERBS = {"open", "start", "launch", "run", "fire", "boot", "spin",
                "pop", "bring", "load", "give", "get", "make", "create",
                "new", "another"}

# words that carry no meaning here and can appear anywhere
LAUNCH_FILLER = {"a", "an", "the", "up", "me", "my", "for", "of", "with",
                 "and", "to", "in", "on", "into", "at", "just", "please",
                 "code", "fresh", "new", "another", "it", "its", "their",
                 "own", "each", "more", "some", "running", "there", "over"}

LAUNCH_COUNTS = dict(NUMBER_WORDS, a="1", an="1", couple="2", few="3")

TAB_WORDS = {"tab", "tabs"}
# The ONLY words that turn tabs off. Tabs are what you get otherwise.
WINDOW_WORDS = {"window", "windows", "separate", "separately"}
# These just NAME the thing being opened — they do NOT mean a separate
# window. "four new instances of Claude Code" is four tabs in one window.
THING_WORDS = {"instance", "instances", "session", "sessions", "copy",
               "copies", "version", "versions"}
TERMINAL_WORDS = {"terminal", "terminals", "console", "powershell", "shell",
                  "prompt", "command", "cmd"}
DESKTOP_WORDS = {"desktop"}
# an unknown word is only allowed as a folder name straight after one of these
LOCATION_WORDS = {"in", "inside", "at", "on", "into", "under", "within",
                  "from", "for"}


def parse_launch(utterance):
    """Understand a spoken launch request that the exact phrases missed.

    Takes an already-normalized utterance. Returns (command-dict,
    spoken folder) ready for VoiceCommands._execute, or None when the
    words aren't a launch request at all."""
    words = utterance.split()
    if not words or words[0] not in LAUNCH_VERBS:
        return None
    count = tab = window = claude = terminal = None
    folder_words = []
    i = 1
    while i < len(words):
        w = words[i]
        if w in LAUNCH_COUNTS:
            count = int(LAUNCH_COUNTS[w])
        elif w.isdigit():
            count = int(w)
        elif w in TAB_WORDS:
            tab = True
        elif w in WINDOW_WORDS:
            window = True
        elif w in THING_WORDS:
            pass                      # "instance"/"session" isn't a window
        elif w == "claude":
            claude = True
        elif w in TERMINAL_WORDS:
            terminal = True
        elif w in DESKTOP_WORDS:
            pass                      # the Desktop is where we start anyway
        elif w in LAUNCH_FILLER:
            pass
        elif words[i - 1] in LOCATION_WORDS:
            folder_words = words[i:]  # "…in folder one" — the rest is a name
            break
        else:
            return None               # a word I don't know: ordinary speech
        i += 1
    if not (claude or terminal):
        return None                   # nothing to actually open
    n = max(1, min(count or 1, 8))
    # Tabs in the one window are the DEFAULT. Only actually saying
    # "window"/"separate" splits them into separate windows.
    tab = not window
    what = "Claude Code" if claude else "a terminal"
    where = "in {folder}" if folder_words else "on the Desktop"
    if n > 1:
        toast = f"{n} {'tabs' if tab else 'windows'} — {what} {where}"
    elif tab:
        toast = f"New tab — {what} {where}"
    else:
        toast = f"Starting {what} {where}"
    cmd = {
        "terminal": "claude" if claude else "",
        "tab": tab,
        "count": n,
        "dir": "~/Desktop/{folder}" if folder_words else "~/Desktop",
        "toast": toast,
    }
    return cmd, " ".join(folder_words)


# ---------------------------------------------------------------------------
# "Hey Mike" — wake word + the executor for the brain's actions
# ---------------------------------------------------------------------------

# Speech recognition rarely writes "Mike" — it writes whatever it thought
# it heard. Any wake phrase ending in one of these is matched with all of
# them, so "hey mic", "hey muck" and "hey mack" all wake Mike. "Mikey" is
# deliberately absent: it's a name, not a mis-hearing.
MIKE_HEARD = ("mike", "mic", "mick", "myke", "mac", "mack", "muck", "nike",
              "mick", "mikes", "mick")

_MIKE_LEAD = re.compile(r"^[\W_]*(?:%s)(?:[\W_]+|$)" % "|".join(set(MIKE_HEARD)),
                        re.IGNORECASE)


def _wake_variants(phrase):
    """['hey', 'mike'] -> one word-list per way 'mike' gets transcribed."""
    words = normalize(phrase).split()
    if not words:
        return []
    if words[-1] in MIKE_HEARD:
        return [words[:-1] + [alt] for alt in dict.fromkeys(MIKE_HEARD)]
    return [words]


def wake_regex(wake_words):
    """One regex matching any wake phrase, tolerant of 'Hey, Mike!'."""
    alts = []
    for phrase in wake_words:
        for words in _wake_variants(phrase):
            rx = r"[\W_]+".join(re.escape(w) for w in words)
            if rx not in alts:
                alts.append(rx)
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
    after = text[m.end():]
    # "hey muck mic, open chrome" — one wake word heard twice. Drop the
    # spare so it doesn't end up inside the command.
    for _ in range(2):
        trimmed = _MIKE_LEAD.sub("", after, count=1)
        if trimmed == after:
            break
        after = trimmed
    return text[:m.start()], after


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


# Full-control mode: Mike runs whatever PowerShell the brain writes. The
# ONLY refusals are catastrophic, never-meant-by-voice operations — this is
# a tripwire against a disastrous mis-hearing, not a security boundary.
DANGER_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bformat(\.com)?\s+[a-z]:",                       # format a drive
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\bcipher\s+/w\b",
    r"\bvssadmin\b.*\bdelete\b",
    r"set-mppreference.*disabl",                        # turn off Defender
    r"(remove-item|\brd\b|\brmdir\b|\bdel\b|\brm\b)"    # delete a drive root,
    r"[^;&|]*[\"' ][a-z]:[\\/]?[\"']?\s*(-|;|$)",       # e.g. rm C:\ -Recurse
    r"(remove-item|\brd\b|\brmdir\b|\bdel\b|\brm\b)"
    r"[^;&|]*\\(windows|program files)",
)]

AUDIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "mike-actions.log")


def _audit(line):
    """Every run_command lands in mike-actions.log — the paper trail for
    full-control mode. Must never raise."""
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + line + "\n")
    except Exception:
        pass


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
                # tabs in the terminal already open are the default; only
                # an explicit "separate window" gets a window of its own
                as_tab = not a.get("window")
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
                launch_terminal(workdir, a.get("run") or "", tab=as_tab,
                                count=tabs)
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
            elif kind == "create_folder":
                raw = (a.get("target") or "").strip()
                if os.path.isabs(raw):
                    path = raw
                else:
                    # strip characters Windows folder names can't hold
                    name = re.sub(r'[<>:"/\\|?*]', "", raw).strip(". ")
                    if not name:
                        failed.append("couldn't make out the folder name")
                        continue
                    path = os.path.join(desktop, name)
                if os.path.isdir(path):
                    failed.append(f"“{os.path.basename(path)}” already exists")
                else:
                    os.makedirs(path)
                    done += 1
            elif kind == "run_command":
                cmd = (a.get("run") or "").strip()
                if not cmd:
                    failed.append("no command to run")
                    continue
                if any(p.search(cmd) for p in DANGER_PATTERNS):
                    _audit("BLOCKED  " + cmd)
                    failed.append("that looked too dangerous — I didn't run it")
                    continue
                _audit("RUN      " + cmd)
                try:
                    p = subprocess.run(
                        ["powershell", "-NoProfile", "-ExecutionPolicy",
                         "Bypass", "-Command", cmd],
                        capture_output=True, text=True, timeout=45,
                        creationflags=0x08000000)       # CREATE_NO_WINDOW
                except subprocess.TimeoutExpired:
                    _audit("TIMEOUT")
                    failed.append("that command took too long — gave up")
                    continue
                _audit(f"EXIT {p.returncode}")
                if p.returncode == 0:
                    done += 1
                else:
                    err = (p.stderr or p.stdout or "").strip().splitlines()
                    failed.append((err[0][:90] if err
                                   else f"command failed ({p.returncode})"))
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

    def try_run(self, text, loose=False):
        """Return None if text is ordinary dictation, else the toast to
        show (the task has been launched, or the error explained).

        Only the exact phrases in commands.json fire while you're just
        dictating. The forgiving parse_launch matching needs loose=True,
        which the pill only passes once you've said "Hey Mike" — so a
        sentence like "open claude code" typed into a chat stays typed."""
        self._reload_if_changed()
        utterance = normalize(text)
        if not utterance:
            return None
        for rx, cmd in self._compiled:
            m = rx.fullmatch(utterance)
            if m:
                return self._execute(cmd, m.groupdict().get("folder"))
        guess = parse_launch(utterance) if loose else None
        if guess is not None:
            cmd, spoken_folder = guess
            if spoken_folder:
                desktop = os.path.expanduser("~/Desktop").replace("/", os.sep)
                if resolve_folder(desktop, spoken_folder) is None:
                    return None      # no such folder — so it wasn't a command
            return self._execute(cmd, spoken_folder)
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
        count = max(1, min(int(cmd.get("count") or 1), 8))
        try:
            if cmd.get("open"):
                os.startfile(cmd["open"].replace("{folder}", folder or ""))
            elif "terminal" in cmd:
                # no "tab" in the command means tabs — that's the default;
                # a command has to say "tab": false to get its own window
                launch_terminal(workdir, cmd.get("terminal", ""),
                                tab=bool(cmd.get("tab", True)), count=count)
            else:
                return None     # a command with nothing to do — ignore it
        except Exception as ex:
            self.dbg(f"voice command failed: {ex!r}")
            return "That command failed to start — check commands.json"
        return "⚡ " + toast
