# DictationMic — Road to Seamless

Goal: dictation that works perfectly on phone and desktop, notes that follow
you everywhere, and a package a friend can install with one click and an
email sign-in. Nothing here changes the core idea — it hardens it.

How the best do it (research summary):
- **Google Recorder** never cuts the audio — one unbroken stream into an
  on-device streaming model, so there are no gaps *by construction*. Lesson:
  if you chop audio into clips, you must pad and overlap the cuts.
- **Wispr Flow** shows nothing live — it records the utterance, transcribes
  it in one go, cleans it with a small LLM, and pastes one perfect block
  ~0.7 s after you stop. Lesson: "one clean block per utterance" beats a
  live stream that churns and garbles.
- **Android Chrome's Web Speech API is officially broken in continuous
  mode** (Chromium bug 40324711, open for 12+ years): finals arrive as
  concatenations of everything said since start. The proven fix is one-shot
  sessions + rebuilding text from `resultIndex` + a silence-timer commit —
  not appending finals, ever.

---

## Phase 1 — Phone dictation: rock solid  ✅ SHIPPED, THEN SUPERSEDED (2026-07-03)

The Web Speech rework (one-shot sessions, fold-not-append, silence-commit)
fixed the garbling, but Chrome's per-phrase mic open/close chime and
indicator flapping can't be fixed from a web page — Steve rejected it.
So the "high-quality mode" from Phase 4 became THE phone mode:

**Recorder architecture (live now):** the phone records one continuous,
silent webm/opus track (MediaRecorder, 32kbps, real level meter, 60s
silence auto-stop, 10min cap). Stopping saves a placeholder note with the
audio riding the offline-safe outbox. The pill's cloudsync spots
untranscribed audio notes, decodes them (PyAV) and transcribes with the
same Whisper as desktop dictation, then patches text+title back and strips
the audio. Verified e2e: injected phone-style note → transcribed in ~5s.
Trade-off: text appears after you stop (Wispr-style), and a computer must
be on at some point. Old Web Speech path deleted.
1. **One-shot sessions on Android** — `continuous = false`; each utterance
   is its own recognition run, auto-restarted on `onend` while the mic is
   live. This sidesteps the Chromium bug entirely instead of patching
   around it.
2. **Rebuild from `event.resultIndex`** — never append raw finals; keep our
   own `committed` text and rebuild the pending part from the results array
   each event.
3. **Silence-timer commit (~750 ms)** — don't trust `isFinal`; when no new
   interim arrives for ~750 ms, commit the current text and restart the
   run. This is also the only strategy that behaves on iOS Safari.
4. **Wispr-style display** — one scratch line shows the in-progress
   utterance; text is only added to the note when an utterance commits.
   The user never sees churn or rewrites.
5. **Test matrix**: 3-sentence paragraph, long 2-minute ramble, rapid
   start/stop, screen-off/on — on Android Chrome and iPhone Safari. Pass =
   every word once, in order, no repeats.

## Phase 2 — Desktop dictation: from 80% to 100%  ✅ FIRST PASS SHIPPED 2026-07-03

Shipped: forced cuts (18s hard / 7s soft) now land at the quietest instant
of the last 1.5s with the remainder carried into the next phrase (no loss,
no duplication — verified by scratchpad cut_test.py: sample-conservation +
cut-in-dip assertions); noise floor no longer rises during speech (it was
reaching speech level in ~4s of unbroken talking, then the silence-trim
discarded REAL words — likely the main dropped-words cause); typing now
2ms/key instead of 0 (delay=0 keystroke flooding drops chars in some apps).
Remaining below if drops persist:
1. **Pad every phrase** — keep ~300–450 ms of audio before the detected
   speech start and after the end (faster-whisper `vad_filter` with
   `speech_pad_ms=400` if using built-in VAD).
2. **Don't split on short pauses** — raise the silence threshold that ends
   a phrase so mid-sentence breaths don't cut words.
3. **Overlap chunks by ~0.5–1 s** and dedupe the overlap textually
   (match the tail of the previous transcript against the head of the new).
4. **Carry context** — pass the last committed sentence as `initial_prompt`
   for the next chunk (reset after long silences to avoid hallucination).
5. **Also investigate the "drops after a certain amount of characters"
   report** — could be the typing-injection path, not transcription. Log a
   dictation to debug.log alongside what was typed and compare.
6. *(Stretch, later)* **LocalAgreement-2** streaming (ufal/whisper_streaming):
   growing audio buffer, re-decode, commit only text that two consecutive
   passes agree on. The gold-standard "never drop a word" architecture.

## Phase 3 — Friend-ready packages

Sign-in and sync already do the right thing: first sign-in auto-creates the
account, every user's notes live under their own uid, unreadable by anyone
else. Model download is already automatic with resume/retry.

1. **Windows** — ✅ shareable now: friends download
   https://dictationmic-sync.web.app/downloads/DictationMic-Windows.zip
   (106 MB, hosted on the project's own Firebase hosting; contains READ ME
   FIRST.txt). DEPLOY NOTE: firebase.json public dir is now `hosting\` —
   build it with `robocopy web hosting /MIR /XD downloads` before every
   `firebase deploy --only hosting`, or the web app changes won't ship;
   the zip lives in `hosting\downloads\` and must never go inside `web\`
   (web\ is bundled into the exe). NEW 2026-07-03: pushing to main also
   auto-deploys hosting via .github/workflows/firebase-hosting-deploy.yml
   (stamps sw.js CACHE with the commit SHA, keeps the friend zip via
   actions/cache) — activates once the one-time service-account secret
   FIREBASE_SERVICE_ACCOUNT_DICTATIONMIC_SYNC is set on the repo; until
   then the workflow runs green but skips. Optional later: Inno Setup
   one-click installer; SmartScreen warning stays unless a code-signing
   cert (~£200/yr) is worth it.
2. **Mac** — MAC-PORT.md source exists; needs building **on a Mac**
   (PyInstaller can't cross-build). Produce a .app in a .dmg. Same
   first-run model download. Gatekeeper equivalent: right-click → Open.
3. **Phone (their phones)** — nothing to install: they open
   dictationmic-sync.web.app, sign in with the same email, Add to Home
   Screen. Works Android + iOS once Phase 1 lands.
4. **A one-page "give to a friend" guide** — link + three steps per device.

## Phase 4 — Lifted from the pros (optional, after 1–3)

1. **Cleanup pass on committed text** — strip fillers ("um", repeated
   words), fix capitalisation/punctuation. Rules first; a small local LLM
   later if wanted. (Wispr's biggest perceived-quality trick.)
2. **High-quality phone mode** — record the utterance audio on the phone
   (MediaRecorder), ship it via Firebase to the laptop, transcribe with the
   same Whisper model, sync the text back. Desktop-grade accuracy on the
   phone, zero cloud cost, falls back to Web Speech when the laptop's off.

## Phase 5 — A clipboard across devices  ✅ SHIPPED 2026-07-03 (late)

Steve's brief: "treat the app like a clipboard" — throw text AND images at
it from any device, get them on every other device, like Google Keep.

Design: an image note is simply a note whose **body is a
`data:image/...;base64` URL** — so images ride the existing sync, outbox,
tombstones and desktop `.txt` files with zero storage/protocol changes.
Compression contract (web/js/imgnote.js == dropnotes.py): longest edge
1600px, JPEG quality ladder 80→50, hard cap 600 KB; originals ≤250 KB
pass through untouched (GIF animation survives); EXIF rotation applied;
transparency flattened. Refuse anything that isn't an image or UTF-8 text
(≤200 KB) with a friendly toast.

What works where:
- **Pill (desktop)**: drag files / selected text / browser images onto it
  (tkdnd; hover ring says "I'll catch it"); middle-click saves the
  clipboard — now including screenshots (Win+Shift+S) and files copied in
  Explorer. Same-day fix: removed speech.js's getUserMedia keep-alive that
  current Android Chrome punishes by starving SpeechRecognition (was:
  beeps but nothing transcribed).
- **Phone/web**: + Image button (camera/gallery), paste anywhere, drag
  onto the window, thumbnails in the list, Copy puts the actual image on
  the clipboard, Share opens the system sheet.

Known trade-off: RTDB's SSE sends the full snapshot on every (re)connect,
so hundreds of image notes would make app-opens heavy. If that day comes,
move image bytes to `/users/$uid/blobs/$noteId` and fetch lazily.

## Phase 6 — File notes: documents across devices  ✅ SHIPPED 2026-07-03

Steve's brief: "almost an AirDrop" — PDFs, Word docs, spreadsheets dragged
onto the pill, viewable on phone/desktop. No videos.

Design: the image-note trick generalised. A file note's body is
`data:<mime>;name=<urlencoded filename>;base64,…` — the `;name=` param
(which image bodies never carry) is what marks a file note and carries the
filename for open/share/download. Files ride every existing pipe (notes\
.txt, RTDB, outbox, IndexedDB) with zero sync/storage changes. Contracts:
dropnotes.py ⇄ web/js/filenote.js, round-trip tested both directions.

Rules: 7 MB cap (base64 of 7 MB ≈ 9.4 MB, under RTDB's 10 MB string
limit); video/audio refused with a toast; CSV/text stay editable text
notes; images stay image notes; a known document mime never falls into the
text path (a tiny all-ASCII PDF must still be a PDF).

What works where:
- **Pill**: drag any document on (or middle-click with files copied in
  Explorer — the clipboard route reuses note_from_path).
- **Phone/web**: + File button, drag onto the window, paste copied files;
  list shows an ext badge + name + size chip; the note view is a file card
  with Open (blob URL, PDFs render in the tab) and Share (share sheet on
  the phone → WhatsApp/Files, download on desktop). sw.js CACHE → v14.

## Order of attack

1. Phase 1 (phone) — the pain point; 1 session of work + phone testing.
2. Phase 2 items 1–5 (desktop padding/overlap + char-drop investigation).
3. Phase 3 item 1 (Windows installer), then 4 (friend guide), then 2 (Mac,
   when a Mac is available to build on).
4. Phase 4 as polish.
