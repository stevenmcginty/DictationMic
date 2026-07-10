// Shared helpers.

// JS port of app.py's note_title_from / sanitize_title.
const RESERVED = new Set(["CON", "PRN", "AUX", "NUL",
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`)]);

export function sanitizeTitle(name) {
  name = String(name)
    .replace(/[<>:"/\\|?*\x00-\x1f]+/g, " ")
    .replace(/\s{2,}/g, " ")
    .replace(/^[ .]+|[ .]+$/g, "");
  if (RESERVED.has(name.toUpperCase())) name = "Note " + name;
  return name.slice(0, 80).replace(/^[ .]+|[ .]+$/g, "");
}

export function noteTitleFrom(text) {
  const title = sanitizeTitle(text.split(/\s+/).filter(Boolean).slice(0, 7).join(" "));
  return title.slice(0, 60).replace(/[ .,!?;:]+$/g, "") || "Note";
}

export function relTime(ms) {
  if (!ms) return "";
  const s = (Date.now() - ms) / 1000;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 172800) return "yesterday";
  const d = new Date(ms);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) +
    (d.getFullYear() !== new Date().getFullYear() ? ` ${d.getFullYear()}` : "");
}

// Day headings the notes list groups under. dayKey collapses a timestamp
// to its calendar day; dayHeading renders it as "Tuesday 1st of June"
// (year added only when it isn't this year's).
export function dayKey(ms) {
  return new Date(ms).toDateString();
}

export function dayHeading(ms) {
  const d = new Date(ms);
  const n = d.getDate();
  const suffix = n % 10 === 1 && n !== 11 ? "st"
    : n % 10 === 2 && n !== 12 ? "nd"
    : n % 10 === 3 && n !== 13 ? "rd" : "th";
  const weekday = d.toLocaleDateString("en-GB", { weekday: "long" });
  const month = d.toLocaleDateString("en-GB", { month: "long" });
  const year = d.getFullYear() !== new Date().getFullYear() ? ` ${d.getFullYear()}` : "";
  return `${weekday} ${n}${suffix} of ${month}${year}`;
}

// The little event chip on a calendared note: "Today · 15:00 · in 20 min".
// cal is the synced calendar field the laptop stamps on the note.
export function calendarLabel(cal) {
  if (!cal) return "";
  if (cal.status !== "ok") return "calendar event didn't make it";
  const d = new Date(cal.start);
  const now = new Date();
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  const day = sameDay(d, now) ? "Today"
    : sameDay(d, new Date(now.getTime() + 86400000)) ? "Tomorrow"
    : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  if (cal.allDay) return `${day} · all day`;
  const t = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const mins = Math.round((cal.start - Date.now()) / 60000);
  return `${day} · ${t}` + (mins > 0 && mins <= 60 ? ` · in ${mins} min` : "");
}

export function debounce(fn, ms) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => { t = null; fn(...args); }, ms);
  };
  wrapped.flush = () => { if (t !== null) { clearTimeout(t); t = null; fn(); } };
  wrapped.cancel = () => { if (t !== null) { clearTimeout(t); t = null; } };
  return wrapped;
}

export function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID().replaceAll("-", "");
  return [...crypto.getRandomValues(new Uint8Array(16))]
    .map(b => b.toString(16).padStart(2, "0")).join("");
}
