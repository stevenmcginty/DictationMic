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
import uuid

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
#
# Raw ctypes end-to-end. The pywin32 route (util.wrap'd IDropSource +
# pythoncom.DoDragDrop) died with an access violation inside
# pythoncom314.dll on every drag (pywin32 312 / Python 3.14) and took the
# whole app down. Nothing here touches pythoncom: the data object is the
# shell's own (SHCreateDataObject) and the drop source is a hand-built COM
# vtable whose callbacks return plain HRESULTs — no gateways, no
# exception-encoded results.
# ----------------------------------------------------------------------------

DROPEFFECT_COPY, DROPEFFECT_MOVE, DROPEFFECT_LINK = 1, 2, 4
_DRAGDROP_S_DROP = 0x00040100
_DRAGDROP_S_CANCEL = 0x00040101
_DRAGDROP_S_USEDEFAULTCURSORS = 0x00040102
_E_NOINTERFACE = -2147467262          # 0x80004002 as a signed HRESULT
_MK_LBUTTON, _MK_RBUTTON = 0x0001, 0x0002


class _DropSource:
    """Minimal IDropSource as a raw COM vtable. Only lives across one
    synchronous DoDragDrop call, so AddRef/Release are token counts."""

    _QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p)
    _REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    _QCD = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int,
                              ctypes.c_ulong)
    _GF = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_ulong)

    _IIDS = (uuid.UUID("00000000-0000-0000-C000-000000000046").bytes_le,
             uuid.UUID("00000121-0000-0000-C000-000000000046").bytes_le)

    def __init__(self):
        self._cbs = (self._QI(self._query_interface),
                     self._REF(lambda this: 2),        # AddRef
                     self._REF(lambda this: 1),        # Release
                     self._QCD(self._query_continue),
                     self._GF(self._give_feedback))
        self._vtbl = (ctypes.c_void_p * 5)(
            *(ctypes.cast(cb, ctypes.c_void_p).value for cb in self._cbs))
        self._body = ctypes.c_void_p(ctypes.addressof(self._vtbl))
        self.ptr = ctypes.addressof(self._body)

    def _query_interface(self, this, riid, ppv):
        try:
            out = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))
            if riid and ctypes.string_at(riid, 16) in self._IIDS:
                out[0] = this
                return 0                               # S_OK
            out[0] = None
            return _E_NOINTERFACE
        except Exception:
            return _E_NOINTERFACE

    def _query_continue(self, this, esc, keystate):
        if esc or keystate & _MK_RBUTTON:
            return _DRAGDROP_S_CANCEL
        if not keystate & _MK_LBUTTON:
            return _DRAGDROP_S_DROP
        return 0                                       # S_OK — keep dragging

    def _give_feedback(self, this, effect):
        return _DRAGDROP_S_USEDEFAULTCURSORS


def _com_release(ptr):
    """IUnknown::Release straight off the raw vtable (slot 2)."""
    try:
        vtbl = ctypes.cast(
            ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(ptr)
    except Exception:
        pass


def _shell_data_object(paths, dbg):
    """Native shell IDataObject* (CF_HDROP + shell formats) for files that
    all live in one folder. Returns a raw COM pointer, or None."""
    s32 = ctypes.windll.shell32
    parse = s32.SHParseDisplayName
    parse.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
                      ctypes.POINTER(ctypes.c_ulong)]
    parse.restype = ctypes.c_long
    find_last = s32.ILFindLastID
    find_last.argtypes = [ctypes.c_void_p]
    find_last.restype = ctypes.c_void_p
    il_free = s32.ILFree
    il_free.argtypes = [ctypes.c_void_p]
    il_free.restype = None
    create = s32.SHCreateDataObject
    create.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_long

    pidls, attrs = [], ctypes.c_ulong(0)

    def _pidl(path):
        p = ctypes.c_void_p()
        hr = parse(path, None, ctypes.byref(p), 0, ctypes.byref(attrs))
        if hr < 0 or not p:
            dbg(f"SHParseDisplayName({path!r}) -> 0x{hr & 0xFFFFFFFF:08X}")
            return None
        pidls.append(p)
        return p

    try:
        folder = _pidl(os.path.dirname(paths[0]))
        if folder is None:
            return None
        kids = []
        for p in paths:
            ab = _pidl(p)
            if ab is None:
                return None
            kids.append(find_last(ab))     # child id points into ab's pidl
        arr = (ctypes.c_void_p * len(kids))(*kids)
        iid = (ctypes.c_ubyte * 16).from_buffer_copy(
            uuid.UUID("0000010E-0000-0000-C000-000000000046").bytes_le)
        out = ctypes.c_void_p()
        hr = create(folder, len(kids), arr, None, ctypes.byref(iid),
                    ctypes.byref(out))
        if hr < 0 or not out:
            dbg(f"SHCreateDataObject -> 0x{hr & 0xFFFFFFFF:08X}")
            return None
        return out                 # the data object clones the pidls
    finally:
        for p in pidls:
            il_free(p)


def drag_shots(paths, dbg=lambda m: None):
    """Start an OLE drag of real files. Blocks until the user drops or
    cancels (OLE pumps messages meanwhile — call from the Tk thread only,
    with the left button still down). Returns True if a drop happened."""
    paths = [os.path.abspath(p) for p in paths if os.path.isfile(p)]
    if not paths:
        return False
    try:
        ole32 = ctypes.windll.ole32
        ole32.OleInitialize.argtypes = [ctypes.c_void_p]
        ole32.OleInitialize.restype = ctypes.c_long
        ole32.DoDragDrop.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_ulong,
                                     ctypes.POINTER(ctypes.c_ulong)]
        ole32.DoDragDrop.restype = ctypes.c_long
        ole32.OleInitialize(None)  # S_FALSE when tkdnd already did — fine
        dataobj = _shell_data_object(paths, dbg)
        if dataobj is None:
            return False
        src = _DropSource()
        effect = ctypes.c_ulong(0)
        try:
            hr = ole32.DoDragDrop(dataobj, src.ptr,
                                  DROPEFFECT_COPY | DROPEFFECT_LINK,
                                  ctypes.byref(effect))
        finally:
            _com_release(dataobj)
        dbg(f"drag_shots -> hr=0x{hr & 0xFFFFFFFF:08X} "
            f"effect={effect.value}")
        return hr == _DRAGDROP_S_DROP
    except Exception as ex:
        dbg(f"drag_shots failed: {ex!r}")
        return False
