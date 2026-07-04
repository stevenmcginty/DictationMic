// File notes: a note whose body is data:<mime>;name=<urlencoded filename>;
// base64,<bytes> — documents (PDF, Word, Excel, …) riding the exact same
// pipes as image notes: IndexedDB, the outbox, RTDB sync, the desktop's
// .txt files. The ;name= parameter is what tells a file note apart from an
// image note (imgnote.js bodies never carry one) and is the filename used
// for open/share/download. Twin of the Python side in dropnotes.py.

export const MAX_FILE_BYTES = 7 * 1024 * 1024;   // b64 of 7 MB ≈ 9.4 MB,
                                                 // under RTDB's 10 MB cap

const FILE_BODY_RE = /^data:([\w.+-]+\/[\w.+-]+);name=([^;,]*);base64,/;

// Spreadsheets stay real file notes (never editable text) and open as a
// plain-text preview rather than downloading — same rule everywhere it's used.
export const SHEET_EXT_RE = /\.(csv|tsv)$/i;

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
