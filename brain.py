"""The "Hey Mike" brain — natural-language voice commands via Gemini.

After the wake word, whatever Steve says is sent HERE (and only here —
ordinary dictation never leaves the machine) to be turned into a small
list of launcher actions:

    open_app       target = app name ("chrome", "notepad", "word")
    open_url       target = full URL (invented from a spoken description)
    open_terminal  tabs = how many, dir = folder, run = command ("claude")
    open_folder    target = folder name or path
    create_folder  target = new folder's name (made on the Desktop)
    run_command    run = one PowerShell command (full computer control)
    none           the words weren't a command after all

Uses the Gemini API free tier (generous daily allowance, £0). The key
lives in a one-line gemini.key file next to app.py — gitignored, never
leaves the machine except to Google. The pill menu's "My Gemini API
key…" dialog writes it (save_key), so everyone pastes their own; a
settings["gemini_api_key"] is honoured as a fallback. No key or no
internet: interpret() returns an "error" the pill can toast; the strict
hot words in commands.json keep working regardless.
"""

import json
import os

import requests

# tried in order until one answers; the winner is remembered for the run
MODELS = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       "{model}:generateContent")

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "actions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "kind": {"type": "STRING",
                             "enum": ["open_app", "open_url",
                                      "open_terminal", "open_folder",
                                      "create_folder", "run_command",
                                      "none"]},
                    "target": {"type": "STRING"},
                    "tabs": {"type": "INTEGER"},
                    "dir": {"type": "STRING"},
                    "run": {"type": "STRING"},
                },
                "required": ["kind"],
            },
        },
        "say": {"type": "STRING"},
    },
    "required": ["actions", "say"],
}

INSTRUCTIONS = """\
You turn ONE spoken instruction, transcribed from speech, into launcher
actions on a Windows 11 laptop. The speaker is telling their computer
what to open. Speech recognition mangles words — infer what was meant
("open crime" = open chrome; "clawed", "cloud code", "clored" = the
`claude` CLI, which is Claude Code).

Actions:
- open_app: target = the app's common name ("chrome", "notepad",
  "word", "spotify", "calculator"...).
- open_url: target = a full URL. Work the address out from a spoken
  description: "fifa dot com" -> "https://www.fifa.com",
  "the BBC sport website" -> "https://www.bbc.co.uk/sport",
  "gemini" (the AI) -> "https://gemini.google.com".
- open_terminal: Windows Terminal tab(s). tabs = how many (default 1).
  dir = a folder name from the Desktop list below, or empty for the
  Desktop itself. run = command each tab runs, e.g. "claude" for
  Claude Code; empty = a plain terminal.
- open_folder: target = folder name from the list below (or a path);
  opens in Explorer.
- create_folder: target = the name for a NEW folder ("make a folder
  called invoices" -> "Invoices"). It is created on the Desktop unless
  the speaker gives a full path. Capitalise the name sensibly.
- run_command: run = ONE PowerShell command doing exactly what was
  asked. This is for everything the actions above can't do: rename,
  move or delete files and folders, write a file, read something out,
  close an app, change volume, empty the recycle bin, lock or restart
  the computer... Prefer the specific actions above when they fit.
  Rules for the command you write:
  * Do only what was asked — no extras, no cleanup they didn't request.
  * Work inside the speaker's own files (Desktop, Documents, Downloads,
    Pictures...) unless they clearly name somewhere else.
  * NEVER format a drive, never touch C:\\Windows or Program Files,
    never turn off security or delete a whole drive or user profile.
  * ALWAYS reach the user's files through PowerShell variables, NEVER a
    hard-coded C:\\Users\\<name> path (you do NOT know the username):
    "$HOME\\Desktop", "$HOME\\Documents", "$HOME\\Downloads",
    "$HOME\\Pictures". The user's real folders are listed below.
  * When asked to DELETE, move it to the Recycle Bin so mistakes are
    recoverable: Add-Type -AssemblyName Microsoft.VisualBasic;
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("<path>",
    'OnlyErrorDialogs','SendToRecycleBin')  (DeleteFile for a file).
- none: the words are NOT an instruction to the computer — ordinary
  dictation, conversation, or you can't tell. WHEN IN DOUBT, "none".

Several things at once is fine: "open chrome and notepad" = two
actions. "open four tabs with claude" = one open_terminal, tabs 4.

Set "say" to a short, friendly confirmation of what you're doing
(or why nothing), under ten words.

The user's folders (use $HOME-relative paths, these show the layout):
{places}
Desktop folders: {folders}
"""


class Brain:
    def __init__(self, settings, app_dir, dbg=lambda m: None):
        self.settings = settings
        self.app_dir = app_dir
        self.dbg = dbg
        self._model = None          # first model that answered

    def _key_path(self):
        return os.path.join(self.app_dir, "gemini.key")

    def key(self):
        # the file wins — it's the one the pill's dialog manages
        try:
            with open(self._key_path(), "r", encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except OSError:
            pass
        return (self.settings.get("gemini_api_key") or "").strip()

    def has_key(self):
        return bool(self.key())

    def save_key(self, key):
        """Write (or, given an empty key, remove) the gemini.key file."""
        key = (key or "").strip()
        try:
            if key:
                with open(self._key_path(), "w", encoding="utf-8") as f:
                    f.write(key + "\n")
            elif os.path.exists(self._key_path()):
                os.remove(self._key_path())
            return True
        except OSError as ex:
            self.dbg(f"brain key save failed: {ex!r}")
            return False

    def test_key(self, key):
        """One tiny request to prove a pasted key works. -> (ok, message)"""
        payload = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                   "generationConfig": {"maxOutputTokens": 10}}
        last = ""
        for model in MODELS:
            try:
                r = requests.post(URL.format(model=model),
                                  params={"key": key}, json=payload,
                                  timeout=8)
            except requests.RequestException as ex:
                self.dbg(f"brain key test offline: {ex!r}")
                return False, "Can't reach Google — is the internet up?"
            if r.status_code in (200, 429):   # 429 = real key, over quota
                return True, ""
            if r.status_code in (400, 401, 403):
                self.dbg(f"brain key test {model} HTTP {r.status_code}: "
                         f"{r.text[:200]}")
                return False, ("Google rejected that key — copy it fresh "
                               "from aistudio.google.com/apikey")
            last = f"{model}: HTTP {r.status_code}"
        self.dbg(f"brain key test exhausted models: {last}")
        return False, "Couldn't check the key just now — try again"

    def _desktop_folders(self):
        desk = os.path.expanduser("~/Desktop").replace("/", os.sep)
        try:
            return [n for n in os.listdir(desk)
                    if os.path.isdir(os.path.join(desk, n))]
        except OSError:
            return []

    def _places(self):
        home = os.path.expanduser("~")
        lines = [f"  home ($HOME) = {home}"]
        for name in ("Desktop", "Documents", "Downloads", "Pictures"):
            p = os.path.join(home, name)
            if os.path.isdir(p):
                lines.append(f"  $HOME\\{name} = {p}")
        return "\n".join(lines)

    def interpret(self, text):
        """Spoken words -> {"actions": [...], "say": str} or {"error": str}."""
        key = self.key()
        if not key:
            return {"error": "The Hey Mike brain needs a Gemini key — "
                             "pill menu → My Gemini API key"}
        payload = {
            "systemInstruction": {"parts": [{"text": INSTRUCTIONS.replace(
                "{places}", self._places()).replace(
                "{folders}", ", ".join(self._desktop_folders()) or "(none)")}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 800,
                "responseMimeType": "application/json",
                "responseSchema": SCHEMA,
            },
        }
        models = ([self._model] if self._model
                  else ([self.settings.get("brain_model")] if
                        self.settings.get("brain_model") else []) + MODELS)
        last = ""
        for model in models:
            try:
                r = requests.post(URL.format(model=model),
                                  params={"key": key}, json=payload,
                                  timeout=8)
            except requests.RequestException as ex:
                self.dbg(f"brain offline: {ex!r}")
                return {"error": "Can't reach the brain — no internet? "
                                 "Exact hot words still work"}
            if r.status_code == 404:
                last = f"{model}: not found"
                continue                      # try the next model name
            if r.status_code != 200:
                self.dbg(f"brain {model} HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code == 429:
                    return {"error": "The brain is over today's free "
                                     "limit — try again later"}
                last = f"{model}: HTTP {r.status_code}"
                continue
            try:
                out = json.loads(r.json()["candidates"][0]["content"]
                                 ["parts"][0]["text"])
                if not isinstance(out.get("actions"), list):
                    raise ValueError("no actions")
            except Exception as ex:
                self.dbg(f"brain bad reply from {model}: {ex!r}")
                last = f"{model}: bad reply"
                continue
            self._model = model
            return out
        self.dbg(f"brain exhausted models: {last}")
        return {"error": "The brain didn't answer — try again"}
