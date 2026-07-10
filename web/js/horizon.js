// The Horizon — a Trainline-style journey line under the top bar. The next
// seven days are stations on a track (today first, left to right, same on
// the phone); calendared notes sit above their day as small tiles. One tile
// opens its note; a day holding several shows the earliest plus a "+N" badge
// and pops the whole day out as a tile stack. Subtle by design: an empty week
// is just the quiet track.

const DAYS = 7;
const DAY_MS = 86400000;

// tile accents, assigned by order within a day so same-date events read apart;
// volt stays first so a lone event wears the app's own colour
const HUES = ["#B6EE3F", "#6FC3FF", "#C9A6FF", "#F4C752", "#FF8B7B"];

const midnight = ms => {
  const d = ms ? new Date(ms) : new Date();
  d.setHours(0, 0, 0, 0);
  return +d;
};

const dayLabel = (ms, i) => {
  const d = new Date(ms);
  return i === 0 ? "TODAY"
    : `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${d.getDate()}`.toUpperCase();
};

const timeLabel = cal => cal.allDay ? "all day"
  : new Date(cal.start).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

// an event is "done" once it's over (or an hour past start when there's no end)
const isDone = cal => Date.now() > (cal.end || cal.start + 3600000) && !cal.allDay;

export class Horizon {
  constructor(el) {
    this.el = el;
    this.notes = [];
    this.pop = null;
    el.addEventListener("click", e => this._onClick(e));
    document.addEventListener("click", e => {
      if (this.pop && !this.pop.contains(e.target) && !this.el.contains(e.target)) {
        this._closePop();
      }
    });
    addEventListener("resize", () => this._closePop());
  }

  render(notes) {
    this.notes = notes;
    this._closePop();
    const start = midnight();
    const buckets = Array.from({ length: DAYS }, () => []);
    for (const n of notes) {
      const c = n.calendar;
      if (!c || c.status !== "ok" || !c.start) continue;
      // bucket by calendar day, not raw ms, so a DST hop can't shift a stop
      const i = Math.round((midnight(c.start) - start) / DAY_MS);
      if (i >= 0 && i < DAYS) buckets[i].push(n);
    }
    for (const b of buckets) b.sort((a, z) => a.calendar.start - z.calendar.start);

    const row = document.createElement("div");
    row.className = "h-days";
    const line = document.createElement("div");
    line.className = "h-line";
    row.append(line);

    buckets.forEach((bucket, i) => {
      const dayMs = start + i * DAY_MS;
      const day = document.createElement("div");
      day.className = "h-day" + (i === 0 ? " today" : "")
        + (bucket.length ? " has-events" : "");
      day.dataset.day = String(i);

      if (bucket.length) {
        // front the next event still to come, not one that's already over
        const front = bucket.find(n => !isDone(n.calendar)) || bucket[bucket.length - 1];
        day.append(this._tile(front, bucket.indexOf(front), bucket.length > 1));
        if (bucket.length > 1) {
          const more = document.createElement("span");
          more.className = "h-more mono";
          more.textContent = `+${bucket.length - 1}`;
          day.append(more);
        }
      }
      const dot = document.createElement("span");
      dot.className = "h-dot";
      const label = document.createElement("span");
      label.className = "h-day-label mono";
      label.textContent = dayLabel(dayMs, i);
      day.append(dot, label);
      row.append(day);
    });

    // nothing on the whole track: say so, once, very quietly
    if (buckets.every(b => !b.length)) {
      const hint = document.createElement("span");
      hint.className = "h-quiet mono";
      hint.textContent = "next 7 days · nothing scheduled";
      row.append(hint);
    }

    this.el.textContent = "";
    this.el.append(row);
    this._buckets = buckets;
  }

  _tile(n, hueIndex, stacked) {
    const c = n.calendar;
    const tile = document.createElement("button");
    tile.className = "h-tile"
      + (stacked ? " stacked" : "")
      + (isDone(c) ? " done" : "");
    tile.dataset.id = n.id;
    tile.style.setProperty("--hue", HUES[hueIndex % HUES.length]);
    tile.title = `${timeLabel(c)} — ${n.title}`;
    const t = document.createElement("time");
    t.className = "mono";
    t.textContent = timeLabel(c);
    const title = document.createElement("span");
    title.className = "h-tile-title";
    title.textContent = n.title;
    tile.append(t, title);
    return tile;
  }

  _onClick(e) {
    const day = e.target.closest(".h-day");
    if (!day) return;
    const bucket = this._buckets?.[Number(day.dataset.day)] || [];
    if (bucket.length > 1) {           // several stops here — fan them out
      e.stopPropagation();
      this._openPop(day, bucket);
      return;
    }
    const tile = e.target.closest(".h-tile");
    if (tile?.dataset.id) location.hash = `#/note/${tile.dataset.id}`;
  }

  // the day's tiles as a little stack pinned under its station — a fixed
  // popover (account-pop style) so the scrolling track never clips it
  _openPop(day, bucket) {
    const wasOpen = this.pop?.dataset.day === day.dataset.day;
    this._closePop();
    if (wasOpen) return;               // second tap on the same day closes it

    const pop = document.createElement("div");
    pop.className = "horizon-pop";
    pop.dataset.day = day.dataset.day;
    const head = document.createElement("div");
    head.className = "horizon-pop-head mono";
    const d = new Date(bucket[0].calendar.start);
    head.textContent = d.toLocaleDateString("en-GB",
      { weekday: "long", day: "numeric", month: "long" });
    pop.append(head);
    bucket.forEach((n, i) => pop.append(this._tile(n, i, false)));
    pop.addEventListener("click", e => {
      const tile = e.target.closest(".h-tile");
      if (tile?.dataset.id) {
        this._closePop();
        location.hash = `#/note/${tile.dataset.id}`;
      }
    });

    document.body.append(pop);
    const rect = day.getBoundingClientRect();
    const w = pop.offsetWidth;
    pop.style.left = Math.max(8, Math.min(
      rect.left + rect.width / 2 - w / 2, innerWidth - w - 8)) + "px";
    pop.style.top = (this.el.getBoundingClientRect().bottom + 6) + "px";
    this.pop = pop;
  }

  _closePop() {
    this.pop?.remove();
    this.pop = null;
  }
}
