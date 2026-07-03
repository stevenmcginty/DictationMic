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

export function debounce(fn, ms) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => { t = null; fn(...args); }, ms);
  };
  wrapped.flush = () => { if (t !== null) { clearTimeout(t); t = null; fn(); } };
  return wrapped;
}

export function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID().replaceAll("-", "");
  return [...crypto.getRandomValues(new Uint8Array(16))]
    .map(b => b.toString(16).padStart(2, "0")).join("");
}
