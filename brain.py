"""The "Hey Mike" brain — natural-language voice commands via Gemini.

After the wake word, whatever Steve says is sent HERE (and only here —
ordinary dictation never leaves the machine) to be turned into a small
list of launcher actions:

    open_app       target = app name ("chrome", "notepad", "word")
    open_url       target = full URL (invented from a spoken description)
    open_terminal  tabs = how many, dir = folder, run = command ("claude")
    open_folder    target = folder name or path
    none           the words weren't a command after all

Uses the Gemini API free tier (generous daily allowance, £0). The key
comes from settings["gemini_api_key"] or a one-line gemini.key file
next to app.py — gitignored, never written by the pill. No key or no
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
- none: the words are NOT an instruction to the computer — ordinary
  dictation, conversation, or you can't tell. WHEN IN DOUBT, "none".

Several things at once is fine: "open chrome and notepad" = two
actions. "open four tabs with claude" = one open_terminal, tabs 4.

Set "say" to a short, friendly confirmation of what you're doing
(or why nothing), under ten words.

Desktop folders: {folders}
"""


class Brain:
    def __init__(self, settings, app_dir, dbg=lambda m: None):
        self.settings = settings
        self.app_dir = app_dir
        self.dbg = dbg
        self._model = None          # first model that answered

    def key(self):
        k = (self.settings.get("gemini_api_key") or "").strip()
        if k:
            return k
        try:
            with open(os.path.join(self.app_dir, "gemini.key"),
                      "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def has_key(self):
        return bool(self.key())

    def _desktop_folders(self):
        desk = os.path.expanduser("~/Desktop").replace("/", os.sep)
        try:
            return [n for n in os.listdir(desk)
                    if os.path.isdir(os.path.join(desk, n))]
        except OSError:
            return []

    def interpret(self, text):
        """Spoken words -> {"actions": [...], "say": str} or {"error": str}."""
        key = self.key()
        if not key:
            return {"error": "The Hey Mike brain needs a Gemini key — "
                             "see gemini.key in the DictationMic folder"}
        payload = {
            "systemInstruction": {"parts": [{"text": INSTRUCTIONS.replace(
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
