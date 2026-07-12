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
6. **Right-click** the pill for the menu — the big volt button at the top,
   **Open DictationMic**, opens the full-screen app (all your notes, image
   entries and files in one window); below it live the options, including
   **Change hotkey…** (press any key you like) and clipboard mode.

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

## Screenshots — the shelf

Take a screenshot (**Win+Shift+S**, PrtScn) or copy any image and the pill
catches it: a little badge appears on the pill's shoulder with a count,
lime until you've looked. The shot is kept as a real PNG in `shots\`
(newest 12, oldest pruned automatically).

Click the badge and the shelf pops open as thumbnails:

- **Drag one out** — a real file drag, straight into Claude Code, a chat
  box, an email, Explorer… anywhere that takes a file.
- **Click one** — copies it as a file *and* as a bitmap, so **Ctrl+V**
  pastes whichever the target app prefers (Claude Code pastes the image).
- Hover **✕** removes a shot; **Clear all** / **Open folder** live in the
  footer. Images dropped on the pill land on the shelf too.

Don't want it? Untick **Catch screenshots & copied images** in the
right-click menu. The shelf itself is a local scratch tray (newest 12,
nothing syncs from it) — but caught screenshots are **also saved as image
notes** so they show up in the full app and on your phone; untick
**Save caught screenshots to my notes** to keep them shelf-only.

## Phone sync

Right-click the pill → **Set up phone sync…** — enter an email and pick a
password (the account is created for you the first time). Then on your
Android phone open **https://dictationmic-sync.web.app**, sign in with the
same details, and use Chrome's ⋮ → *Add to Home screen* to install it.

- Dictate on the phone (big mic button — records silently, no beeps; the
  computer writes it into text within seconds of seeing it) and the note
  lands in `notes\` on the computer.
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

## Google Calendar — "add to calendar"

Say **"add to calendar"** anywhere in a dictation and the note becomes a
Google Calendar event too. The note itself is untouched — full text, same
place in the list — it just gets a volt wash and a little event chip
("Tomorrow · 15:00") in the notes app, plus a link to open the event. The
pill (and the notes app, if it's open) give you a heads-up 15 minutes
before a timed event starts.

- *"Add to calendar dentist tomorrow at 3"* → Sat 3:00 pm, one hour.
- Understands today/tomorrow/weekdays, "on the 15th", "august 15", "15/8",
  "at 3(pm)", "3:30", noon/tonight/morning…, "for 2 hours", "10 to 2",
  "in 45 minutes". A bare "at 3" reads as 3 pm; "at 9" as 9 am.
- A date with no time makes an **all-day** event; no date and no time at
  all makes an all-day event **today**.
- Works from the phone as well — a phone note (voice or typed) that says
  "add to calendar" gets its event as soon as the computer sees it, exactly
  like voice-note transcription.
- If the event can't be made (Google unreachable, sign-in expired) the note
  is washed amber instead so you can see it never landed; editing the note
  arms it again.

### One-time setup (~5 minutes)

Google doesn't hand out calendar access without your own (free) OAuth
client, so once, in [console.cloud.google.com](https://console.cloud.google.com)
with the **dictationmic-sync** project selected:

1. **APIs & Services → Library** → search *Google Calendar API* → **Enable**.
2. **APIs & Services → OAuth consent screen** → External → fill in the two
   required fields (app name, your email) → save. Under *Publishing status*
   press **Publish app** (staying in "Testing" makes Google expire the
   connection every 7 days). It stays unverified — that's fine, only you
   use it; the consent page just shows an extra "unverified" step the one
   time you connect.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   → application type **Desktop app** → Create. Copy the **Client ID** and
   **Client secret**.
4. Right-click the pill → **Connect Google Calendar…** → paste both →
   **Connect** → approve in the browser tab that opens. Done — the menu
   shows which Google account is connected.

Disconnect any time from the same menu (this revokes the token with
Google). Apple / iCloud Calendar is planned as a second provider; the
events the app makes carry your calendar's default reminders, so your
phone still buzzes even when the pill isn't running. Note edits and
deletes don't (yet) follow through to the event — manage the event in
Google Calendar once it's made.

## Speech engine

**Parakeet** — NVIDIA's Parakeet TDT 0.6B, run locally through
[onnx-asr](https://github.com/istupakov/onnx-asr). One ~660 MB one-time
fetch, then it does **both** jobs — live dictation *and* phone voice
notes — so there's no second model to download or hold in RAM. It sits
above `whisper medium.en` on the open English leaderboards and runs far
faster on CPU: on this machine an 88-second voice note transcribes in
~4 s, and long recordings are chunked at their quietest instant so nothing
is lost at the seams. Everything stays on-device.
Benchmark your own machine any time:

```
venv\Scripts\python.exe bench_stt.py clip.wav [clip.txt with the true words]
```

## Voice commands & "Hey Mike"

Two layers, both hands-free:

**Exact hot words** (offline, instant). If a whole spoken phrase matches
one in `commands.json` — say *"open claude in a terminal"* — the pill
runs the task instead of typing the words. Right-click the pill →
*Edit my voice commands…* to add your own; the file reloads on save.
`{folder}` in a phrase matches a real Desktop folder by sound
("folder one" finds `folder1`), and `"tab": true` opens a tab in the
terminal window you already have.

**"Hey Mike"** (natural language, needs internet). Say **"Hey Mike"**
mid-dictation and the pill stops typing and listens for a command
instead: *"open Chrome and go to fifa.com"*, *"open four tabs with
claude in each"*, *"open Notepad"*, *"open the wankers folder"* —
phrased however you like. Powered by the Gemini API's **free tier**
(hundreds of commands a day, £0): put a key from
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) in a
one-line `gemini.key` file next to `app.py` (gitignored, never leaves
your machine except to Google). Only what you say **after** "Hey Mike"
is ever sent — ordinary dictation stays fully offline. Nothing said in
command mode is ever typed; say *"back to typing"* (or just stop) to
return to dictation. No key? Exact hot words keep working.

## Claude / MCP

`mcp_server.py` serves your notes over the
[Model Context Protocol](https://modelcontextprotocol.io), so Claude Code
(or any MCP client) can work with what you dictate — entirely through the
`notes\` folder, no cloud involved:

- `latest_note` — "read my latest note and do what it says" straight after
  you dictate something.
- `search_notes` / `list_notes` / `read_note` — everything you've ever
  dictated, searchable from a chat.
- `create_note` — Claude writes a note; the pill adopts it and it's on
  your phone moments later.

Run Claude Code inside this folder and it's already wired up (`.mcp.json`).
To have it everywhere:

```
claude mcp add --scope user dictationmic -- ^
  C:\Users\steve\Desktop\DictationMic\venv\Scripts\python.exe ^
  C:\Users\steve\Desktop\DictationMic\mcp_server.py
```

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
| `catch_shots`       | pin screenshots/copied images to the shelf  |
| `shots_keep`        | how many pinned shots to keep, default 12   |
| `shots_to_notes`    | caught screenshots also saved as image notes|
| `beeps`             | `true`/`false` — start/stop sounds          |
| `calendar_enabled`  | "add to calendar" in a dictation makes an event |
| `gcal_client_id` / `gcal_client_secret` | your OAuth desktop client (see above) |

## Tech

- Speech recognition: NVIDIA Parakeet TDT 0.6B via
  [onnx-asr](https://github.com/istupakov/onnx-asr) locally on the CPU
  (int8); speech is chunked into phrases at natural pauses so text appears
  live while you keep talking. faster-whisper stays installed only for its
  `decode_audio` helper (phone voice-note containers → PCM).
- Model auto-downloads from Hugging Face on first run (with resume + retry),
  then lives in `models\parakeet-tdt-0.6b-v2`.

## Rebuilding from source

```
venv\Scripts\pyinstaller --noconsole --name DictationMic --icon icon.ico ^
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime ^
  --add-data "web;web" app.py
```

Run from source instead: `venv\Scripts\pythonw.exe app.py`

Source layout: `app.py` (pill + dictation), `notestore.py` (notes folder +
sync index), `shots.py` (screenshot shelf: clipboard watch, copy, OLE
drag-out), `localserver.py` (localhost API for the notes UI),
`cloudsync.py` (Firebase sync), `web\` (notes UI + phone PWA — deploy with
`firebase deploy --only hosting`). The desktop look (pill, menu, shelf,
toasts) is specced in `DESIGN-DESKTOP.md`; the web app's in `web\DESIGN.md`.
