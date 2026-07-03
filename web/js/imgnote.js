// Image notes: a note whose body is a data:image/... URL. That one trick
// means images ride every existing pipe — IndexedDB, the outbox, RTDB sync,
// the desktop's .txt files — with zero changes to any of them. This module
// owns making those bodies (compressed hard, phones shoot 4MB photos) and
// turning them back into blobs for copy/share.

export const MAX_IMAGE_BYTES = 600 * 1024;   // hard cap AFTER compression
const MAX_SOURCE_BYTES = 40 * 1024 * 1024;   // refuse absurd inputs outright
const KEEP_ORIGINAL_BYTES = 250 * 1024;      // small enough? keep pixels as-is

const IMG_BODY_RE = /^data:image\/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$/;

export function isImageBody(body) {
  return typeof body === "string"
    && body.length < MAX_IMAGE_BYTES * 2      // cheap reject before the regex
    && IMG_BODY_RE.test(body);
}

export function imageKb(body) {
  return Math.max(1, Math.round((body.length * 3) / 4 / 1024));
}

export function dataUrlMime(body) {
  return (body.match(/^data:([^;]+);/) || [])[1] || "image/jpeg";
}

const blobToDataUrl = blob => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(r.result);
  r.onerror = () => reject(new Error("Couldn't read the image"));
  r.readAsDataURL(blob);
});

const loadImage = src => new Promise((resolve, reject) => {
  const img = new Image();
  img.onload = () => resolve(img);
  img.onerror = () => reject(new Error("That doesn't look like an image"));
  img.src = src;
});

// Compress a picked/dropped/pasted image file down to a data URL that fits
// MAX_IMAGE_BYTES. Drawing through <img> applies EXIF rotation for free.
export async function fileToImageBody(file) {
  if (!file.type.startsWith("image/")) throw new Error("Images only");
  if (file.size > MAX_SOURCE_BYTES) throw new Error("That image is too big");

  // small and already web-friendly: keep the original bytes (and animation)
  if (file.size <= KEEP_ORIGINAL_BYTES
      && /^image\/(png|jpeg|webp|gif)$/.test(file.type)) {
    const body = await blobToDataUrl(file);
    if (isImageBody(body)) return body;
  }

  const url = URL.createObjectURL(file);
  try {
    const img = await loadImage(url);
    const ladder = [
      { edge: 1600, q: 0.80 }, { edge: 1600, q: 0.65 }, { edge: 1280, q: 0.60 },
      { edge: 1024, q: 0.55 }, { edge: 800, q: 0.50 },
    ];
    for (const { edge, q } of ladder) {
      const scale = Math.min(1, edge / Math.max(img.naturalWidth, img.naturalHeight));
      const w = Math.max(1, Math.round(img.naturalWidth * scale));
      const h = Math.max(1, Math.round(img.naturalHeight * scale));
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff";                 // flatten transparency for JPEG
      ctx.fillRect(0, 0, w, h);
      ctx.drawImage(img, 0, 0, w, h);
      const blob = await new Promise(res => canvas.toBlob(res, "image/jpeg", q));
      if (blob && blob.size <= MAX_IMAGE_BYTES) {
        const body = await blobToDataUrl(blob);
        if (isImageBody(body)) return body;
      }
    }
    throw new Error("Couldn't shrink that image enough");
  } finally {
    URL.revokeObjectURL(url);
  }
}

// data URL -> Blob of its own mime (for share/download)
export function imageBodyToBlob(body) {
  const [meta, b64] = body.split(",", 2);
  const mime = dataUrlMime(body);
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

// data URL -> PNG blob (the only mime the async Clipboard API accepts)
export async function imageBodyToPngBlob(body) {
  const img = await loadImage(body);
  const canvas = document.createElement("canvas");
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  canvas.getContext("2d").drawImage(img, 0, 0);
  const blob = await new Promise(res => canvas.toBlob(res, "image/png"));
  if (!blob) throw new Error("Couldn't convert the image");
  return blob;
}

export function imageBodyToFile(body, title) {
  const mime = dataUrlMime(body);
  const ext = mime === "image/png" ? "png" : mime === "image/webp" ? "webp"
    : mime === "image/gif" ? "gif" : "jpg";
  const name = (title || "image").replace(/[^\w\- ]+/g, "").trim() || "image";
  return new File([imageBodyToBlob(body)], `${name}.${ext}`, { type: mime });
}

// "Photo 3 Jul 17.42" — matches the desktop's sanitize rules (no colon)
export function photoTitle(prefix = "Photo") {
  const d = new Date();
  const mon = d.toLocaleDateString(undefined, { month: "short" });
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${prefix} ${d.getDate()} ${mon} ${hh}.${mm}`;
}
