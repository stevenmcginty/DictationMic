// Files handed to the app by Android rather than by the page.
//
// Two of them, and inside the APK they arrive the same way. A screenshot shared
// from the system share sheet, and a photo picked with "+ Image" / "+ File" —
// the shell answers that file input with its own picker so it can read the
// bytes where it has a ContentResolver, instead of leaving the page holding a
// File with no type and nothing in it.
//
// Either way the bytes wait in SharedInbox on the Kotlin side and the page
// pulls them across, a chunk at a time, then saves them through the ordinary
// saveAnyFiles path — so a shared screenshot becomes exactly the note a pasted
// or dropped one does.
//
// In a browser tab none of this exists: the PWA's share sheet goes through the
// manifest's share_target and the service worker (see sw.js + drainSharedFiles
// in main.js), and a file input is just a file input.

const N = () => window.DictationMicNative;

// 24 MB is the shell's own cap; at 288 KB a chunk that's 84, so this only ever
// stops a runaway, never a real file.
const MAX_CHUNKS = 128;

let draining = false;

export function wireShellShare(app) {
  if (!N() || typeof N().sharedFiles !== "function") return;   // older shell
  const shell = (window.DictationMicShell = window.DictationMicShell || {});
  shell.shared = () => { drain(app); };
  // Tell the shell there's someone home. Until it hears this it leaves the
  // file picker alone, so installing a new APK over a page the phone hasn't
  // refreshed yet can't leave "+ Image" doing nothing.
  try { N().sharedReady(); } catch { }
  drain(app);                        // whatever was shared before the page woke
}

// One at a time, and re-checking the queue at the end: a share can land while
// the previous one is still being read.
async function drain(app) {
  if (draining) return;
  draining = true;
  try {
    for (;;) {
      let items;
      try { items = JSON.parse(N().sharedFiles() || "[]"); }
      catch { return; }
      if (!items.length) return;
      const files = [];
      for (const item of items) {
        try {
          files.push(await readItem(item));
        } catch {
          app?.toast(`Couldn't read ${item.name || "that file"}`);
        }
        // Claimed either way — a file we can't read is one we'd fail to read
        // again on every drain from now on.
        try { N().clearShared(item.id); } catch { }
      }
      if (files.length) await app.saveAnyFiles(files);
    }
  } finally {
    draining = false;
  }
}

// Each chunk is base64 of a whole number of 3-byte groups, so joining them
// gives one valid base64 string — no re-encoding, no padding in the middle.
async function readItem(item) {
  const parts = [];
  for (let i = 0; i < MAX_CHUNKS; i++) {
    const b64 = N().sharedChunk(item.id, i);
    if (!b64) break;
    parts.push(b64);
  }
  if (!parts.length) throw new Error("empty");
  const mime = item.mime || "application/octet-stream";
  const blob = await (await fetch(`data:${mime};base64,${parts.join("")}`)).blob();
  return new File([blob], item.name || "shared", { type: mime });
}
