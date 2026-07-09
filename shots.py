"""
shots — the pill's screenshot shelf. Every screenshot you take
(Win+Shift+S, PrtScn) and every image you copy lands here as a real PNG
file in the shots folder, capped to the newest few. The pill grows a badge;
click it and the shelf pops open as draggable thumbnails, ready to be
pulled into Claude Code, a chat box, an email — anywhere that takes a
file — or clicked to copy (file + bitmap both go on the clipboard, so
Ctrl+V pastes whichever the target app prefers).

Split like dropnotes: ShotShelf and the DIB/DROPFILES builders are pure
disk/bytes helpers, testable headless. Only clip_seq/copy_shots/
drag_shots touch Win32, and drag_shots must run on the Tk (UI) thread —
OLE drags pump messages on the thread that owns the source window.
"""

import ctypes
import hashlib
import io
import os
import shutil
import struct
import time

from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DEFAULT_KEEP = 12
_DEDUPE_SECONDS = 12       # Snipping Tool fires the clipboard twice per snip


def _shot_title():
    """'Shot 9 Jul 14.32' — same shape as dropnotes.photo_title."""
    t = time.localtime()
    return (f"Shot {t.tm_mday} {time.strftime('%b', t)} "
            f"{t.tm_hour:02d}.{t.tm_min:02d}")


class ShotShelf:
    """The shots\\ folder: newest-first, capped, self-pruning."""

    def __init__(self, folder, keep=DEFAULT_KEEP):
        self.folder = folder
        self.keep = max(1, int(keep or DEFAULT_KEEP))
        self._last_hash = ""
        self._last_pin_t = 0.0

    def paths(self):
        try:
            names = [n for n in os.listdir(self.folder)
                     if os.path.splitext(n)[1].lower() in IMAGE_EXTS]
        except OSError:
            return []
        full = [os.path.join(self.folder, n) for n in names]
        return sorted(full, key=lambda p: os.path.getmtime(p), reverse=True)

    def count(self):
        return len(self.paths())

    def _fresh_path(self, stem, ext):
        os.makedirs(self.folder, exist_ok=True)
        p = os.path.join(self.folder, stem + ext)
        n = 2
        while os.path.exists(p):
            p = os.path.join(self.folder, f"{stem} -{n}{ext}")
            n += 1
        return p

    def _prune(self):
        for p in self.paths()[self.keep:]:
            try:
                os.remove(p)
            except OSError:
                pass

    def pin_image(self, img):
        """PIL image -> pinned PNG. Returns the path, or None when it's the
        same pixels as the last pin moments ago (Snipping Tool double-fire)."""
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        digest = hashlib.md5(
            f"{img.size}{img.mode}".encode() + img.tobytes()).hexdigest()
        now = time.time()
        if (digest == self._last_hash
                and now - self._last_pin_t < _DEDUPE_SECONDS):
            return None
        path = self._fresh_path(_shot_title(), ".png")
        img.save(path, format="PNG")
        self._last_hash, self._last_pin_t = digest, now
        self._prune()
        return path

    def pin_file(self, src):
        """Copy an existing image file onto the shelf. Returns path or None."""
        ext = os.path.splitext(src)[1].lower()
        if ext not in IMAGE_EXTS or not os.path.isfile(src):
            return None
        stem = os.path.splitext(os.path.basename(src))[0].strip() or "Shot"
        path = self._fresh_path(stem[:60], ext)
        try:
            shutil.copyfile(src, path)
        except OSError:
            return None
        self._prune()
        return path

    def remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def clear(self):
        for p in self.paths():
            self.remove(p)


# ----------------------------------------------------------------------------
# Clipboard: watch + copy
# ----------------------------------------------------------------------------

def clip_seq():
    """Bumps on every clipboard change — a cheap poll beats a message pump."""
    try:
        return ctypes.windll.user32.GetClipboardSequenceNumber()
    except Exception:
        return 0


def dropfiles_bytes(paths):
    """DROPFILES struct + double-NUL UTF-16 path list = CF_HDROP payload."""
    files = "".join(p + "\0" for p in paths) + "\0"
    return (struct.pack("<IiiII", 20, 0, 0, 0, 1)
            + files.encode("utf-16-le"))


def dib_bytes(img):
    """PIL image -> CF_DIB payload (a BMP minus its 14-byte file header).
    Transparency flattens to white, same as dropnotes.compress_image."""
    if img.mode in ("RGBA", "LA", "P", "PA"):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    return buf.getvalue()[14:]


def copy_shots(paths):
    """Put shots on the clipboard three ways at once — CF_HDROP (paste as
    files in Explorer / drag-savvy apps), CF_DIB + registered PNG (paste as
    an image in Claude Code, chats, editors). Returns True on success."""
    import win32clipboard as wc
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return False
    png_raw = dib = None
    try:
        with open(paths[0], "rb") as f:
            png_raw = f.read()
        dib = dib_bytes(Image.open(io.BytesIO(png_raw)))
    except Exception:
        png_raw = dib = None
    for attempt in range(6):           # clipboard can be briefly held by anyone
        try:
            wc.OpenClipboard()
            break
        except Exception:
            if attempt == 5:
                return False
            time.sleep(0.05)
    try:
        wc.EmptyClipboard()
        wc.SetClipboardData(wc.CF_HDROP, dropfiles_bytes(paths))
        if dib and len(paths) == 1:
            wc.SetClipboardData(wc.CF_DIB, dib)
            if paths[0].lower().endswith(".png"):
                wc.SetClipboardData(wc.RegisterClipboardFormat("PNG"), png_raw)
        return True
    except Exception:
        return False
    finally:
        try:
            wc.CloseClipboard()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# OLE drag-out — the real thing, same as dragging a file out of Explorer.
# ----------------------------------------------------------------------------

DROPEFFECT_COPY, DROPEFFECT_MOVE, DROPEFFECT_LINK = 1, 2, 4


def drag_shots(paths, dbg=lambda m: None):
    """Start an OLE drag of real files. Blocks until the user drops or
    cancels (OLE pumps messages meanwhile — call from the Tk thread only,
    with the left button still down). Returns True if a drop happened."""
    import pythoncom
    import winerror
    import win32con
    from win32com.server import util
    from win32com.server.exception import COMException
    from win32com.shell import shell

    class _DropSource:
        _com_interfaces_ = [pythoncom.IID_IDropSource]
        _public_methods_ = ["QueryContinueDrag", "GiveFeedback"]

        def QueryContinueDrag(self, esc, keystate):
            if esc:
                raise COMException(hresult=winerror.DRAGDROP_S_CANCEL)
            if not keystate & win32con.MK_LBUTTON:
                raise COMException(hresult=winerror.DRAGDROP_S_DROP)
            return winerror.S_OK

        def GiveFeedback(self, effect):
            raise COMException(
                hresult=winerror.DRAGDROP_S_USEDEFAULTCURSORS)

    paths = [os.path.abspath(p) for p in paths if os.path.isfile(p)]
    if not paths:
        return False
    try:
        pythoncom.OleInitialize()
    except Exception:
        pass                # already initialized on this thread (tkdnd does)
    try:
        # all shots live in one folder -> parent pidl + relative children
        folder = os.path.dirname(paths[0])
        parent = shell.SHParseDisplayName(folder, 0)[0]
        kids = [shell.SHParseDisplayName(p, 0)[0][len(parent):]
                for p in paths]
        dataobj = shell.SHCreateDataObject(parent, kids, None,
                                           pythoncom.IID_IDataObject)
        src = util.wrap(_DropSource(), pythoncom.IID_IDropSource)
        ret = pythoncom.DoDragDrop(
            dataobj, src, DROPEFFECT_COPY | DROPEFFECT_LINK)
        dbg(f"drag_shots -> {ret!r}")
        # DoDragDrop surfaces DRAGDROP_S_DROP / _CANCEL differently across
        # pywin32 builds (return int or tuple) — treat "not cancelled" as done
        flat = ret if isinstance(ret, int) else next(
            (v for v in (ret or ()) if isinstance(v, int)), 0)
        return flat != winerror.DRAGDROP_S_CANCEL
    except Exception as ex:
        dbg(f"drag_shots failed: {ex!r}")
        return False
