"""Unit pass for whenparse — run: venv\\Scripts\\python.exe test_whenparse.py"""

import sys
from datetime import datetime, timedelta

from whenparse import parse_when, has_trigger, strip_trigger

# a fixed "now": Friday 10 July 2026, 10:30 local
NOW = datetime(2026, 7, 10, 10, 30).astimezone()

CASES = [
    # --- trigger detection ---
    ("trigger", "Remember to add to calendar dentist tomorrow at 3", True),
    ("trigger", "Add this to my calendar, lunch with Tom on Friday", True),
    ("trigger", "put it in the calendar for Monday", True),
    ("trigger", "Add it to the Google Calendar please", True),
    ("trigger", "stick that on the calendar", True),
    ("trigger", "buy a new calendar for the kitchen", False),
    ("trigger", "the calendar looks nice", False),

    # --- explicit date + time ---
    ("when", "add to calendar dentist tomorrow at 3pm",
     dict(day=(2026, 7, 11), hm=(15, 0), all_day=False)),
    ("when", "add to calendar dentist tomorrow at 3",
     dict(day=(2026, 7, 11), hm=(15, 0), all_day=False)),          # bare 3 -> pm
    ("when", "add to calendar call mum tomorrow at 9",
     dict(day=(2026, 7, 11), hm=(9, 0), all_day=False)),           # bare 9 -> am
    ("when", "add to calendar meeting tomorrow at 9 in the evening",
     dict(day=(2026, 7, 11), hm=(21, 0), all_day=False)),          # daypart wins
    ("when", "add to calendar dentist tomorrow at 3:30",
     dict(day=(2026, 7, 11), hm=(15, 30), all_day=False)),
    ("when", "add to calendar staff meeting monday at 11am",
     dict(day=(2026, 7, 13), hm=(11, 0), all_day=False)),
    ("when", "add to calendar catch up next tuesday at 2pm",
     dict(day=(2026, 7, 14), hm=(14, 0), all_day=False)),
    ("when", "add to calendar review on the 15th at 10am",
     dict(day=(2026, 7, 15), hm=(10, 0), all_day=False)),
    ("when", "add to calendar dentist on the 3rd at 10am",
     dict(day=(2026, 8, 3), hm=(10, 0), all_day=False)),           # 3rd gone -> Aug
    ("when", "add to calendar birthday on the 15th of august",
     dict(day=(2026, 8, 15), all_day=True)),
    ("when", "add to calendar trip on august 15 at noon",
     dict(day=(2026, 8, 15), hm=(12, 0), all_day=False)),
    ("when", "add to calendar mot on 15/8 at 8am",
     dict(day=(2026, 8, 15), hm=(8, 0), all_day=False)),
    ("when", "add to calendar supplier call today at 4pm",
     dict(day=(2026, 7, 10), hm=(16, 0), all_day=False)),
    ("when", "add to calendar drinks tonight",
     dict(day=(2026, 7, 10), hm=(19, 0), all_day=False)),
    ("when", "add to calendar day after tomorrow lunch with Sam",
     dict(day=(2026, 7, 12), hm=(12, 0), all_day=False)),

    # --- time only -> today, or tomorrow if past ---
    ("when", "add to calendar dentist at 3pm",
     dict(day=(2026, 7, 10), hm=(15, 0), all_day=False)),
    ("when", "add to calendar early gym at 7am",
     dict(day=(2026, 7, 11), hm=(7, 0), all_day=False)),           # 7am passed

    # --- relative ---
    ("when", "add to calendar check the oven in 2 hours",
     dict(day=(2026, 7, 10), hm=(12, 30), all_day=False)),
    ("when", "add to calendar tea break in 45 minutes",
     dict(day=(2026, 7, 10), hm=(11, 15), all_day=False)),

    # --- duration / range ---
    ("when", "add to calendar workshop tomorrow at 1pm for 2 hours",
     dict(day=(2026, 7, 11), hm=(13, 0), end_hm=(15, 0), all_day=False)),
    ("when", "add to calendar shift monday 10 to 2",
     dict(day=(2026, 7, 13), hm=(10, 0), end_hm=(14, 0), all_day=False)),
    ("when", "add to calendar call tomorrow at 3 for half an hour",
     dict(day=(2026, 7, 11), hm=(15, 0), end_hm=(15, 30), all_day=False)),

    # --- fallbacks ---
    ("when", "add to calendar sort out the invoices",
     dict(day=(2026, 7, 10), all_day=True)),                       # nothing -> today
    ("when", "add to calendar do the VAT return on friday",
     dict(day=(2026, 7, 17), all_day=True)),                       # date, no time
    ("when", "add to calendar buy 3 apples and 2 pears tomorrow",
     dict(day=(2026, 7, 11), all_day=True)),                       # bare digits != times

    # --- title stripping ---
    ("strip", "Add to calendar dentist tomorrow at 3pm",
     "dentist tomorrow at 3pm"),
    ("strip", "Dentist tomorrow at 3, add it to my calendar please",
     "Dentist tomorrow at 3, please"),
]


def main():
    fails = 0
    for kind, text, want in CASES:
        if kind == "trigger":
            got = has_trigger(text)
            ok = got == want
        elif kind == "strip":
            got = strip_trigger(text)
            ok = got == want
        else:
            r = parse_when(text, now=NOW)
            got = {"day": (r["start"].year, r["start"].month, r["start"].day),
                   "hm": (r["start"].hour, r["start"].minute),
                   "end_hm": (r["end"].hour, r["end"].minute),
                   "all_day": r["all_day"]}
            ok = all(got[k] == v for k, v in want.items())
            if ok and not want.get("all_day") and "end_hm" not in want:
                # default duration must be one hour
                ok = (r["end"] - r["start"]) == timedelta(hours=1)
        if not ok:
            fails += 1
            print(f"FAIL [{kind}] {text!r}\n  want {want}\n  got  {got}")
    total = len(CASES)
    print(f"{total - fails}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
