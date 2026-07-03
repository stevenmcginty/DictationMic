// File notes: a note whose body is data:<mime>;name=<urlencoded filename>;
// base64,<bytes> — documents (PDF, Word, Excel, …) riding the exact same
// pipes as image notes: IndexedDB, the outbox, RTDB sync, the desktop's
// .txt files. The ;name= parameter is what tells a file note apart from an
// image note (imgnote.js bodies never carry one) and is the filename used
// for open/share/download. Twin of the Python side in dropnotes.py.

export const MAX_FILE_BYTES = 7 * 1024 * 1024;   // b64 of 7 MB ≈ 9.4 MB,
                                                 // under RTDB's 10 MB cap

const FILE_BODY_RE = /^data:([\w.+-]+\/[\w.+-]+);name=([^;,]*);base64,/;

export function isFileBody(body) {
  return typeof body === "string" && FILE_BODY_RE.test(body.slice(0, 400));
}

export function fileMeta(body) {
  const m = body.slice(0, 400).match(FILE_BODY_RE) || [];
  let name = "file";
  try { name = decodeURIComponent(m[2] || "") || "file"; } catch { /* keep */ }
  const bytes = Math.max(1, Math.round((body.length - body.indexOf(",") - 1) * 3 / 4));
  const ext = (name.match(/\.(\w+)$/) || [])[1] || "";
  return { mime: m[1] || "application/octet-stream", name, bytes,
           ext: ext.toUpperCase() };
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} kB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function fileBodyToBlob(body) {
  const { mime } = fileMeta(body);
  const bin = atob(body.slice(body.indexOf(",") + 1));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

export function fileBodyToFile(body) {
  const { mime, name } = fileMeta(body);
  return new File([fileBodyToBlob(body)], name, { type: mime });
}

// Types the in-app viewer shows itself (as a table or plain text) — from a
// blob tab the browser would just download these instead of rendering them.
// PDFs and images stay on the new-tab path, which browsers render natively.
const TEXT_VIEW_EXTS = new Set(["CSV", "TXT", "LOG", "MD", "JSON", "XML",
  "YAML", "YML", "INI", "TSV", "HTML", "CSS", "JS", "TS", "PY"]);

export function isViewableText(meta) {
  return TEXT_VIEW_EXTS.has(meta.ext) || (meta.mime || "").startsWith("text/");
}

export function fileBodyToText(body) {
  const bin = atob(body.slice(body.indexOf(",") + 1));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder("utf-8").decode(bytes);
}

// Small quote-aware CSV parser -> array of rows (arrays of cells).
export function parseCsv(text, maxRows = 1000) {
  const rows = [];
  let row = [], cell = "", q = false;
  const push = () => { row.push(cell.replace(/\r$/, "")); cell = ""; };
  for (let i = 0; i < text.length && rows.length < maxRows; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; } else q = false;
      } else cell += c;
    } else if (c === '"') q = true;
    else if (c === ",") push();
    else if (c === "\n") { push(); rows.push(row); row = []; }
    else cell += c;
  }
  if (cell !== "" || row.length) { push(); rows.push(row); }
  return rows.filter(r => r.some(c => c !== ""));
}

const blobToDataUrl = blob => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(r.result);
  r.onerror = () => reject(new Error("Couldn't read that file"));
  r.readAsDataURL(blob);
});

// A picked/dropped document -> file note body. Rejects media and oversize.
export async function fileToFileBody(file) {
  if (/^(video|audio)\//.test(file.type || "")) {
    throw new Error(`${file.name} — videos and audio don't sync, documents only`);
  }
  if (file.size > MAX_FILE_BYTES) {
    throw new Error(`${file.name} is ${fmtBytes(file.size)} — files up to 7 MB can sync`);
  }
  if (!file.size) throw new Error(`${file.name} is empty`);
  const dataUrl = await blobToDataUrl(file);
  const b64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
  const mime = file.type || "application/octet-stream";
  return `data:${mime};name=${encodeURIComponent(file.name || "file")};base64,${b64}`;
}
