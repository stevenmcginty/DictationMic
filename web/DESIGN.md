# DictationMic Notes — design spec

## Named aesthetic identity
A notes PWA for DictationMic (desktop via localhost, phone via Firebase
Hosting). The named design language is **"Obsidian Capsule"** — the entire UI
is derived from the dictation pill itself: a near-black lacquered ground,
capsule-shaped controls, sleeping grey dots for idle states, and rising lime
voice-bars as the one signature motif. Mood: nocturnal, tactile, quietly
technical. It is NOT a white productivity app, NOT material design, NOT a
generic dark theme with blue buttons.

## Color palette (strict)
- Background: `#0B0C0A` (obsidian with a green undertone)
- Surface / cards: `#131512`; raised surface `#1A1D18`
- Ink: `#ECEEE7` (warm bone white)
- Muted text: ink at 52% → `rgba(236,238,231,.52)`
- Hairlines: `rgba(236,238,231,.08)`
- Accent (the ONLY loud color — voice bars, unsynced dot, active states,
  mic ring): volt lime `#B6EE3F`
- Rare secondary, destructive confirm only: `#FF5C48`

## Typography
- Display/UI: "Space Grotesk" (vendored variable woff2, 300–700).
  Fallback: system-ui stack.
- Data/meta: "JetBrains Mono" (vendored variable woff2) — timestamps, counts,
  sync status, eyebrows.
- View titles: `clamp(1.6rem, 5vw, 2.2rem)`, weight 600, `-0.03em`, lh 1.
- Eyebrow/labels: 10–11px, UPPERCASE, `letter-spacing: .28em`, mono, muted.
- Note body (editor): Space Grotesk 400, 16px/1.65, max-width 68ch.
- List title: 15px/600; snippet 13px muted, 2-line clamp.

## Layout & structure
1. Top bar (fixed, blurred obsidian): brand capsule (mini pill mark w/ 5 bars)
   + wordmark; centre search capsule (desktop); right sync-status capsule.
2. Desktop ≥900px: two panes — 340px list rail (hairline-separated rows) and
   the editor pane. Mobile: list is a route; note and mic are full-screen
   routes with a back chevron.
3. Note rows: title, mono relative time, 2-line snippet, lime dot when
   unsynced. Editor: inline-editable title, mono meta line (created · synced),
   body textarea, capsule actions (Copy / Delete→Sure?).
4. Mic FAB (phone only): 64px lime capsule bottom-centre → full-screen
   dictation view: live bars, interim text dimmed under committed transcript.
5. Ambient: 2.5% noise overlay, hairline rules, sleeping-dots empty state,
   mono count ticker in list header ("14 NOTES · 2 PENDING").

## Motion / interaction spec
- Entrance: list rows stagger in (12ms/row, translateY(6px)→0 + fade, 320ms,
  cubic-bezier(.2,.7,.2,1)); editor pane fades/slides 8px.
- Signature: brand-mark bars idle as 3 sleeping dots; while dictating they
  become 5 lime bars animating at staggered speeds. Mic screen scales that to
  a full-width 24-bar meter driven by mic volume (or a gentle loop fallback).
- Press states: capsules scale(.97), 120ms. Copy flashes "COPIED" in lime.
- Sync dot pulses (1.6s ease-in-out) while an item is pending upload.
- prefers-reduced-motion: no stagger/pulse/bar animation — bars render as a
  static row, everything appears instantly.

## Responsive behavior
- Desktop ≥900px: two panes, hover states, keyboard ('/' focuses search,
  Delete key on focused row, F2/click title to rename).
- <900px: single column routes, 48px touch targets, mic FAB, no hover-only
  affordances; safe-area insets respected (PWA standalone).

## Tech notes
- Framework-free HTML/CSS/vanilla ES modules. No build step. Same files serve
  from localhost (app.py) and Firebase Hosting.
- Fonts vendored in `fonts/` (@font-face, woff2). No CDN links anywhere.
- Data via adapter interface (adapters/local.js ↔ adapters/firebase.js).
- Runs fully offline on desktop; installable PWA (manifest + sw, hosted only).
