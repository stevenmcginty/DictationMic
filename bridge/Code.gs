/**
 * DictationMic calendar bridge -- Google Apps Script.
 *
 * Runs in the owner's own Google account on a 1-minute time trigger. It spots
 * new/changed Google Calendar events and writes the note straight into the
 * DictationMic Firebase Realtime Database, so every device sees a calendar
 * event as a note even while the Windows laptop ("the pill") is asleep.
 *
 * This is the PRIMARY importer; the pill is the fallback (it defers importing
 * brand-new events by a few minutes to give this bridge first crack).
 *
 * Auth: every RTDB call sends the script owner's OAuth token as a Bearer
 * header. That works because the owner of this script is also the Owner of the
 * dictationmic-sync Firebase/GCP project -- the token (with the firebase.database
 * scope declared in appsscript.json) is granted admin access to the database.
 * No client secret, refresh token or API key lives in this file.
 */

var DB_URL = "https://dictationmic-sync-default-rtdb.europe-west1.firebasedatabase.app";
var UID = "wcDpO74VdugzlbhJ54Um74dx6ph2";
var NOTES_BASE = "/users/" + UID + "/notes";
var TZ = "Europe/London";              // matches the Windows app's timezone

// ---------------------------------------------------------------------------
// setup / teardown -- run these by hand from the editor
// ---------------------------------------------------------------------------

/**
 * One-time install. Run this once (approve the consent screen when asked).
 * Clears any existing trigger, sets the poll window to NOW so old events are
 * never backfilled, then installs the every-minute trigger.
 */
function setup() {
  var removed = deleteAllTriggers();
  // Start the clock now -- poll() only looks at events changed AFTER this, so
  // the calendar you already have is left alone.
  var now = nowIsoUtc();
  PropertiesService.getScriptProperties().setProperty("lastPoll", now);
  ScriptApp.newTrigger("poll").timeBased().everyMinutes(1).create();
  console.log("DictationMic bridge is ON. Polling every minute from " + now
              + ". (" + removed + " old trigger(s) cleared.)");
}

/** Turn the bridge off: delete its triggers. Safe to run any time. */
function teardown() {
  var removed = deleteAllTriggers();
  console.log("DictationMic bridge is OFF. " + removed + " trigger(s) removed.");
}

function deleteAllTriggers() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    ScriptApp.deleteTrigger(triggers[i]);
  }
  return triggers.length;
}

// ---------------------------------------------------------------------------
// poll -- the worker, fired every minute by the trigger
// ---------------------------------------------------------------------------

/**
 * Fast in the common "nothing changed" case: one Calendar API call, then out.
 * Minute triggers on a consumer account share a ~90 min/day total-runtime
 * quota, so we do the least possible work when there's no news.
 */
function poll() {
  var props = PropertiesService.getScriptProperties();
  var lastPoll = props.getProperty("lastPoll");
  if (!lastPoll) {
    // No window yet (setup() never ran, or properties were cleared). Arm the
    // clock and skip this tick so we don't backfill the whole calendar.
    props.setProperty("lastPoll", nowIsoUtc());
    return;
  }

  // Capture the window's upper bound BEFORE listing, so an event created while
  // this call runs is picked up next minute rather than skipped.
  var pollStarted = nowIsoUtc();

  var resp = Calendar.Events.list("primary", {
    updatedMin: lastPoll,
    maxResults: 100,
    showDeleted: true      // so deletions (status "cancelled") reach us too
  });
  var items = (resp && resp.items) || [];
  if (!items.length) {
    // Nothing changed -- advance the window and return. This is the hot path.
    props.setProperty("lastPoll", pollStarted);
    return;
  }

  var imported = readJsonProp(props, "imported");   // eventId -> noteId
  var now = Date.now();

  for (var i = 0; i < items.length; i++) {
    var ev = items[i];
    var eid = ev.id;
    if (!eid) continue;

    // Already a bridge-imported event: a move or delete. Refresh its note's
    // calendar chip with a PATCH that carries no updatedAt, so the note keeps
    // its place in the list instead of jumping to the top.
    if (imported.hasOwnProperty(eid)) {
      patchCalendar(imported[eid], ev);
      continue;
    }

    // Import filters -- the same ones the pill applies.
    if (ev.status !== "confirmed") continue;          // includes deletes we don't own
    var start = parseSide(ev.start);
    if (start === null) continue;                     // no usable start time
    if (ev.recurringEventId) continue;                // recurring instances are out of scope
    if ((ev.description || "").indexOf("dictated with DictationMic") !== -1) {
      continue;                                       // an event the pill itself made
    }
    if (start.ms < now - 3600 * 1000) continue;       // more than an hour in the past

    // Dedup against other importers (the pill, or another device). If a note
    // for this event already exists, adopt its id and move on.
    var existing = findNoteByEventId(eid);
    if (existing) {
      imported[eid] = existing;
      continue;
    }

    // Import. A PUT (not PATCH) makes the realtime stream deliver the whole
    // record to listening devices in a single event.
    var noteId = Utilities.getUuid().replace(/-/g, "");   // 32 hex -- matches the app's ids
    var summary = ((ev.summary || "").trim()) || "Event";
    var whenLine = buildWhenLine(start);
    var record = {
      title: summary,
      body: summary + "\n" + whenLine + "\n\nAdded in Google Calendar",
      createdAt: now,
      deleted: false,
      origin: "gcal",
      updatedAt: {".sv": "timestamp"},
      calendar: buildCalendar(ev, start, now)
    };
    putNote(noteId, record);        // throws on non-2xx -> lastPoll not saved, retried
    imported[eid] = noteId;
  }

  // Persist the dedup map and advance the window ONLY after the loop finished
  // without throwing: if a PUT failed above, lastPoll stays put and the whole
  // window is retried next minute (the dedup query stops duplicates).
  writeJsonProp(props, "imported", capMap(imported, 500));
  props.setProperty("lastPoll", pollStarted);
}

// ---------------------------------------------------------------------------
// calendar / event helpers
// ---------------------------------------------------------------------------

/**
 * Parse one side (start or end) of an event into {ms, allDay}, or null.
 * - Timed events carry start.dateTime (RFC3339 with offset).
 * - All-day events carry start.date ("YYYY-MM-DD"); we take midnight in the
 *   script timezone (Europe/London), which is what the Windows app does. The
 *   all-day end.date is EXCLUSIVE -- we store it as parsed, same as the app.
 */
function parseSide(raw) {
  if (!raw) return null;
  if (raw.date) {
    var p = String(raw.date).split("-");
    var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));  // midnight, script tz
    return {ms: d.getTime(), allDay: true};
  }
  if (raw.dateTime) {
    var t = new Date(raw.dateTime).getTime();
    if (isNaN(t)) return null;
    return {ms: t, allDay: false};
  }
  return null;
}

/** Build the calendar object in the exact shape the app expects. */
function buildCalendar(ev, start, now) {
  var endSide = parseSide(ev.end);
  return {
    status: "ok",
    provider: "google",
    eventId: ev.id,
    link: ev.htmlLink || "",
    start: start.ms,
    end: endSide ? endSide.ms : start.ms,   // missing end -> use start
    allDay: start.allDay,
    addedAt: now,
    source: "gcal"
  };
}

/**
 * The human "when" line, matching the app's format exactly:
 *   "today" / "tomorrow" / "Wed 15 Jul"  (day-of-month not zero-padded)
 * then ", all day" for all-day events, or " at HH:mm" (24h) for timed ones.
 * today/tomorrow are relative to run time, in Europe/London.
 */
function buildWhenLine(start) {
  var startDate = new Date(start.ms);
  var day = Utilities.formatDate(startDate, TZ, "EEE d MMM");   // e.g. "Wed 15 Jul"
  var evYmd = Utilities.formatDate(startDate, TZ, "yyyy-MM-dd");
  var todayYmd = Utilities.formatDate(new Date(), TZ, "yyyy-MM-dd");
  // Tomorrow = the calendar day after today, worked out from a mid-day anchor
  // so a DST change near midnight can't shift the date.
  var tp = todayYmd.split("-");
  var anchor = new Date(Number(tp[0]), Number(tp[1]) - 1, Number(tp[2]), 12, 0, 0);
  var tomorrowYmd = Utilities.formatDate(
    new Date(anchor.getTime() + 24 * 3600 * 1000), TZ, "yyyy-MM-dd");

  if (evYmd === todayYmd) day = "today";
  else if (evYmd === tomorrowYmd) day = "tomorrow";

  if (start.allDay) return day + ", all day";
  return day + " at " + Utilities.formatDate(startDate, TZ, "HH:mm");
}

// ---------------------------------------------------------------------------
// RTDB access -- every call carries the owner's OAuth token as a Bearer header
// ---------------------------------------------------------------------------

function rtdbFetch(url, options) {
  options = options || {};
  options.muteHttpExceptions = true;
  options.headers = options.headers || {};
  options.headers.Authorization = "Bearer " + ScriptApp.getOAuthToken();
  return UrlFetchApp.fetch(url, options);
}

/**
 * Has any device already made a note for this event? Returns that note's id,
 * or null. Uses an orderBy/equalTo query on calendar/eventId -- the orderBy and
 * equalTo values include their own double quotes, and the whole thing is
 * URL-encoded.
 *
 * A `.indexOn` for "calendar/eventId" is being added to the database rules. If
 * the query comes back as an index error (before that deploys), we log it and
 * treat it as "not found" so the bridge still imports in the meantime.
 */
function findNoteByEventId(eid) {
  var url = DB_URL + NOTES_BASE + ".json"
    + "?orderBy=" + encodeURIComponent('"calendar/eventId"')
    + "&equalTo=" + encodeURIComponent('"' + eid + '"');
  var resp = rtdbFetch(url, {method: "get"});
  var code = resp.getResponseCode();
  var text = resp.getContentText();
  if (code < 200 || code >= 300) {
    console.error("dedup query " + code + ": " + text.slice(0, 200));
    return null;                       // e.g. index not defined yet -> treat as not found
  }
  var data = JSON.parse(text || "null");
  if (data && typeof data === "object") {
    var keys = Object.keys(data);
    if (keys.length) return keys[0];
  }
  return null;
}

/**
 * Create a note with a full PUT. On any non-2xx we log the code + first ~200
 * chars of body and THROW, so poll() doesn't record the event as imported and
 * doesn't advance lastPoll -- the next run retries it.
 */
function putNote(noteId, record) {
  var url = DB_URL + NOTES_BASE + "/" + noteId + ".json";
  var resp = rtdbFetch(url, {
    method: "put",
    contentType: "application/json",
    payload: JSON.stringify(record)
  });
  var code = resp.getResponseCode();
  if (code < 200 || code >= 300) {
    console.error("note PUT " + code + ": " + resp.getContentText().slice(0, 200));
    throw new Error("note PUT failed (" + code + ")");
  }
}

/**
 * Refresh an already-imported note's calendar chip after a move or delete.
 * We read the current calendar object first, then merge over it, so fields the
 * app owns (eventId, provider, addedAt, source) survive -- a note-level PATCH
 * replaces the whole `calendar` subtree, so we must send it complete. No
 * updatedAt is sent, so the note keeps its place in the list. Non-fatal: a
 * failed chip refresh just logs and lets the poll carry on.
 */
function patchCalendar(noteId, ev) {
  var base = getNoteCalendar(noteId);
  if (base === null) return;           // couldn't read it / no chip -- leave it alone
  var cal = shallowCopy(base);
  if (ev.status === "cancelled") {
    cal.status = "cancelled";
  } else {
    var start = parseSide(ev.start);
    if (start === null) return;        // no sensible new time -- nothing to do
    var endSide = parseSide(ev.end);
    cal.status = "ok";
    cal.start = start.ms;
    cal.end = endSide ? endSide.ms : start.ms;
    cal.allDay = start.allDay;
    cal.link = ev.htmlLink || cal.link || "";
  }
  cal.eventId = ev.id;                 // keep the join key present no matter what

  var url = DB_URL + NOTES_BASE + "/" + noteId + ".json";
  var resp = rtdbFetch(url, {
    method: "patch",
    contentType: "application/json",
    payload: JSON.stringify({calendar: cal, origin: "gcal"})
  });
  var code = resp.getResponseCode();
  if (code < 200 || code >= 300) {
    console.error("calendar PATCH " + code + ": " + resp.getContentText().slice(0, 200));
  }
}

/** The current calendar object on a note, or null if absent / unreadable. */
function getNoteCalendar(noteId) {
  var url = DB_URL + NOTES_BASE + "/" + noteId + "/calendar.json";
  var resp = rtdbFetch(url, {method: "get"});
  var code = resp.getResponseCode();
  if (code < 200 || code >= 300) {
    console.error("calendar GET " + code + ": " + resp.getContentText().slice(0, 200));
    return null;
  }
  var data = JSON.parse(resp.getContentText() || "null");
  return (data && typeof data === "object") ? data : null;
}

// ---------------------------------------------------------------------------
// small utilities
// ---------------------------------------------------------------------------

/** RFC3339 UTC, e.g. "2026-07-13T10:00:00Z". */
function nowIsoUtc() {
  return Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd'T'HH:mm:ss'Z'");
}

function readJsonProp(props, key) {
  var raw = props.getProperty(key);
  if (!raw) return {};
  try {
    var v = JSON.parse(raw);
    return (v && typeof v === "object") ? v : {};
  } catch (e) {
    return {};
  }
}

function writeJsonProp(props, key, value) {
  props.setProperty(key, JSON.stringify(value));
}

/** Cap the eventId->noteId map at `max`, dropping the oldest-inserted keys. */
function capMap(map, max) {
  var keys = Object.keys(map);
  if (keys.length <= max) return map;
  var out = {};
  for (var i = keys.length - max; i < keys.length; i++) {
    out[keys[i]] = map[keys[i]];
  }
  return out;
}

function shallowCopy(o) {
  var out = {};
  for (var k in o) {
    if (Object.prototype.hasOwnProperty.call(o, k)) out[k] = o[k];
  }
  return out;
}
