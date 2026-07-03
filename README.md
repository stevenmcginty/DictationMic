# DictationMic

On-device **live** dictation for Windows. A small, dark floating pill that
turns your speech into text as you talk — **no internet needed** after the
first run, nothing leaves your computer.

## How to use

1. Double-click **DictationMic.exe**.
2. First run only: the pill shows a percentage while it fetches the speech
   model (~480 MB, one time). After that it's fully offline.
3. **Tap RIGHT CTRL** (or click the pill) and just talk. Every time you pause
   for breath, that phrase is typed straight into whatever box your cursor
   is in.
4. Tap again to stop — or say nothing for 10 seconds and it stops itself.
5. **Hold** the hotkey instead of tapping for push-to-talk.
6. **Right-click** the pill for options — including **Change hotkey…**
   (press any key you like) and clipboard mode.

What the pill means:
- **Dark with a row of sleeping grey dots** — idle, ready when you are.
- **Lime-green bars rising and falling** — listening; the meter moves with
  your voice the moment you speak.
- **Three pulsing white dots** — finishing off the last phrase.
- **Percentage + lime progress strip** — one-time model download.

## Your notes

Every finished dictation is **kept automatically** as a plain text file in
the `notes\` folder — whatever mode you're in. Right-click the pill →
**My notes** opens them in your browser (served locally — works offline,
nothing leaves your machine):

- **Search** everything you've ever dictated, edit notes in place,
  rename by clicking the title, **Copy** or **Delete** with one tap.
- Notes are just text files — back them up, sync them, open them anywhere.
- Don't want copies kept? Untick **Keep a copy of each dictation** in the
  right-click menu.

## Phone sync

Right-click the pill → **Set up phone sync…** — enter an email and pick a
password (the account is created for you the first time). Then on your
Android phone open **https://dictationmic-sync.web.app**, sign in with the
same details, and use Chrome's ⋮ → *Add to Home screen* to install it.

- Dictate on the phone (big mic button — records silently, no beeps; the
  computer's Whisper writes it into text within seconds of seeing it) and
  the note lands in `notes\` on the computer.
- Works the other way too: everything you dictate at the computer shows up
  on the phone, and edits, renames and deletes flow both directions.
- No signal? Notes queue on the phone (even if you close the app) and port
  across the next time it's online. Laptop off? They wait in the cloud.
- Notes are stored in your own Firebase project (`dictationmic-sync`),
  readable only by your account. Turn it all off any time from the
  right-click menu.

Extras:
- The pill floats above every window, never steals focus, and can be
  **dragged** anywhere (hold + move). It remembers its spot.
- Hover over it for a reminder of the controls.
- Only one copy runs at a time.

## Sharing it

Send someone `DictationMic-Windows.zip`. They just:
1. Extract it anywhere (e.g. their Desktop).
2. Run `DictationMic.exe` — Windows SmartScreen may warn because the app is
   unsigned: click **More info → Run anyway**.
3. Wait for the one-time model download, then dictate away.

Windows only — the Mac needs a separately built app.

## Settings (settings.json)

| key                 | meaning                                     |
|---------------------|---------------------------------------------|
| `mode`              | `"type"` or `"clipboard"`                   |
| `save_notes`        | keep a copy of each dictation in `notes\`   |
| `hotkeys`           | list of global hotkeys                      |
| `auto_stop_seconds` | stop after this much silence (0 = never)    |
| `size`              | pill width in pixels, default 84            |
| `beeps`             | `true`/`false` — start/stop sounds          |
| `model`             | folder under `models\`, default `small.en`  |

## Tech

- Speech recognition: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  running Whisper `small.en` locally on the CPU (int8) with voice-activity
  detection; speech is chunked into phrases at natural pauses so text appears
  live while you keep talking.
- Model auto-downloads from Hugging Face on first run (with resume + retry),
  then lives in `models\small.en`.

## Rebuilding from source

```
venv\Scripts\pyinstaller --noconsole --name DictationMic --icon icon.ico ^
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime ^
  --add-data "web;web" app.py
```

Run from source instead: `venv\Scripts\pythonw.exe app.py`

Source layout: `app.py` (pill + dictation), `notestore.py` (notes folder +
sync index), `localserver.py` (localhost API for the notes UI),
`cloudsync.py` (Firebase sync), `web\` (notes UI + phone PWA — deploy with
`firebase deploy --only hosting`).
