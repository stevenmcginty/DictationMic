# Porting DictationMic to macOS

`app.py` is the whole program — a floating always-on-top dictation pill.
Tap a hotkey (or click the pill), talk, and each phrase is transcribed
locally with Whisper the moment you pause, then typed into the focused app.
The speech pipeline (LiveRecorder / Transcriber / worker thread), the
pill rendering (PillRenderer, Pillow-drawn capsule with a lime level meter)
and the Notes feature (auto-saves every dictation to `notes/*.txt`, with the
NotesWindow browser: search / copy / rename / delete) are pure Python and
carry over unchanged. What needs replacing is the Windows glue below.

Give this file and app.py to Claude (or any developer) on the Mac and ask
for a macOS port.

## Windows-only pieces and their macOS replacements

| In app.py | What it does | macOS approach |
|---|---|---|
| `keyboard` library (hook + `keyboard.write`) | global talk-key + typing text into the focused app | `pynput` (`keyboard.Listener` + `Controller().type()`); needs **Accessibility** permission granted to the terminal/app in System Settings → Privacy & Security |
| `ctypes.windll` mutex (`already_running`) | single instance | lock file with `fcntl.flock`, or bind a localhost socket |
| `make_non_activating` (WS_EX_NOACTIVATE / TOOLWINDOW) | pill never steals focus, hidden from alt-tab | no direct Tk equivalent; acceptable to skip, or use a small pyobjc call (`NSWindow` `setCanBecomeKeyWindow`/`NSPanel`) if focus-stealing annoys |
| `root.attributes("-transparentcolor", ...)` | shaped (non-rectangular) window | not on mac. Use `root.attributes("-transparent", True)` with `bg='systemTransparent'` (works on macOS Tk), or just keep a rectangular window with the capsule drawn edge-to-edge |
| `winsound.Beep` | start/stop beeps | `NSBeep` via pyobjc, `os.system("afplay /System/Library/Sounds/Pop.aiff &")`, or drop beeps |
| `SetProcessDpiAwareness` | crisp pixels on Windows scaling | delete — macOS handles Retina in Tk |
| Segoe UI font paths (`C:\Windows\Fonts`) | % text during model download | `/System/Library/Fonts/SFNS.ttf` or Helvetica via Pillow default |
| `MessageBoxW` (already-running notice) | info dialog | `tkinter.messagebox.showinfo` |
| `os.startfile` (Notes window: open note / open folder) | open file in default app | `subprocess.call(["open", path])` |
| `DwmSetWindowAttribute` (`make_titlebar_dark`) | dark title bar on the Notes window | delete — macOS follows system appearance |

## Things that work as-is on macOS

- `faster-whisper` / CTranslate2: runs on Apple Silicon and Intel (CPU int8).
- `sounddevice` (PortAudio): needs **Microphone** permission on first run.
- `numpy`, `Pillow`, `pyperclip`, Tkinter UI, the settings.json logic.
- The `models/small.en` folder is platform-independent — copy it next to
  app.py to skip the 480 MB download, or let it auto-download.

## Suggested setup on the Mac (run from source)

```bash
cd DictationMic
python3 -m venv venv
venv/bin/pip install faster-whisper sounddevice numpy pillow pyperclip pynput
venv/bin/python app.py
```

Grant Microphone + Accessibility permissions when macOS asks (System
Settings → Privacy & Security), or hotkeys/typing will silently do nothing.

A double-clickable .app needs PyInstaller run **on the Mac** (see README
for the Windows command; same idea, drop the icon.ico for an .icns).
Unsigned apps need right-click → Open the first time.

## Tuning that took real-world iteration — keep these values

- Phrase cutting: `PAUSE_CUT_S = 0.35`, soft-cut at `0.2` s dips once a
  phrase exceeds `3.5` s, hard cut at `8` s — this is what makes text
  appear *while* you talk instead of after you stop.
- Transcribe with `beam_size=1`, `vad_filter=False`,
  `without_timestamps=True` — latency lives here.
- Keep the `"..."`-scrubbing and breath-hallucination filter in
  `Transcriber.transcribe`, and the warm-up transcribe after model load.
