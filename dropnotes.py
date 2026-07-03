"""
dropnotes — turns things thrown at the pill into notes: files dragged from
Explorer, text selections dragged from any app, image URLs dragged out of a
browser, and clipboard images. Pure helpers, no Tk — testable headless.

An image note is a note whose body is a data:image/...;base64 URL — the
exact contract of web/js/imgnote.js, so the same entry renders on the phone,
the hosted app and the desktop viewer with zero changes to sync or storage.
Compression mirrors the web ladder: longest edge 1600px, JPEG, stepping down
until the result fits MAX_IMAGE_BYTES.

A file note is the same trick for documents (PDF, Word, Excel, …): the body
is data:<mime>;name=<urlencoded filename>;base64,<bytes> — the contract of
web/js/filenote.js. The ;name= parameter is what tells a file note apart
from an image note, and carries the filename for download/share. Capped at
MAX_FILE_BYTES so the base64 stays under Firebase RTDB's 10 MB string limit;
video and audio are refused (this is a document pipe, not a media locker).
"""

import base64
import io
import mimetypes
import os
import re
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageOps

MAX_IMAGE_BYTES = 600 * 1024        # hard cap AFTER compression (= imgnote.js)
KEEP_ORIGINAL_BYTES = 250 * 1024    # already small and web-friendly? keep as-is
MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_TEXT_BYTES = 200 * 1024
MAX_FILE_BYTES = 7 * 1024 * 1024    # file notes (= filenote.js); b64 of 7 MB
                                    # ≈ 9.4 MB, under RTDB's 10 MB string cap
LADDER = ((1600, 80), (1600, 65), (1280, 60), (1024, 55), (800, 50))

PASSTHROUGH_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TEXTY_EXTS = {".txt", ".md", ".markdown", ".csv", ".log", ".json", ".xml",
              ".yaml", ".yml", ".ini", ".py", ".js", ".ts", ".html", ".css"}
DULL_STEMS = {"image", "img", "download", "unnamed", "screenshot", "untitled"}

# extensions mimetypes sometimes misses on a bare Windows install
EXTRA_MIMES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
    ".zip": "application/zip",
}

DATA_URL_RE = re.compile(
    r"^data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\s]+)$")
URL_RE = re.compile(r"^https?://\S+$")


def photo_title(prefix="Photo"):
    """'Photo 3 Jul 17.42' — colon-free so it survives sanitize_title."""
    t = time.localtime()
    return (f"{prefix} {t.tm_mday} {time.strftime('%b', t)} "
            f"{t.tm_hour:02d}.{t.tm_min:02d}")


def _stem_title(name):
    stem = os.path.splitext(os.path.basename(name or ""))[0].strip()
    if stem and stem.lower() not in DULL_STEMS:
        return stem[:60]
    return photo_title()


def compress_image(img, keep_bytes=None, keep_mime=None):
    """PIL Image -> data URL within MAX_IMAGE_BYTES.

    keep_bytes/keep_mime: the original encoded file, passed through untouched
    when it's already small and web-friendly (keeps GIF animation, keeps
    screenshots pixel-perfect)."""
    if (keep_bytes is not None and len(keep_bytes) <= KEEP_ORIGINAL_BYTES
            and keep_mime in PASSTHROUGH_MIMES):
        return "data:%s;base64,%s" % (
            keep_mime, base64.b64encode(keep_bytes).decode("ascii"))

    img = ImageOps.exif_transpose(img)     # phone photos are rotated via EXIF
    if img.mode in ("RGBA", "LA", "P", "PA"):
        rgba = img.convert("RGBA")         # flatten transparency onto white
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")

    for edge, quality in LADDER:
        w, h = img.size
        scale = min(1.0, edge / float(max(w, h)))
        frame = img if scale >= 1.0 else img.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            return ("data:image/jpeg;base64,"
                    + base64.b64encode(buf.getvalue()).decode("ascii"))
    raise ValueError("Couldn't shrink that image enough to save it")


def _image_note_from_bytes(raw, title):
    img = Image.open(io.BytesIO(raw))
    img.load()
    mime = Image.MIME.get(img.format)
    return title, compress_image(img, keep_bytes=raw, keep_mime=mime)


def image_note_from_file(path):
    if os.path.getsize(path) > MAX_SOURCE_BYTES:
        raise ValueError(os.path.basename(path) + " is over 40 MB — too big")
    with open(path, "rb") as f:
        raw = f.read()
    return _image_note_from_bytes(raw, _stem_title(path))


def text_note_from_file(path):
    name = os.path.basename(path)
    if os.path.getsize(path) > MAX_TEXT_BYTES:
        raise ValueError(name + " is too big for a note")
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Can't save " + name + " — it doesn't look like text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError(name + " is empty")
    stem = os.path.splitext(name)[0].strip()
    from notestore import note_title_from
    return (stem[:60] or note_title_from(text)), text


def _is_image_file(path):
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def mime_for(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in EXTRA_MIMES:
        return EXTRA_MIMES[ext]
    return mimetypes.guess_type(os.path.basename(path))[0] \
        or "application/octet-stream"


def file_note_from_file(path):
    """Any document -> (title, data:<mime>;name=…;base64 body). The phone
    and desktop viewers turn it back into a viewable/sharable file."""
    name = os.path.basename(path)
    size = os.path.getsize(path)
    if size == 0:
        raise ValueError(name + " is empty")
    if size > MAX_FILE_BYTES:
        raise ValueError(f"{name} is {size / (1024 * 1024):.0f} MB — "
                         "files up to 7 MB can sync")
    with open(path, "rb") as f:
        raw = f.read()
    body = "data:%s;name=%s;base64,%s" % (
        mime_for(path), urllib.parse.quote(name),
        base64.b64encode(raw).decode("ascii"))
    stem = os.path.splitext(name)[0].strip()
    return (stem[:60] or name[:60]), body


def note_from_path(path):
    """(title, body) for any dropped file. Images become image notes, text
    stays an editable text note, video/audio are refused, and every other
    document becomes a file note. ValueError = friendly refusal."""
    if not os.path.isfile(path):
        raise ValueError("Can't find " + os.path.basename(path))
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return image_note_from_file(path)
    if ext in TEXTY_EXTS:
        return text_note_from_file(path)
    mime = mime_for(path)
    if mime.startswith(("video/", "audio/")):
        raise ValueError(os.path.basename(path)
                         + " — videos and audio don't sync, documents only")
    if (os.path.getsize(path) <= MAX_SOURCE_BYTES and _is_image_file(path)):
        return image_note_from_file(path)
    # unknown extension (no known mime) but plain text inside? keep it
    # editable — known document types always stay real files
    if (mime == "application/octet-stream"
            and os.path.getsize(path) <= MAX_TEXT_BYTES):
        try:
            return text_note_from_file(path)
        except ValueError:
            pass
    return file_note_from_file(path)


def _fetch_image(url):
    """Fetch a dragged image URL. (title, body) or None to fall back to a
    text note — never raises."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (DictationMic)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            raw = r.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            return None
        if not (ctype.startswith("image/") or url.lower().rsplit("?", 1)[0]
                .endswith(tuple(IMAGE_EXTS))):
            return None
        return _image_note_from_bytes(raw, _stem_title(url.split("?")[0]))
    except Exception:
        return None


def note_from_dropped_text(text, fetch=True):
    """Dropped/pasted text -> (title, body). data:image URLs decode straight
    to image notes; a lone http(s) URL is fetched and, if it's an image,
    becomes an image note; anything else is saved as a text note."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Nothing there to save")
    m = DATA_URL_RE.match(text)
    if m:
        raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
        return _image_note_from_bytes(raw, photo_title("Image"))
    first = text.split("\n", 1)[0].strip()      # Firefox drags "url\ntitle"
    if fetch and URL_RE.match(first) and len(text.split("\n")) <= 2:
        got = _fetch_image(first)
        if got is not None:
            return got
    from notestore import note_title_from
    return note_title_from(text), text
