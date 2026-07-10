"""
whenparse — pull a calendar intent and a date/time out of dictated text.

Everything here is plain text logic with no I/O, so it's unit-testable and
the calendar backend (gcal.py) never needs to know about phrasing. STT output
arrives with digits ("at 3 PM", "on the 15th") and usually punctuation, so
the rules lean on numbers plus a small vocabulary of day words.

parse_when() never returns None — per the design, saying "add to calendar"
always produces an event:
  - time but no date  -> today (or tomorrow if that time already passed)
  - date but no time  -> all-day on that date
  - neither           -> all-day today
"""

import re
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# trigger: a scheduling verb with "…(to/in/on/for) (the/my) calendar" later in
# the same sentence — the thing being added can sit in the middle, exactly as
# people dictate: "add 8:00 a.m. Autos to my calendar on Monday",
# "add event for my calendar to Monday at 8am", "put it in the calendar"
# ---------------------------------------------------------------------------

_VERBS = r"(?:add|put|stick|pop|create|make|schedule)"

# NB: "." is NOT a boundary here — dictation is full of "8:00 a.m." dots
TRIGGER_RE = re.compile(
    r"\b" + _VERBS + r"\b[^\n!?;]{0,80}?"
    r"\b(?:to|in|into|on|onto|for)\s+(?:the\s+|my\s+)?(?:google\s+)?calendar\b"
    r"|\b(?:add|create|make|schedule)\b[^\n!?;]{0,40}?"
    r"\bcalendar\s+(?:event|entry|appointment)\b",
    re.IGNORECASE)

# the scaffolding words to peel off for the event title (never from the note)
_PREP_CAL_RE = re.compile(
    r"\b(?:to|in|into|on|onto|for)\s+(?:the\s+|my\s+)?(?:google\s+)?"
    r"calendar\b[,.]?", re.IGNORECASE)
_LEAD_VERB_RE = re.compile(
    r"^\s*(?:please\s+)?" + _VERBS + r"\b\s*(?:it|this|that|an?|the)?\s*"
    r"(?:calendar\s+)?(?:event|entry|appointment)?\s*(?:for|of)?\s*[,.]?\s*",
    re.IGNORECASE)
_DANGLING_RE = re.compile(
    r"\b" + _VERBS + r"\s+(?:it|this|that)\s*(?=please\b|$|[,.])",
    re.IGNORECASE)
_LEAD_PREP_RE = re.compile(
    r"^(?:to|on|for)\s+(?=(?:next\s+)?(?:monday|tuesday|wednesday|thursday"
    r"|friday|saturday|sunday|tomorrow|today)\b)", re.IGNORECASE)


def has_trigger(text):
    return bool(TRIGGER_RE.search(text or ""))


def strip_trigger(text):
    """The note text without the trigger scaffolding — used for the event
    title. The note itself always keeps the full original text."""
    out = _PREP_CAL_RE.sub(" ", text or "")
    out = _LEAD_VERB_RE.sub("", out)
    out = _DANGLING_RE.sub(" ", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.;:")
    return _LEAD_PREP_RE.sub("", out)


# ---------------------------------------------------------------------------
# date / time vocabulary
# ---------------------------------------------------------------------------

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}
MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
          "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
          "november": 11, "december": 12}
# STT sometimes shortens months
MONTHS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
               "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12})

_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY_RE = "|".join(WEEKDAYS)

# hour defaults for day-part words when no clock time was said
DAYPART_HOURS = {"morning": 9, "afternoon": 14, "evening": 18,
                 "tonight": 19, "night": 19, "lunchtime": 12, "lunch": 12,
                 "noon": 12, "midday": 12, "midnight": 0}


def _local_now():
    return datetime.now().astimezone()


def _next_weekday(base, target, allow_today=False):
    ahead = (target - base.weekday()) % 7
    if ahead == 0 and not allow_today:
        ahead = 7
    return base + timedelta(days=ahead)


# ---------------------------------------------------------------------------
# time of day
# ---------------------------------------------------------------------------

# A number only counts as a time when something anchors it as one:
#   (a) a lead word    — "at 3", "from 10:30", "around 4"
#   (b) an am/pm/o'clock suffix — "3pm", "3 o'clock"
#   (c) an unambiguous 24h clock — "15:00"
#   (d) the start of a range     — "10 to 2"
# A bare digit with none of those ("buy 3 apples") is never a time.
_CLOCK = r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)?"
_TIME_FORMS = [
    re.compile(r"\b(?:at|by|from|around|about)\s+" + _CLOCK + r"\b",
               re.IGNORECASE),
    re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?|o'?clock)\b",
               re.IGNORECASE),
    re.compile(r"\b(\d{1,2})[:.](\d{2})()\b"),
    re.compile(r"\b" + _CLOCK + r"(?=\s+(?:to|until|till)\s+\d)",
               re.IGNORECASE),
]


def _find_time(text):
    """(hour, minute, explicit) or None. `explicit` means am/pm (or a 24h
    clock) pinned the half of the day — a bare hour gets a guess later."""
    for pat in _TIME_FORMS:
        for m in pat.finditer(text):
            h = int(m.group(1))
            mnt = int(m.group(2) or 0)
            suffix = (m.group(3) or "").replace(".", "").lower()
            # numbers that belong to a date ("on the 15th", "15 august",
            # "15/8") are never times — the date patterns own those
            after = text[m.end():m.end() + 14].lower()
            if re.match(r"\s*(st|nd|rd|th)\b", after):
                continue
            if re.match(r"\s*(?:of\s+)?(" + _MONTH_RE + r")\b", after):
                continue
            if re.match(r"\s*/", after):
                continue
            if h > 23 or mnt > 59:
                continue
            explicit = bool(suffix in ("am", "pm") or h > 12 or h == 0)
            if suffix == "pm" and h < 12:
                h += 12
            elif suffix == "am" and h == 12:
                h = 0
            return (h, mnt, explicit)
    return None


def _find_range_end(text, start_h):
    """'3 to 4', 'from 3 until 5pm' — the end hour, or None."""
    m = re.search(
        r"\b(?:to|until|till|through)\s+(\d{1,2})(?:[:.](\d{2}))?\s*"
        r"(a\.?m\.?|p\.?m\.?)?\b", text, re.IGNORECASE)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "").lower()
    if suffix == "pm" and h < 12:
        h += 12
    elif not suffix and h <= 12 and h + 12 > start_h and h < start_h:
        h += 12                       # "10 to 2" -> 14:00
    if h > 23 or mnt > 59:
        return None
    return (h, mnt)


def _find_duration(text):
    """'for 2 hours', 'for 45 minutes', 'for half an hour' -> minutes."""
    m = re.search(r"\bfor\s+(?:(\d+)|an?|half\s+an?)\s*"
                  r"(hours?|hrs?|minutes?|mins?)\b", text, re.IGNORECASE)
    if not m:
        if re.search(r"\bfor\s+half\s+an?\s+hour\b", text, re.IGNORECASE):
            return 30
        return None
    n = m.group(1)
    unit = m.group(2).lower()
    if n is None:
        n = 0.5 if "half" in m.group(0).lower() else 1
    else:
        n = int(n)
    return int(n * 60) if unit.startswith(("hour", "hr")) else int(n)


# ---------------------------------------------------------------------------
# date
# ---------------------------------------------------------------------------

def _find_date(text, now, has_time=False):
    """(date, src) for the event, or (None, "").  src names which pattern
    matched — parse_when uses "weekday" to roll a stale match a week on."""
    low = text.lower()
    if re.search(r"\bday after tomorrow\b", low):
        return (now + timedelta(days=2)).date(), "rel"
    if re.search(r"\btomorrow\b", low):
        return (now + timedelta(days=1)).date(), "rel"
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", low):
        return now.date(), "rel"

    m = re.search(r"\b(next\s+)?(" + _DAY_RE + r")\b", low)
    if m:
        # "next X" and plain "X" both mean the upcoming one. Today only
        # counts when a time was said too ("friday at 4pm" on a Friday
        # morning); a time-less "on friday" always means the coming one.
        return _next_weekday(now, WEEKDAYS[m.group(2)],
                             allow_today=has_time and not m.group(1)
                             ).date(), "weekday"

    # "15th of august" / "august 15(th)" / "15 august"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_RE
                  + r")\b", low) or \
        re.search(r"\b(" + _MONTH_RE + r")\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b",
                  low)
    if m:
        a, b = m.group(1), m.group(2)
        day, month = (int(a), MONTHS[b]) if a.isdigit() else (int(b), MONTHS[a])
        if 1 <= day <= 31:
            year = now.year
            try:
                cand = now.replace(year=year, month=month, day=day).date()
            except ValueError:
                return None, ""
            if cand < now.date():
                cand = cand.replace(year=year + 1)
            return cand, "month"

    # "on the 15th" — this month, or next month if it's already gone
    m = re.search(r"\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b", low)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            y, mo = now.year, now.month
            if day < now.day:
                mo += 1
                if mo == 13:
                    mo, y = 1, y + 1
            try:
                return now.replace(year=y, month=mo, day=day).date(), "dom"
            except ValueError:
                return None, ""

    # numeric "15/8" or "15/08/2026" (UK day-first)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            cand = datetime(year, month, day).date()
        except ValueError:
            return None, ""
        if not m.group(3) and cand < now.date():
            cand = cand.replace(year=year + 1)
        return cand, "numeric"
    return None, ""


def _find_daypart(text):
    low = text.lower()
    for word, hour in DAYPART_HOURS.items():
        if re.search(r"\b" + word + r"\b", low):
            return hour
    return None


# ---------------------------------------------------------------------------
# the one entry point
# ---------------------------------------------------------------------------

def parse_when(text, now=None):
    """-> {"start": datetime, "end": datetime, "all_day": bool}
    Always returns an event; see the module docstring for the fallbacks."""
    now = now or _local_now()
    stripped = strip_trigger(text)

    # "in 2 hours" / "in 45 minutes" / "in an hour" — relative beats absolute
    m = re.search(r"\bin\s+(?:(\d+)|an?|half\s+an?)\s*"
                  r"(hours?|hrs?|minutes?|mins?)\b", stripped, re.IGNORECASE)
    if m:
        n = m.group(1)
        n = (0.5 if "half" in m.group(0).lower() else 1) if n is None else int(n)
        mins = int(n * 60) if m.group(2).lower().startswith(("hour", "hr")) \
            else int(n)
        start = (now + timedelta(minutes=mins)).replace(second=0, microsecond=0)
        dur = _find_duration(stripped) or 60
        return {"start": start, "end": start + timedelta(minutes=dur),
                "all_day": False}

    t = _find_time(stripped)
    daypart = _find_daypart(stripped)
    date, date_src = _find_date(stripped, now,
                                has_time=t is not None or daypart is not None)

    if t is not None:
        h, mnt, explicit = t[0], t[1], t[2]
        if not explicit:
            # bare "at 3": daypart word decides, else 1-7 reads pm, 8-11 am
            if daypart is not None and daypart >= 12 and h < 12:
                h += 12
            elif daypart is not None and daypart < 12:
                pass                                  # morning: keep am
            elif 1 <= h <= 7:
                h += 12
    elif daypart is not None:
        h, mnt = daypart, 0
    else:
        h = None

    if h is None:
        # no time at all -> all-day (today when no date either)
        day = date or now.date()
        start = datetime(day.year, day.month, day.day).astimezone()
        return {"start": start, "end": start + timedelta(days=1),
                "all_day": True}

    if date is None:
        date = now.date()
        cand = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if cand <= now:                               # that time already passed
            date = date + timedelta(days=1)

    start = datetime(date.year, date.month, date.day, h, mnt).astimezone()
    if start <= now and date_src == "weekday":
        start += timedelta(days=7)      # "friday at 9am" said Friday lunchtime
    rng = _find_range_end(stripped, h)
    if rng is not None and (rng[0], rng[1]) > (h, mnt):
        end = start.replace(hour=rng[0], minute=rng[1])
    else:
        end = start + timedelta(minutes=_find_duration(stripped) or 60)
    return {"start": start, "end": end, "all_day": False}
