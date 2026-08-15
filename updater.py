"""
updater — notices when a newer Windows build has been published, so the pill
can say so.

The desktop app has no store and no installer sitting behind it: it ships as a
zip you extract, and the folder you extracted *is* the app. Nothing is ever
going to come along and tell Steve a newer one exists, so the app has to find
out for itself. The phone already does exactly this (Updater.kt) and the rules
here are its rules — it just can't take the last step, because on Windows
there is nothing to hand an install to.

Android and Windows releases share one list on the repo and are told apart by
their tag prefix, so GitHub's "latest" endpoint is no use at all: it answers
with whichever release was flagged latest overall, which is usually the phone.
Walk the list and filter instead.

Two timings, and they are deliberately different. The GitHub call is
rate-limited, but what that call *found* is remembered in settings.json and
offered again the instant the app starts — so quitting and reopening puts the
update back in front of him immediately instead of going quiet until the gap
expires.

Every network call is wrapped and a failure just means "try again later". None
of this may ever reach the user as an error; cloudsync.py holds to the same
rule for the same reason.
"""

import re
import time

RELEASES = "https://api.github.com/repos/stevenmcginty/DictationMic/releases"
RELEASES_PAGE = "https://github.com/stevenmcginty/DictationMic/releases"

# Only gates the GitHub call. A release that has already been found is offered
# again on every start regardless of this — see pending_update().
CHECK_GAP_S = 300

# A version has to have a dot in it to be recognised inside free text. Without
# that, a release titled "DictationMic for Windows (2026 rebuild)" would be
# read as version 2026 and would nag forever.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def is_newer(candidate, current):
    """Plain dotted numbers, compared piece by piece. Anything that doesn't
    parse counts as "not newer" — a typo in a tag must never nag."""
    try:
        a = [int(p) for p in str(candidate).split(".")]
        b = [int(p) for p in str(current).split(".")]
    except (TypeError, ValueError):
        return False
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x != y:
            return x > y
    return False


def _version_of(release):
    """What version a release claims to be, or None if it doesn't claim one.

    The tag is the honest place for it — `windows-v1.2`, mirroring the
    `android-v1.2` the phone already reads. But the Windows zip is published
    to one fixed `windows-latest` tag whose entire point is a download link
    that never changes (see .github/workflows/release-windows.yml), and that
    tag names no version at all. So a version in the release *title* counts
    too: retitling that rolling release "DictationMic for Windows 1.2" is
    enough to make this work without disturbing the fixed link.

    A release that names no version anywhere is passed over rather than
    guessed at — same instinct as is_newer(): if it can't be read, it isn't
    newer, and Steve gets no prompt rather than a wrong one.
    """
    tag = release.get("tag_name") or ""
    if tag.startswith("windows-v") and tag[len("windows-v"):]:
        return tag[len("windows-v"):]
    found = _VERSION_RE.search(release.get("name") or "")
    return found.group(0) if found else None


def latest_windows_release():
    """The highest-numbered published Windows release, as
    {"version", "url"} — or None if the list can't be read or nothing in it
    says which version it is. Drafts and prereleases aren't offers."""
    import requests
    best = None
    try:
        r = requests.get(RELEASES, timeout=15,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return None
        for release in r.json():
            if release.get("draft") or release.get("prerelease"):
                continue
            if not (release.get("tag_name") or "").startswith("windows-"):
                continue
            version = _version_of(release)
            if version is None:
                continue
            # Highest version wins rather than first-in-the-list, which is how
            # the phone does it: GitHub orders by when a release was created,
            # and the rolling `windows-latest` release keeps its original
            # creation date however many times its zip is replaced. Its
            # position therefore says nothing about which build is newest.
            if best is None or is_newer(version, best["version"]):
                best = {"version": version,
                        "url": release.get("html_url") or RELEASES_PAGE}
    except Exception:
        return None          # no internet, GitHub having a moment, odd JSON
    return best


def pending_update(settings, save, current_version):
    """Refresh the cached finding — at most once every CHECK_GAP_S — and hand
    back whatever update is worth offering right now, or None.

    The answer comes out of the cache, never straight from the call. That is
    what makes reopening the app re-offer a known update instantly instead of
    staying silent until the gap expires.

    Whether the offer is actually *shown* is the app's business. A version
    Steve has waved away is still returned here, so it can keep its place in
    the right-click menu while no longer popping anything up at him.
    """
    now = time.time()
    if now - (settings.get("update_last_check") or 0) >= CHECK_GAP_S:
        settings["update_last_check"] = now
        found = latest_windows_release()
        if found is not None:
            settings["update_pending_version"] = found["version"]
            settings["update_pending_url"] = found["url"]
        # The timestamp is saved even when the call failed, so a machine that
        # is offline for a week doesn't hammer GitHub on every poll.
        save(settings)
    version = settings.get("update_pending_version") or ""
    if not is_newer(version, current_version):
        return None
    return {"version": version,
            "url": settings.get("update_pending_url") or RELEASES_PAGE}
