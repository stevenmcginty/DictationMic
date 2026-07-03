// Tiny promisified IndexedDB layer. Three stores:
//   notes  — local mirror of the cloud records (the UI's source of truth)
//   outbox — pending uploads; survives reloads and force-closes, which is
//            what makes airplane-mode dictation durable
//   meta   — key/value odds and ends (tombstones we've applied, etc.)

const DB_NAME = "dictmic";
const DB_VERSION = 1;

let _db = null;

export function openDb() {
  if (_db) return Promise.resolve(_db);
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("notes")) {
        db.createObjectStore("notes", { keyPath: "id" });
      }
      if (!db.objectStoreNames.contains("outbox")) {
        db.createObjectStore("outbox", { autoIncrement: true });
      }
      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta");
      }
    };
    req.onsuccess = () => { _db = req.result; resolve(_db); };
    req.onerror = () => reject(req.error);
  });
}

function tx(store, mode, fn) {
  return openDb().then(db => new Promise((resolve, reject) => {
    const t = db.transaction(store, mode);
    const result = fn(t.objectStore(store));
    t.oncomplete = () => resolve(result.__value ?? result);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);
  }));
}

function req(r) {
  const holder = {};
  r.onsuccess = () => { holder.__value = r.result; };
  return holder;
}

export const notesDb = {
  all: () => tx("notes", "readonly", s => req(s.getAll())),
  get: id => tx("notes", "readonly", s => req(s.get(id))),
  put: note => tx("notes", "readwrite", s => req(s.put(note))),
  del: id => tx("notes", "readwrite", s => req(s.delete(id))),
};

export const outboxDb = {
  all: () => tx("outbox", "readonly", s => {
    const out = { __value: [] };
    const r = s.openCursor();
    r.onsuccess = () => {
      const c = r.result;
      if (c) { out.__value.push({ key: c.key, ...c.value }); c.continue(); }
    };
    return out;
  }),
  add: entry => tx("outbox", "readwrite", s => req(s.add(entry))),
  del: key => tx("outbox", "readwrite", s => req(s.delete(key))),
};

export const metaDb = {
  get: key => tx("meta", "readonly", s => req(s.get(key))),
  set: (key, value) => tx("meta", "readwrite", s => req(s.put(value, key))),
};
