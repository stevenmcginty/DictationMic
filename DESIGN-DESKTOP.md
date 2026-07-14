# DictationMic desktop shell — design spec

Companion to `web/DESIGN.md`. That file specs the notes PWA; this one specs
every pixel the Python app itself draws: the pill, the right-click command
card, the screenshot badge + shelf, toasts, and the full-screen app window.

## Named aesthetic identity
The language is **"Obsidian Capsule"**, same as the web app — the desktop
shell is where the language was born, so it must be its best expression.
One big idea: the pill is a machined obsidian pebble that *floats* over any
wallpaper and *wakes up lime*. Every other surface (menu, shelf, toast) is
a matte card cut from the same stone: near-black with a green undertone,
bone ink, hairline edges, and a single volt-lime accent that only ever
means "live / armed / fresh". Mood: nocturnal, tactile, quietly technical,
instant. It is NOT a grey Win32 tray app, NOT material design, NOT a neon
gamer overlay.

**Elevation is the light-background answer** (à la Dynamic Island): the
pill, toasts, tooltip and badges are per-pixel-alpha layered windows with a
two-layer drop shadow — a tight key shadow + a wide soft ambient. On light
wallpaper the shadow defines the edge; on dark, the bone hairline does.
Never a chroma-key fringe.

## Color palette (strict)
- Capsule body: vertical gradient `#1F2220` (top) → `#0E100E` (bottom);
  machined top highlight: 1px inner arc, white at 13%, fading out by
  mid-height (milled stone — no glossy ellipse, no plastic)
- Elevation (pill & cards, layered mode): key shadow black ~27% (offset
  ~2px, blur ~3px) + ambient black ~18% (offset ~5px, blur ~9–12px);
  live states add an outer glow (lime listening/drop, ice flash) that
  breathes with the rim
- Card surface (menu / shelf / toast): `#131512`; raised row hover
  `#1A1D18`; PIL cards get a whisper of gradient `#1A1D19 → #131512`
- Ink: `#ECEEE7`; muted `#878C7F` (≈52%); dim `#5C6156` (≈32%);
  toast detail line `#9FA498`
- Hairline on card: `#23251F` (Tk) / ink at 14% (PIL cards)
- Accent — the ONLY loud color: volt lime `#B6EE3F`
  (voice bars, armed rim, fresh badge ring, radio/check marks, hero button)
- Rare secondaries: ice `#56C5FF` = "caught/saved" only;
  signal red `#FF5C48` = errors / destructive rows only
- Sleeping dots: ink at 33% (idle) / 59% (hover) / 18% (model loading)
- Chroma-key fallback only: transparent matte `#010203`

## Typography
- UI: "Segoe UI Variable Display" (Win11-native, echoes Space Grotesk),
  fallback "Segoe UI". Rows 10pt; hero button 10pt semibold.
- Data/meta/eyebrows: "Cascadia Mono" (Win11-native, echoes JetBrains Mono),
  fallback "Consolas". Eyebrows 7.5pt UPPERCASE, letter-spaced by
  hair-space injection (Tk has no letter-spacing) — `S H O T S`.
- Pill percent (download): Segoe UI Semibold at 40% of pill height, PIL.

## Layout & structure
1. **The pill** (84×30 body default, supersampled ×3, premultiplied LANCZOS;
   the layered window is padded ~14/9/16px around the body for its shadow):
   machined capsule floating on the two-layer shadow.
   - Idle: 3 sleeping dots, centered — the brand mark at rest (matches web).
   - Hover: dots warm, rim turns volt (55%) — "armed".
   - Listening: 11 volt bars w/ under-glow (22% at 1.9× width), rim pulses,
     and a soft lime outer glow breathes with it.
   - Finishing: 3 volt dots waving.
   - Drop-hover: full volt ring + glow + arrow-into-tray.
     Flash: ice ring + glow + tick.
   - Downloading: track + volt fill + percent.
   - Toast (PIL card, click-through): icon disc (ice tick = saved, volt
     dots = info, red bang = error) + semibold title + one muted detail
     line, radius 11, hairline + elevation. Tooltip: the same card with
     gesture/action rows (ink semibold / muted).
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
  no slide (windows are screen-edge-clamped). Close: instant. Layered
  toasts/tooltips fade via the ULW blend alpha (same 110ms ramp).
- Pill listening rim + outer glow: sine pulse, quantized to 4 steps so
  bodies (and glow underlays) cache.
- Copied tile: ice ring for 450ms. Toast: fade-in 110ms, auto-expire,
  click-through (never eats a click meant for what's beneath).
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
- **Per-pixel alpha**: pill, badges, toast and tooltip paint via
  `UpdateLayeredWindow` (ctypes; premultiplied BGRA DIB). The wrapper hwnd
  only exists once a window is MAPPED — `update()`/deiconify before
  `layered_ready`. Frames downsample in premultiplied `RGBa` so the AA
  edge never fringes. First blit is verified at startup; any failure (or
  `settings.layered_ui: false`) falls back to the old Tk
  `-transparentcolor` chroma-key path, flat frames, label + PhotoImage.
  Measured: listening render+blit ≈ 2.6ms/frame at the 33ms tick.
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
