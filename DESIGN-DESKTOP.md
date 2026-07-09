# DictationMic desktop shell — design spec

Companion to `web/DESIGN.md`. That file specs the notes PWA; this one specs
every pixel the Python app itself draws: the pill, the right-click command
card, the screenshot badge + shelf, toasts, and the full-screen app window.

## Named aesthetic identity
The language is **"Obsidian Capsule"**, same as the web app — the desktop
shell is where the language was born, so it must be its best expression.
One big idea: the pill is a lacquered obsidian pebble that *wakes up lime*.
Every other surface (menu, shelf, toast) is a matte card cut from the same
stone: near-black with a green undertone, bone ink, hairline edges, and a
single volt-lime accent that only ever means "live / armed / fresh".
Mood: nocturnal, tactile, quietly technical, instant. It is NOT a grey
Win32 tray app, NOT material design, NOT a neon gamer overlay.

## Color palette (strict)
- Transparent matte key: `#010203` (Tk transparentcolor; AA flattens onto it)
- Capsule body: vertical lacquer `#1F2220` (top) → `#0E100E` (bottom);
  top sheen: white at 9% over the upper 45%, elliptical falloff;
  lower inner shade: black at 22% bottom band
- Card surface (menu / shelf / toast): `#131512`; raised row hover `#1A1D18`
- Ink: `#ECEEE7`; muted `#878C7F` (≈52%); dim `#5C6156` (≈32%)
- Hairline on card: `#23251F` (ink at 8% flattened)
- Accent — the ONLY loud color: volt lime `#B6EE3F`
  (voice bars, armed rim, fresh badge ring, radio/check marks, hero button)
- Rare secondaries: ice `#56C5FF` = "caught/saved" flash only;
  signal red `#FF5C48` = destructive rows only
- Sleeping dots: ink at 30% (idle) / 55% (hover) / 18% (model loading)

## Typography
- UI: "Segoe UI Variable Display" (Win11-native, echoes Space Grotesk),
  fallback "Segoe UI". Rows 10pt; hero button 10pt semibold.
- Data/meta/eyebrows: "Cascadia Mono" (Win11-native, echoes JetBrains Mono),
  fallback "Consolas". Eyebrows 7.5pt UPPERCASE, letter-spaced by
  hair-space injection (Tk has no letter-spacing) — `S H O T S`.
- Pill percent (download): Segoe UI Semibold at 40% of pill height, PIL.

## Layout & structure
1. **The pill** (84×30 default, supersampled ×3, LANCZOS): lacquered capsule.
   - Idle: 3 sleeping dots, centered — the brand mark at rest (matches web).
   - Hover: dots warm, rim turns volt (55%) — "armed".
   - Listening: 11 volt bars w/ under-glow (22% at 1.9× width), rim pulses.
   - Finishing: 3 volt dots waving.
   - Drop-hover: full volt ring + arrow-into-tray. Flash: ice ring + tick.
   - Downloading: track + volt fill + percent.
2. **Command card** (right-click): hero capsule button "Open DictationMic"
   (volt on volt-12% fill, PIL-drawn) at top — opens the full-screen app.
   Below: eyebrow-labelled sections (OUTPUT / NOTES / SCREENSHOTS / PHONE
   SYNC / SPEECH ENGINE / TALK KEY), 1px hairlines, radio `●`/check `✓` in
   volt, hints right-aligned in mono dim, destructive row in signal red.
3. **Shot badge**: 22px obsidian disc on the pill's right shoulder; count in
   mono; volt ring + volt numeral while unseen, hairline ring + bone once
   seen. Click = shelf ("the old notification bar").
4. **Shots shelf**: card with mono eyebrow header `S H O T S · N`, 4-col
   thumbnail grid (96×72, r9, hairline ring; hover = volt ring + ✕ disc;
   copied = ice ring flash), mono footer: hint left, actions right.
5. **Full-screen app**: the notes UI (text + image + file entries) in a
   chromeless Edge app-mode window (`--app=`), maximized — hosted app when
   phone sync is on, token-gated localhost otherwise. The web spec owns
   everything inside that window.

## Motion / interaction spec
- Card/shelf entrance: alpha 0→1 over ~110ms (5 steps via `after(16)`),
  no slide (windows are screen-edge-clamped). Close: instant.
- Pill listening rim: sine pulse, quantized to 4 alpha steps so bodies cache.
- Copied tile: ice ring for 450ms. Toast: fade-in 110ms, auto-expire.
- Nothing else moves. Reduced-motion equivalent: all fades are ≤110ms and
  skippable (window appears fully opaque if alpha unsupported).

## Responsive behavior
- Pill scales from `settings.size` (64–200px); every metric derives from
  height. Menu/shelf clamp to screen edges, open upward near the bottom.
- Multi-DPI: process is per-monitor DPI-aware v1; PIL renders at ×3.

## Tech notes
- Stack: Python 3 / Tk / PIL, zero new dependencies. Full-screen app =
  `msedge.exe --app=<url> --start-maximized` (App Paths lookup, Program
  Files fallback, `webbrowser` last resort) — no pywebview, nothing for
  Smart App Control to block.
- Fonts: system-native only (Segoe UI Variable, Cascadia Mono ship with
  Win11); PIL glyphs from `C:\Windows\Fonts`.
- **Performance contract** (the redesign is also a de-lag):
  - `draw()` must be a no-op when the frame key hasn't changed — no
    per-tick `label.configure` while idle.
  - Animated frames (listening/dots/downloading) encode as raw PPM (P6),
    never PNG+base64 — no zlib, no base64, no PNG decode at 30fps.
  - Tick runs at 33ms only while animating or a modifier is held
    (push-to-talk timing); 100ms otherwise. Clipboard watch is
    timestamp-based (0.5s), independent of tick rate.
  - Shelf thumbnails cache by `(path, mtime)` at class level — opening the
    shelf twice must not re-render a single tile.
- Screenshots caught by the shelf are also saved as image notes
  (`shots_to_notes`, default on) — the exact `imgnote.js` data-URL
  contract, so they appear in the full app and sync to the phone.
