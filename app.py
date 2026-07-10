"""
DictationMic — on-device live dictation for Windows.

A floating, draggable, always-on-top dictation pill. Tap RIGHT CTRL (or click
the pill) and just talk: each phrase is transcribed locally with NVIDIA's
Parakeet model the moment you pause, and typed straight into the focused
input box (or accumulated to the clipboard). Goes quiet for 10 s? It stops by
itself. Hold the hotkey instead of tapping for push-to-talk. Right-click the
pill (or Shift+click / Ctrl+click, for touchpads with a stubborn right
button) to change the hotkey to any key you like.

First run downloads the speech model (~660 MB, one time); after that it is
fully offline.
"""

import base64
import ctypes
import ctypes.wintypes
import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from collections import deque

import numpy as np
import sounddevice as sd
import keyboard
import pyperclip
import winsound
from PIL import Image, ImageDraw, ImageFont

import shots

# OS drag-and-drop onto the pill (tkdnd). Optional: if the package or its
# native DLL is unavailable (Smart App Control has blocked stranger things),
# the pill still runs — you just lose dropping, not the clipboard route.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES, DND_TEXT
except Exception:
    TkinterDnD = None
    DND_FILES = DND_TEXT = None


def tk_photo(img):
    """PIL image -> tk.PhotoImage via in-memory PNG. Avoids PIL.ImageTk, whose
    native DLL Smart App Control likes to block in freshly built exes."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))

# ----------------------------------------------------------------------------
# Paths / settings
# ----------------------------------------------------------------------------

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = app_dir()
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
NOTES_DIR = os.path.join(APP_DIR, "notes")

_DEBUG = bool(os.environ.get("DICTMIC_DEBUG"))

# Debug lines go through a queue to a writer thread. dbg() used to open and
# append the file inline — called from the keyboard-hook callback that means
# disk I/O on every keystroke system-wide, and Windows silently removes
# low-level hooks whose callbacks dawdle (the hotkey then dies with no error).
_dbg_q = queue.Queue(maxsize=4000)

def _dbg_writer():
    path = os.path.join(APP_DIR, "debug.log")
    while True:
        lines = [_dbg_q.get()]
        try:
            while len(lines) < 200:
                lines.append(_dbg_q.get_nowait())
        except queue.Empty:
            pass
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            pass

if _DEBUG:
    threading.Thread(target=_dbg_writer, daemon=True).start()

def dbg(msg):
    if _DEBUG:
        try:
            _dbg_q.put_nowait(f"{time.time():.3f} {msg}\n")
        except Exception:
            pass

DEFAULT_SETTINGS = {
    "mode": "type",            # "type" -> types into focused box, "clipboard" -> copies
    "hotkeys": ["ctrl", "f8"],         # tap = start/stop, hold = push-to-talk
    "beeps": True,
    "save_notes": True,        # keep a copy of every dictation in notes\
    "auto_stop_seconds": 10,   # stop listening after this much silence (0 = never)
    "size": 84,                # pill width in px (height follows)
    "catch_shots": True,       # screenshots/copied images pin to the shelf
    "shots_keep": 12,          # how many pinned shots to keep (oldest pruned)
    "shots_to_notes": True,    # caught screenshots also saved as image notes
    "phone_shots": True,       # image notes arriving from the phone pin to the shelf
    "seen_intro": False,
    "seen_intro2": False,
    "x": None,
    "y": None,
    "sync_enabled": False,     # phone sync via Firebase (cloudsync.py)
    "sync_email": "",
    "sync_refresh_token": "",
    "sync_uid": "",
    "calendar_enabled": True,  # "add to calendar" in a dictation makes an event
    "calendar_provider": "google",   # apple/iCloud is a future option
    "gcal_client_id": "",      # Steve's own OAuth client (see README)
    "gcal_client_secret": "",
    "gcal_refresh_token": "",  # empty = not connected
    "gcal_email": "",
}

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    loaded = {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        s.update(loaded)
    except Exception:
        pass
    # migrate old single-hotkey config
    if "hotkeys" not in loaded:
        old = loaded.get("hotkey")
        s["hotkeys"] = ["ctrl", "f8"]
        if old and old not in s["hotkeys"]:
            s["hotkeys"].insert(0, old)
    if not isinstance(s["hotkeys"], list) or not s["hotkeys"]:
        s["hotkeys"] = ["ctrl", "f8"]
    # left/right modifier variants can't be told apart reliably -> use the family
    s["hotkeys"] = [("ctrl" if isinstance(hk, str) and hk.endswith("ctrl") else hk)
                    for hk in s["hotkeys"]]
    # Whisper is gone — Parakeet is the engine. Drop the old knobs so a
    # stale settings.json can't resurrect them.
    for legacy in ("engine", "model", "voice_model", "language", "beam_size"):
        s.pop(legacy, None)
    return s

def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass

# ----------------------------------------------------------------------------
# Notes: every finished dictation is kept as a plain .txt in notes\
# (file ownership + sync index live in notestore.py)
# ----------------------------------------------------------------------------

from notestore import NoteStore, sanitize_title, note_title_from
import whenparse
from gcal import GCal

_STORE = None

def get_store():
    global _STORE
    if _STORE is None:
        _STORE = NoteStore(NOTES_DIR, dbg=dbg)
    return _STORE

def save_note(text):
    return get_store().create(note_title_from(text), text)


# ----------------------------------------------------------------------------
# Win32: never steal focus, hide from alt-tab, one instance only
# ----------------------------------------------------------------------------

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

def make_non_activating(widget):
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        if not hwnd:
            hwnd = widget.winfo_id()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception:
        pass

def make_titlebar_dark(win):
    """Ask DWM for a dark title bar so windows match the app."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        v = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 pre-20H1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0:
                break
    except Exception:
        pass

_mutex_handle = None

def already_running():
    """True if another DictationMic instance holds the mutex."""
    global _mutex_handle
    try:
        ERROR_ALREADY_EXISTS = 183
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(
            None, False, "DictationMic_SingleInstance")
        return ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    except Exception:
        return False

# ----------------------------------------------------------------------------
# Live recorder: cuts the stream into phrases at natural pauses
# ----------------------------------------------------------------------------

SAMPLE_RATE = 16000
BLOCK = 1024                       # 64 ms blocks
PAUSE_CUT_S = 1.0                  # silence gap that ends a phrase — a real
                                   # sentence pause, not a breath. Short gaps
                                   # were splitting sentences into fragments
                                   # the engine punctuated as "Full. Stops."
SOFT_CUT_S = 0.55                  # mid-speech dip that's enough to split at...
SOFT_CUT_AFTER_S = 7.0             # ...once the phrase is already this long
MIN_VOICED_BLOCKS = 2              # ~130 ms of speech before a phrase counts
MAX_PHRASE_S = 18                  # force a cut on very long phrases
MIC_MAX_BOOST = 12.0               # software mic gain cap (~ +21 dB)

class LiveRecorder:
    """Streams the mic and emits ("audio", phrase) items on natural pauses."""

    def __init__(self, out_queue):
        self.out = out_queue
        self.stream = None
        self.level = 0.0           # smoothed 0..1 for the animation
        self.last_voice_time = 0.0
        self._pending = []
        self._voiced_blocks = 0
        self._noise_floor = 0.004
        self._recent_voiced = deque(maxlen=2)
        self._gain = 1.0
        self._peaks = deque(maxlen=int(6 * SAMPLE_RATE / BLOCK))  # ~6s of peaks

    def start(self):
        self._pending = []
        self._voiced_blocks = 0
        self._recent_voiced.clear()
        self._peaks.clear()
        # _gain is deliberately NOT reset: the mic doesn't get louder between
        # sessions, and re-learning from 1.0 would swallow the first quiet
        # words of every dictation
        self.level = 0.0
        self.last_voice_time = time.time()
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=BLOCK, callback=self._callback)
        self.stream.start()

    def _callback(self, indata, frames, t, status):
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block ** 2)))

        # adaptive noise floor: falls fast, learns background noise slowly,
        # and barely moves during speech — otherwise long unbroken talking
        # drags the floor up until real words get classed as silence and
        # thrown away by the silence-trim below
        if rms < self._noise_floor:
            self._noise_floor = 0.8 * self._noise_floor + 0.2 * rms
        elif rms < self._noise_floor * 3.5:
            self._noise_floor = 0.995 * self._noise_floor + 0.005 * rms
        else:
            self._noise_floor = 0.9995 * self._noise_floor + 0.0005 * rms
        # the mic-boost gain divides the absolute floor: on a quiet mic (or
        # a Windows input level someone left low) soft speech used to fall
        # under the fixed 0.008 gate and get thrown away as silence — you
        # had to shout. Once the AGC learns the mic is quiet, the bar drops
        # with it; noisy rooms are still handled by the 3.5x relative gate,
        # which compares raw signal to raw floor and never sees the gain.
        threshold = max(0.008 / self._gain, self._noise_floor * 3.5)

        voiced = rms > threshold
        self._recent_voiced.append(voiced)
        now = time.time()
        if any(self._recent_voiced):
            self.last_voice_time = now

        self.level = 0.55 * self.level + 0.45 * min(1.0, (rms / max(threshold, 1e-4)) * 0.35)

        # software mic boost for the engine's benefit: aim the loudest recent
        # audio at a healthy peak so quiet mics transcribe like loud ones.
        # Adapt ONLY while the window holds real signal (well clear of the
        # noise floor) — during silence the gain HOLDS, so a thinking pause
        # can't wind it to max and drop the gate onto amplified room noise.
        raw_peak = float(np.max(np.abs(block))) if block.size else 0.0
        self._peaks.append(raw_peak)
        # learn only from blocks that were clearly signal (well above the
        # raw noise floor): silence never dilutes the estimate, so a healthy
        # mic keeps gain ~1 and behaves exactly as before this boost existed
        sig = [p for p in self._peaks if p > self._noise_floor * 6]
        if len(sig) >= 8:
            loud = sorted(sig)[int(len(sig) * 0.9)]
            want = min(MIC_MAX_BOOST, max(1.0, 0.40 / max(loud, 1e-5)))
            self._gain += 0.1 * (want - self._gain)
        if raw_peak * self._gain > 0.98:         # never clip — duck instantly
            self._gain = 0.98 / max(raw_peak, 1e-5)

        self._pending.append(block * self._gain)
        if voiced:
            self._voiced_blocks += 1

        pending_s = len(self._pending) * BLOCK / SAMPLE_RATE
        silent_for = now - self.last_voice_time

        if self._voiced_blocks >= MIN_VOICED_BLOCKS and silent_for > PAUSE_CUT_S:
            # a real pause: the phrase already carries ~1s of trailing
            # silence and the next one keeps everything from here on, so
            # nothing can be clipped at this kind of cut
            self._emit()
        elif self._voiced_blocks >= MIN_VOICED_BLOCKS and (
                (pending_s > SOFT_CUT_AFTER_S and silent_for > SOFT_CUT_S)
                or pending_s > MAX_PHRASE_S):
            # forced cut during (near-)continuous speech: never slice at
            # "now" — that lands mid-word and the engine drops both halves
            self._emit_at_quietest()
        elif self._voiced_blocks == 0 and pending_s > 3.0:
            # nothing but silence piling up — drop it
            self._pending = self._pending[-4:]

    def _emit(self):
        audio = np.concatenate(self._pending)
        self._pending = []
        self._voiced_blocks = 0
        self.out.put(("audio", audio))

    def _emit_at_quietest(self):
        """Cut at the quietest instant of the last ~1.5s instead of right
        now; the blocks after that instant seed the next phrase, so no
        audio is lost and none is duplicated."""
        tail = min(len(self._pending) - 1, int(1.5 * SAMPLE_RATE / BLOCK))
        if tail < 3:
            self._emit()
            return
        start = len(self._pending) - tail
        rms = [float(np.sqrt(np.mean(b ** 2))) for b in self._pending[start:]]
        cut = start + int(np.argmin(rms)) + 1
        carry = self._pending[cut:]
        self._pending = self._pending[:cut]
        self._emit()
        self._pending = carry
        # carried blocks are gain-boosted, the floor tracks the raw signal
        threshold = max(0.008, self._noise_floor * 3.5 * self._gain)
        self._voiced_blocks = sum(
            1 for b in carry if float(np.sqrt(np.mean(b ** 2))) > threshold)

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self._voiced_blocks >= MIN_VOICED_BLOCKS:
            self._emit()
        self._pending = []
        self.out.put(("end", None))

# ----------------------------------------------------------------------------
# Speech engine — NVIDIA's Parakeet TDT 0.6B via onnx-asr.
# Tops the open English ASR leaderboard while running faster than realtime on
# CPU, so words land sooner AND read better. One model serves live dictation
# and phone voice notes.
# ----------------------------------------------------------------------------

def model_dir(name):
    return os.path.join(APP_DIR, "models", name)

PARAKEET_NAME = "parakeet-tdt-0.6b-v2"
PARAKEET_SIZE_HINT = "~660 MB"
_PARAKEET_BASE = ("https://huggingface.co/istupakov/parakeet-tdt-0.6b-v2-onnx"
                  "/resolve/main/")
# file -> minimum plausible size, so a stray HTML error page can never pass
# for a model file
PARAKEET_FILES = {
    "config.json": 50,             # 97 B
    "vocab.txt": 5_000,            # 9.4 KB
    "decoder_joint-model.int8.onnx": 5_000_000,    # 9 MB
    "encoder-model.int8.onnx": 500_000_000,        # 652 MB
}

def parakeet_dir():
    return model_dir(PARAKEET_NAME)

def parakeet_files_ready():
    d = parakeet_dir()
    try:
        return all(os.path.getsize(os.path.join(d, n)) >= size
                   for n, size in PARAKEET_FILES.items())
    except OSError:
        return False

def fetch_resumable(url, part, progress=None):
    """One HTTP fetch into a .part file, resuming what a previous attempt
    left behind (module-level so scripts can reuse it; the pill's _fetch
    wraps it to drive the download % on the pill)."""
    existing = os.path.getsize(part) if os.path.isfile(part) else 0
    headers = {"User-Agent": "DictationMic/1.0"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        if existing and getattr(r, "status", 200) != 206:
            existing = 0                    # server ignored resume
        total = existing + int(r.headers.get("Content-Length") or 0)
        done = existing
        with open(part, "ab" if existing else "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
        if total and done < total:
            raise IOError("incomplete download")

def download_parakeet(progress=None, notify=None):
    """Fetch the Parakeet model into models\\, waiting out internet drops and
    resuming partial files. progress(frac) follows the big encoder file;
    notify(msg) surfaces one-time status toasts."""
    d = parakeet_dir()
    os.makedirs(d, exist_ok=True)
    warned = False
    for name, min_size in PARAKEET_FILES.items():
        dest = os.path.join(d, name)
        if os.path.isfile(dest) and os.path.getsize(dest) >= min_size:
            continue
        part = dest + ".part"
        big = name.startswith("encoder")
        while True:
            try:
                fetch_resumable(_PARAKEET_BASE + name,
                                part, progress if big else None)
                os.replace(part, dest)
                break
            except urllib.error.HTTPError as ex:
                if ex.code == 416:          # ranged past the end -> complete
                    os.replace(part, dest)
                    break
                if notify:
                    notify(f"Couldn't fetch the Parakeet model (HTTP {ex.code})")
                return False
            except Exception:
                if not warned:
                    warned = True
                    if notify:
                        notify("Fetching the Parakeet speech model (one-time, "
                               f"{PARAKEET_SIZE_HINT}).\nWaiting for internet…")
                time.sleep(4)
        if os.path.getsize(dest) < min_size:
            try:
                os.remove(dest)
            except OSError:
                pass
            if notify:
                notify("The Parakeet download came back broken — try again")
            return False
    return True

class ParakeetTranscriber:
    """The speech engine: load()/transcribe()/model/error, shared by the
    live-dictation worker, the warm-up and phone voice notes."""

    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.error = None

    def load(self):
        try:
            import onnxruntime as ort
            import onnx_asr
            # only HALF the cores, so the pill's meter, animation and
            # keypress handling stay snappy while we transcribe
            so = ort.SessionOptions()
            so.intra_op_num_threads = min(6, max(2, (os.cpu_count() or 8) // 2))
            so.inter_op_num_threads = 1
            # ORT worker threads busy-spin between ops and keep spinning after
            # each run by default — with a phrase transcribed every few seconds
            # that pegs the cores continuously and starves the Tk thread.
            # Sleep instead of spin: costs microseconds at 10-22x realtime
            # headroom.
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            so.add_session_config_entry("session.inter_op.allow_spinning", "0")
            self.model = onnx_asr.load_model(
                "nemo-" + PARAKEET_NAME, parakeet_dir(),
                quantization="int8", sess_options=so)
        except Exception as e:
            self.error = str(e)

    # the ONNX export runs full attention, so a 10-minute voice note in one
    # piece would eat RAM for no accuracy win — cut long audio at its
    # quietest instant, the same trick the live recorder uses for forced cuts
    CHUNK_S = 60

    def _pieces(self, audio):
        max_n = self.CHUNK_S * SAMPLE_RATE
        while len(audio) > max_n:
            win = audio[max_n - 10 * SAMPLE_RATE:max_n]
            cut = max_n - 10 * SAMPLE_RATE + int(np.argmin(np.abs(win)))
            yield audio[:cut]
            audio = audio[cut:]
        yield audio

    def transcribe(self, audio, context="", long=False):
        if self.model is None:
            return ""
        parts = []
        for piece in self._pieces(np.ascontiguousarray(audio, dtype=np.float32)):
            parts.append((self.model.recognize(piece) or "").strip())
        text = " ".join(p for p in parts if p)
        text = re.sub(r"\.{2,}|…", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        # what the model hears in a chunk of breath
        if text.lower().strip(" .,!?") in (
                "you", "thank you", "thanks for watching", "bye", "uh", "um"):
            return ""
        return text

# ----------------------------------------------------------------------------
# Typing helper: don't type while a modifier is physically held, or the held
# key turns our letters into app shortcuts (e.g. push-to-talk on Right Ctrl)
# ----------------------------------------------------------------------------

_MODIFIERS = ("ctrl", "alt", "shift", "left windows", "right windows")

# modifier families: left/right variants are indistinguishable at hook level
MOD_FAMILIES = {
    "ctrl": {"ctrl", "left ctrl", "right ctrl"},
    "alt": {"alt", "left alt", "right alt", "alt gr"},
    "shift": {"shift", "left shift", "right shift"},
    "windows": {"windows", "left windows", "right windows"},
}

def mod_family(name):
    for fam, names in MOD_FAMILIES.items():
        if name in names:
            return fam
    return None

# Keys whose only "side-effect" is flipping a sticky Windows state. If one of
# these is chosen as the talk key we suppress it at the hook so a press ONLY
# starts/stops dictation — otherwise every tap would also toggle Caps/Num/
# Scroll Lock (leaving you typing in CAPITALS) or Insert (overtype mode).
SIDE_EFFECT_KEYS = {"caps lock", "num lock", "scroll lock", "insert"}

def wait_modifiers_up(timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        held = False
        for m in _MODIFIERS:
            try:
                if keyboard.is_pressed(m):
                    held = True
                    break
            except Exception:
                pass
        if not held:
            return
        time.sleep(0.05)

# ----------------------------------------------------------------------------
# Rendering (Pillow, supersampled) — "Obsidian Capsule" (see DESIGN-DESKTOP.md):
# lacquered obsidian pebble, bone ink, one volt-lime accent.
# ----------------------------------------------------------------------------

SS = 3  # supersampling factor

C_TRANSPARENT = (1, 2, 3, 255)

BODY_TOP = (31, 34, 32)                  # lacquer gradient, top -> bottom
BODY_BOT = (14, 16, 14)                  # (green undertone, like the web app)
EDGE_IDLE = (236, 238, 231, 26)          # hairline rim, bone ink
EDGE_DIM = (236, 238, 231, 14)
LIME = (182, 238, 63)                    # volt — THE accent (#B6EE3F)
ICE = (86, 197, 255)                     # "caught it" flash after a drop/paste
INK = (236, 238, 231)                    # bone white
DOT_SLEEP = INK + (77,)                  # 3 sleeping dots at rest
DOT_SLEEP_HOVER = INK + (140,)
DOT_SLEEP_DIM = INK + (46,)
DOT_THINK = LIME + (200,)                # "your words are coming" dots
TEXT_SOFT = INK + (255,)
TRACK = (255, 255, 255, 34)              # download progress track


def pil_font(px, names=("seguisb.ttf", "segoeuib.ttf", "segoeui.ttf",
                        "arialbd.ttf")):
    """A Windows font at px height for PIL drawing, or None."""
    for name in names:
        try:
            return ImageFont.truetype(
                os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                             "Fonts", name), int(px))
        except Exception:
            continue
    return None


def round_corners(win):
    """Ask DWM for rounded corners on a Toplevel (Windows 11)."""
    try:
        win.update_idletasks()
        hwnd = (ctypes.windll.user32.GetParent(win.winfo_id())
                or win.winfo_id())
        pref = ctypes.c_int(2)            # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(pref), 4)
    except Exception:
        pass


def fade_in(win, steps=5, ms=16):
    """~110ms alpha ramp — the only entrance motion any card gets."""
    try:
        win.attributes("-alpha", 1.0 / steps)
    except Exception:
        return

    def step(i=2):
        try:
            win.attributes("-alpha", min(1.0, i / steps))
            if i < steps:
                win.after(ms, step, i + 1)
        except Exception:
            pass
    win.after(ms, step)


def spaced(text):
    """'Shots' -> 'S H O T S' — mono eyebrows, Tk has no letter-spacing."""
    return " ".join(text.upper())


# Tk font families — resolved once a root exists (pick_ui_fonts); the
# Variable/Cascadia families ship with Windows 11 and echo the web app's
# Space Grotesk / JetBrains Mono without bundling a single font file.
UI_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


def pick_ui_fonts(root):
    global UI_FAMILY, MONO_FAMILY
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
        for cand in ("Segoe UI Variable Text", "Segoe UI Variable Display"):
            if cand in fams:
                UI_FAMILY = cand
                break
        for cand in ("Cascadia Mono", "Cascadia Code"):
            if cand in fams:
                MONO_FAMILY = cand
                break
    except Exception:
        pass


class PillRenderer:
    """Draws the capsule in every state. frame_* methods return PIL images
    (testable without Tk); the public methods wrap them as PhotoImages."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.sw, self.sh = width * SS, height * SS
        self.pad = SS                        # room so the rim's AA isn't clipped
        self.edge_w = max(2, round(SS * 1.1))
        # meter layout: bars stay clear of the round end caps
        self.nbars = 11
        inset = self.sh * 0.55
        span = self.sw - 2 * inset
        self.bar_w = span / (self.nbars * 1.85 - 0.85)
        self.bar_gap = self.bar_w * 0.85
        self.bar_x0 = inset
        self.max_half = self.sh * 0.27       # bar half-height at full voice
        self.nub_half = max(2.0, self.sh * 0.042)   # sleeping dot half-height
        self._bodies = {}
        self._static = {}
        self._font = None
        for name in ("seguisb.ttf", "segoeuib.ttf", "segoeui.ttf", "arialbd.ttf"):
            try:
                self._font = ImageFont.truetype(
                    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name),
                    int(self.sh * 0.40))
                break
            except Exception:
                continue

    # ---- cached capsule bodies ----

    def _body(self, key, edge_rgba):
        if key in self._bodies:
            return self._bodies[key]
        sw, sh, p = self.sw, self.sh, self.pad
        yy = np.linspace(0.0, 1.0, sh, dtype=np.float32)[:, None]
        arr = np.empty((sh, sw, 4), np.uint8)
        for i in range(3):
            col = (BODY_TOP[i] + (BODY_BOT[i] - BODY_TOP[i]) * yy).astype(np.uint8)
            arr[..., i] = np.broadcast_to(col, (sh, sw))
        arr[..., 3] = 255
        grad = Image.fromarray(arr, "RGBA")
        box = [p, p, sw - p, sh - p]
        radius = (sh - 2 * p) / 2
        mask = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
        body = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        body.paste(grad, (0, 0), mask)
        # the lacquer: a soft top sheen + a bottom inner shade, clipped to
        # the capsule, give the flat gradient its "polished pebble" depth
        lac = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lac)
        ld.ellipse([-sw * 0.25, -sh * 0.95, sw * 1.25, sh * 0.52],
                   fill=(255, 255, 255, 23))
        ld.rectangle([0, sh * 0.72, sw, sh], fill=(0, 0, 0, 30))
        lac.putalpha(Image.composite(
            lac.getchannel("A"), Image.new("L", (sw, sh), 0), mask))
        body.alpha_composite(lac)
        d = ImageDraw.Draw(body)
        d.rounded_rectangle(box, radius=radius, outline=edge_rgba, width=self.edge_w)
        self._bodies[key] = body
        return body

    # ---- shared meter ----

    def _bars(self, d, vals, color, glow=None):
        cy = self.sh / 2
        if glow:                            # volt under-glow, drawn first
            x = self.bar_x0
            gx = self.bar_w * 0.45
            for v in vals:
                half = self.nub_half + (self.max_half - self.nub_half) \
                    * max(0.0, min(1.0, v))
                d.rounded_rectangle(
                    [x - gx, cy - half - gx, x + self.bar_w + gx,
                     cy + half + gx],
                    radius=self.bar_w / 2 + gx, fill=glow)
                x += self.bar_w + self.bar_gap
        x = self.bar_x0
        for v in vals:
            half = self.nub_half + (self.max_half - self.nub_half) * max(0.0, min(1.0, v))
            d.rounded_rectangle([x, cy - half, x + self.bar_w, cy + half],
                                radius=self.bar_w / 2, fill=color)
            x += self.bar_w + self.bar_gap

    def _sleep_dots(self, body, color):
        """The brand mark at rest: 3 dots, blended onto the lacquer."""
        d = ImageDraw.Draw(body, "RGBA")
        cy = self.sh / 2
        r = self.sh * 0.058
        gap = self.sh * 0.36
        for i in (-1, 0, 1):
            x = self.sw / 2 + i * gap
            d.ellipse([x - r, cy - r, x + r, cy + r], fill=color)

    # ---- frames (pure PIL) ----

    def _compose(self, img):
        canvas = Image.new("RGBA", (self.sw, self.sh), C_TRANSPARENT)
        canvas.alpha_composite(img)
        return canvas.resize((self.w, self.h), Image.LANCZOS)

    def _finish(self, img):
        return tk_photo(self._compose(img))

    def _finish_fast(self, img):
        """Animated frames: raw PPM into Tk — no PNG deflate, no base64,
        no PNG decode. ~10x cheaper per frame at 30fps than _finish."""
        rgb = self._compose(img).convert("RGB")
        data = b"P6\n%d %d\n255\n" % rgb.size + rgb.tobytes()
        return tk.PhotoImage(data=data, format="ppm")

    def frame_idle(self, hover, dim=False):
        if dim:
            body, dot = self._body("dim", EDGE_DIM).copy(), DOT_SLEEP_DIM
        elif hover:
            # volt rim the moment the pointer touches the pill: it's armed —
            # a click talks, Ctrl+V / middle-click pastes, a drag drops in
            body, dot = self._body("hover", LIME + (140,)).copy(), DOT_SLEEP_HOVER
        else:
            body, dot = self._body("idle", EDGE_IDLE).copy(), DOT_SLEEP
        self._sleep_dots(body, dot)
        return body

    def frame_listening(self, vals, pulse):
        step = min(3, int(max(0.0, pulse) * 4))     # quantized so bodies cache
        body = self._body(("listen", step), LIME + (95 + step * 16,)).copy()
        self._bars(ImageDraw.Draw(body, "RGBA"), vals, LIME + (255,),
                   glow=LIME + (52,))
        return body

    def frame_dots(self, phase):
        body = self._body("idle", EDGE_IDLE).copy()
        d = ImageDraw.Draw(body, "RGBA")
        cy = self.sh / 2
        r0 = self.sh * 0.065
        gap = self.sh * 0.40
        for i in range(3):
            k = 0.6 + 0.4 * math.sin(phase - i * 0.9)
            r = r0 * (0.55 + 0.75 * k)
            x = self.sw / 2 + (i - 1) * gap
            d.ellipse([x - r, cy - r, x + r, cy + r], fill=DOT_THINK)
        return body

    def frame_drop(self):
        """Drag hovering over the pill: full lime ring + arrow-into-tray,
        so there's no doubt it will catch the drop. Static on purpose."""
        body = self._body("drop", LIME + (235,)).copy()
        d = ImageDraw.Draw(body)
        p = self.pad
        d.rounded_rectangle([p, p, self.sw - p, self.sh - p],
                            radius=(self.sh - 2 * p) / 2,
                            outline=LIME + (235,), width=self.edge_w * 2)
        cx = self.sw / 2
        ah = self.sh * 0.50                      # glyph box height
        top = self.sh / 2 - ah / 2
        shaft = max(2.0, self.sh * 0.075)
        head = self.sh * 0.17
        tip = top + ah * 0.68
        lime = LIME + (255,)
        d.rounded_rectangle([cx - shaft / 2, top, cx + shaft / 2,
                             tip - head * 0.7], radius=shaft / 2, fill=lime)
        d.polygon([(cx - head, tip - head), (cx + head, tip - head),
                   (cx, tip)], fill=lime)
        ty = top + ah                            # the tray it drops into
        d.rounded_rectangle([cx - head * 1.55, ty - shaft, cx + head * 1.55, ty],
                            radius=shaft / 2, fill=lime)
        return body

    def frame_flash(self):
        """A completely different colour the instant a drop/paste lands —
        full ice-blue ring + tick, so there's no doubt the pill caught it
        (the lime states all mean voice/drop-armed; blue means 'saved')."""
        body = self._body("flash", ICE + (235,)).copy()
        d = ImageDraw.Draw(body)
        p = self.pad
        d.rounded_rectangle([p, p, self.sw - p, self.sh - p],
                            radius=(self.sh - 2 * p) / 2,
                            outline=ICE + (235,), width=self.edge_w * 2)
        cx, cy = self.sw / 2, self.sh / 2
        u = self.sh * 0.16                       # tick glyph scale
        w = max(2.0, self.sh * 0.075)
        ice = ICE + (255,)
        d.line([(cx - 1.6 * u, cy), (cx - 0.4 * u, cy + u),
                (cx + 1.7 * u, cy - 1.1 * u)], fill=ice, width=int(w),
               joint="curve")
        return body

    def frame_downloading(self, frac):
        body = self._body("dim", EDGE_DIM).copy()
        d = ImageDraw.Draw(body)
        frac = max(0.0, min(1.0, frac))
        x0, x1 = self.bar_x0, self.sw - self.bar_x0
        th = max(2.0, self.sh * 0.05)
        ty = self.sh * 0.70
        d.rounded_rectangle([x0, ty - th, x1, ty + th], radius=th, fill=TRACK)
        fx = x0 + max(th * 2.2, (x1 - x0) * frac)
        d.rounded_rectangle([x0, ty - th, fx, ty + th], radius=th, fill=LIME + (255,))
        if self._font is not None:
            d.text((self.sw / 2, self.sh * 0.36), f"{int(frac * 100)}%",
                   font=self._font, fill=TEXT_SOFT, anchor="mm")
        return body

    # ---- PhotoImage wrappers ----

    def idle(self, hover, dim=False):
        key = ("idle", hover, dim)
        if key not in self._static:
            self._static[key] = self._finish(self.frame_idle(hover, dim))
        return self._static[key]

    def drop(self):
        if "drop" not in self._static:
            self._static["drop"] = self._finish(self.frame_drop())
        return self._static["drop"]

    def flash(self):
        if "flash" not in self._static:
            self._static["flash"] = self._finish(self.frame_flash())
        return self._static["flash"]

    def listening(self, vals, pulse):
        return self._finish_fast(self.frame_listening(vals, pulse))

    def dots(self, phase):
        return self._finish_fast(self.frame_dots(phase))

    def downloading(self, frac):
        return self._finish_fast(self.frame_downloading(frac))


# ----------------------------------------------------------------------------
# Right-click menu — the "command card": a matte obsidian card cut from the
# same stone as the pill. (tk.Menu draws like Windows 95 and can't be styled.)
# ----------------------------------------------------------------------------

MENU_BG = "#131512"
MENU_EDGE = "#23251F"
MENU_HOVER = "#1A1D18"
MENU_FG = "#ECEEE7"
MENU_SUB = "#878C7F"
MENU_DIM = "#5C6156"
MENU_LIME = "#B6EE3F"
MENU_RED = "#FF5C48"
MENU_GREEN = "#B6EE3F"       # "on" is an accent state — volt, not a 2nd green


class PopupMenu:
    """items is a list of dicts:
      {"kind": "hero", "text": ..., "hint": ..., "command": fn}   volt capsule
      {"kind": "header", "text": ...}                       mono eyebrow label
      {"kind": "sep"}                                       hairline
      {"kind": "status", "text": ..., "bullet": "#hex",
       "hint": ...}                                         non-clickable info
      {"kind": "item", "text": ..., "command": fn,
       "hint": ..., "radio": bool|None, "check": bool|None,
       "bullet": "#hex"|None, "danger": bool}               clickable row
    """

    PAD_X = 16

    def __init__(self, parent, items, on_close=None):
        self._root = parent
        self.on_close = on_close
        self.closed = False
        self._hero_photos = []        # PhotoImage refs must stay alive
        self._heroes = []             # (placeholder label, item) — sized late
        self.win = tk.Toplevel(parent, bg=MENU_BG)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(highlightthickness=1,
                           highlightbackground=MENU_EDGE,
                           highlightcolor=MENU_EDGE)
        self._prev_buttons = True     # swallow the click that opened us
        self._body = tk.Frame(self.win, bg=MENU_BG)
        self._body.pack(fill="both", expand=True, pady=7)
        for it in items:
            self._add(self._body, it)
        self._finish_heroes(self._body)
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<FocusOut>", lambda e: self.close())

    def _add(self, body, it):
        kind = it.get("kind", "item")
        if kind == "sep":
            tk.Frame(body, bg=MENU_EDGE, height=1).pack(
                fill="x", padx=10, pady=6)
            return
        if kind == "hero":
            lab = tk.Label(body, bg=MENU_BG, bd=0, cursor="hand2")
            lab.pack(padx=10, pady=(2, 6))
            self._heroes.append((lab, it))
            return
        if kind == "header":
            tk.Label(body, text=spaced(it["text"]), bg=MENU_BG, fg=MENU_DIM,
                     font=(MONO_FAMILY, 7), anchor="w"
                     ).pack(fill="x", padx=self.PAD_X, pady=(5, 1))
            return
        row = tk.Frame(body, bg=MENU_BG)
        row.pack(fill="x")
        lead_txt, lead_fg = "", MENU_DIM
        if it.get("radio") is not None:
            lead_txt = "●" if it["radio"] else "○"
            lead_fg = MENU_LIME if it["radio"] else MENU_DIM
        elif it.get("check") is not None:
            lead_txt = "✓" if it["check"] else ""
            lead_fg = MENU_LIME
        elif it.get("bullet"):
            lead_txt, lead_fg = "●", it["bullet"]
        widgets = [row]
        lead = tk.Label(row, text=lead_txt, width=2, bg=MENU_BG, fg=lead_fg,
                        font=(UI_FAMILY, 10), anchor="w")
        lead.pack(side="left", padx=(self.PAD_X - 6, 0), pady=4)
        widgets.append(lead)
        fg = (MENU_RED if it.get("danger")
              else MENU_SUB if kind == "status" else MENU_FG)
        lab = tk.Label(row, text=it["text"], bg=MENU_BG, fg=fg,
                       font=(UI_FAMILY, 10), anchor="w")
        lab.pack(side="left", pady=4)
        widgets.append(lab)
        tail = tk.Label(row, text=it.get("hint", ""), bg=MENU_BG, fg=MENU_DIM,
                        font=(MONO_FAMILY, 7), anchor="e")
        tail.pack(side="right", padx=(24, self.PAD_X), pady=4)
        widgets.append(tail)
        cmd = it.get("command") if kind == "item" else None
        if cmd is not None:
            def set_bg(color, ws=widgets):
                for w in ws:
                    w.configure(bg=color)
            for w in widgets:
                w.configure(cursor="hand2")
                w.bind("<Enter>", lambda e: set_bg(MENU_HOVER))
                w.bind("<Leave>", lambda e: set_bg(MENU_BG))
                w.bind("<ButtonRelease-1>", lambda e, c=cmd: self._invoke(c))

    # ---- the hero: a PIL-drawn volt capsule button, sized to the card ----

    def _hero_frame(self, w, h, text, hint, hover):
        s = SS
        img = Image.new("RGB", (w * s, h * s), (19, 21, 18))   # MENU_BG
        d = ImageDraw.Draw(img, "RGBA")
        box = [s, s, w * s - s, h * s - s]
        rad = (h * s - 2 * s) / 2
        d.rounded_rectangle(box, radius=rad,
                            fill=LIME + (46 if hover else 26,),
                            outline=LIME + (220 if hover else 120,),
                            width=max(2, round(s * 1.1)))
        f_main = pil_font(h * s * 0.34)
        f_hint = pil_font(h * s * 0.20, names=("CascadiaMono.ttf",
                                               "CascadiaCode.ttf",
                                               "consola.ttf", "segoeui.ttf"))
        cx = w * s / 2
        if hint and f_hint is not None:
            d.text((cx, h * s * 0.36), text, font=f_main,
                   fill=LIME + (255,), anchor="mm")
            d.text((cx, h * s * 0.70), spaced(hint), font=f_hint,
                   fill=INK + (120,), anchor="mm")
        else:
            d.text((cx, h * s / 2), text, font=f_main,
                   fill=LIME + (255,), anchor="mm")
        return tk_photo(img.resize((w, h), Image.LANCZOS))

    def _finish_heroes(self, body):
        if not self._heroes:
            return
        body.update_idletasks()
        w = max(240, body.winfo_reqwidth() - 20)
        for lab, it in self._heroes:
            h = 44 if it.get("hint") else 34
            normal = self._hero_frame(w, h, it["text"], it.get("hint"), False)
            hover = self._hero_frame(w, h, it["text"], it.get("hint"), True)
            self._hero_photos += [normal, hover]
            lab.configure(image=normal)
            lab.bind("<Enter>", lambda e, l=lab, p=hover: l.configure(image=p))
            lab.bind("<Leave>", lambda e, l=lab, p=normal: l.configure(image=p))
            lab.bind("<ButtonRelease-1>",
                     lambda e, c=it.get("command"): c and self._invoke(c))

    def _invoke(self, cmd):
        root = self._root
        self.close()
        root.after(10, cmd)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except Exception:
            pass
        if self.on_close:
            self.on_close()

    def _hero_center_y(self):
        """Vertical centre of the first hero capsule, relative to the card's
        top edge — so show() can put the capsule exactly where the pill is."""
        if not self._heroes:
            return None
        lab = self._heroes[0][0]
        return self._body.winfo_y() + lab.winfo_y() + lab.winfo_height() // 2

    def show(self, x, y, anchor=None):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        if anchor:
            # the card replaces the pill: the hero capsule opens dead on the
            # pill's spot (the pill hides while we're open), so right-click
            # reads as the pill unfolding into the card and folding back
            ax, ay, aw, ah = anchor
            x = ax + (aw - w) // 2
            cy = self._hero_center_y()
            if cy is not None:
                y = ay + ah // 2 - cy
            else:
                y = ay - h + 2 if ay - h + 2 >= 8 else ay + ah - 2
        elif y - h > 8:               # pill lives near the bottom: open upward
            y = y - h
        x = max(8, min(x, sw - w - 8))
        y = max(8, min(y, sh - h - 8))
        self.win.geometry(f"+{x}+{y}")
        round_corners(self.win)
        fade_in(self.win)
        self.win.lift()
        try:
            self.win.focus_force()
        except Exception:
            pass
        self._watch_outside_click()

    def _watch_outside_click(self):
        """Win32 backstop: a fresh mouse press outside the menu closes it —
        FocusOut alone can't be trusted around a WS_EX_NOACTIVATE owner."""
        if self.closed:
            return
        try:
            down = any(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
                       for vk in (0x01, 0x02, 0x04))
            if down and not self._prev_buttons:
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                x0, y0 = self.win.winfo_rootx(), self.win.winfo_rooty()
                if not (x0 <= pt.x < x0 + self.win.winfo_width()
                        and y0 <= pt.y < y0 + self.win.winfo_height()):
                    self.close()
                    return
            self._prev_buttons = down
        except Exception:
            pass
        try:
            self.win.after(80, self._watch_outside_click)
        except tk.TclError:
            pass                  # window already gone (app exiting)


# ----------------------------------------------------------------------------
# Screenshot shelf UI — the badge on the pill's shoulder + the thumbnail
# tray it opens. The plumbing (folder, clipboard, OLE drag-out) is shots.py.
# ----------------------------------------------------------------------------

MENU_BG_RGB = (19, 21, 18)               # PIL twin of MENU_BG "#131512"


class ShotBadge:
    """The little button that appears on the pill's shoulder the moment a
    screenshot is pinned: a dark disc with the count. Lime ring while
    there's something you haven't looked at; settles to grey once the
    shelf has been opened. Click = open / close the shelf."""

    def __init__(self, app):
        self.app = app
        self.d = max(20, round(app.height * 0.74))
        self.win = tk.Toplevel(app.root, bg=TRANSPARENT_HEX)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", TRANSPARENT_HEX)
        self.label = tk.Label(self.win, bg=TRANSPARENT_HEX, bd=0,
                              cursor="hand2")
        self.label.pack()
        for seq in ("<ButtonRelease-1>", "<ButtonRelease-3>"):
            self.label.bind(seq, lambda e: app.toggle_shots_window())
        self._frames = {}
        self._photo = None
        self._font = None
        self.visible = False
        self.win.withdraw()
        self.win.update_idletasks()
        make_non_activating(self.win)

    def _frame(self, count, fresh):
        key = (count, fresh)
        if key in self._frames:
            return self._frames[key]
        s = self.d * SS
        img = Image.new("RGBA", (s, s), C_TRANSPARENT)
        d = ImageDraw.Draw(img)
        ring = LIME + (235,) if fresh else INK + (64,)
        d.ellipse([SS, SS, s - SS, s - SS], fill=MENU_BG_RGB + (255,),
                  outline=ring, width=max(2, round(SS * 1.3)))
        txt = "9+" if count > 9 else str(count)
        if self._font is None:
            self._font = pil_font(s * 0.42, names=(
                "CascadiaMono.ttf", "CascadiaCode.ttf", "consola.ttf",
                "seguisb.ttf", "segoeui.ttf"))
        fill = LIME + (255,) if fresh else INK + (190,)
        d.text((s / 2, s / 2 + SS * 0.3), txt, font=self._font,
               fill=fill, anchor="mm")
        ph = tk_photo(img.resize((self.d, self.d), Image.LANCZOS))
        self._frames[key] = ph
        return ph

    def place(self):
        """Sit on the pill's top-right shoulder, wherever the pill goes."""
        r = self.app.root
        x = r.winfo_x() + self.app.width - round(self.d * 0.60)
        y = r.winfo_y() - round(self.d * 0.42)
        x = max(0, min(x, r.winfo_screenwidth() - self.d))
        y = max(0, min(y, r.winfo_screenheight() - self.d))
        self.win.geometry(f"{self.d}x{self.d}+{x}+{y}")

    def hide(self):
        if self.visible:
            self.win.withdraw()
            self.visible = False

    def refresh(self):
        count = self.app.shots.count()
        if count <= 0:
            if self.visible:
                self.win.withdraw()
                self.visible = False
            return
        self._photo = self._frame(count, self.app.shots_fresh)
        self.label.configure(image=self._photo)
        self.place()
        if not self.visible:
            self.win.deiconify()
            self.visible = True
        self.win.lift()


class ShotsWindow:
    """The shelf, popped open: recent screenshots as thumbnails. Drag one
    straight into Claude Code / a chat / an email — a real OLE file drag —
    or click it to copy (file + bitmap both land on the clipboard, so
    Ctrl+V pastes whichever the target prefers). Hover ✕ removes."""

    COLS = 4
    TW, TH = 96, 72

    # thumbnails cache across opens, keyed (path, mtime) — opening the shelf
    # must never re-render a tile it has already drawn (that was the lag)
    _thumb_cache = {}

    def __init__(self, app, on_close=None):
        self.app = app
        self.on_close = on_close
        self.closed = False
        self._drag_active = False
        self._prev_buttons = True     # swallow the click that opened us
        self._photos = []             # PhotoImage refs must stay alive
        self.win = tk.Toplevel(app.root, bg=MENU_BG)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(highlightthickness=1,
                           highlightbackground=MENU_EDGE,
                           highlightcolor=MENU_EDGE)
        self.body = tk.Frame(self.win, bg=MENU_BG)
        self.body.pack(fill="both", expand=True, padx=10, pady=(10, 8))
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<FocusOut>", lambda e: self.close())
        self.rebuild()

    # ---- thumbnails ----

    def _thumbs(self, path):
        """(normal, hover, copied) PhotoImages for one shot — cover-cropped,
        rounded; hover adds the lime ring + ✕, copied flashes ice-blue."""
        tw, th = self.TW * SS, self.TH * SS
        img = Image.open(path)
        img.load()
        scale = max(tw / img.width, th / img.height)
        img = img.convert("RGB").resize(
            (max(tw, round(img.width * scale)),
             max(th, round(img.height * scale))), Image.LANCZOS)
        left, top = (img.width - tw) // 2, (img.height - th) // 2
        img = img.crop((left, top, left + tw, top + th))
        base = Image.new("RGBA", (tw, th), MENU_BG_RGB + (255,))
        mask = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, tw - 1, th - 1], radius=9 * SS, fill=255)
        base.paste(img, (0, 0), mask)

        def ringed(color, cross):
            im = base.copy()
            d = ImageDraw.Draw(im)
            d.rounded_rectangle([1, 1, tw - 2, th - 2], radius=9 * SS,
                                outline=color, width=SS * 2)
            if cross:                        # ✕ disc, top-right of the tile
                cx, cy, r = tw - 12 * SS, 12 * SS, 8 * SS
                d.ellipse([cx - r, cy - r, cx + r, cy + r],
                          fill=(12, 13, 16, 235))
                a = r * 0.42
                for sx in (-1, 1):
                    d.line([(cx - a * sx, cy - a), (cx + a * sx, cy + a)],
                           fill=(235, 238, 242, 255), width=SS)
            return tk_photo(im.resize((self.TW, self.TH), Image.LANCZOS))

        return (ringed(INK + (38,), False),
                ringed(LIME + (220,), True),
                ringed(ICE + (235,), False))

    def _thumbs_cached(self, path):
        key = (path, os.path.getmtime(path))
        got = self._thumb_cache.get(key)
        if got is None:
            got = self._thumbs(path)
            if len(self._thumb_cache) > 96:      # stale keys from pruned shots
                self._thumb_cache.clear()
            self._thumb_cache[key] = got
        return got

    def rebuild(self):
        if self.closed:
            return
        for w in self.body.winfo_children():
            w.destroy()
        self._photos.clear()
        paths = self.app.shots.paths()
        head = tk.Frame(self.body, bg=MENU_BG)
        head.pack(fill="x", pady=(0, 6))
        tk.Label(head, text=spaced("Shots"), bg=MENU_BG, fg=MENU_DIM,
                 font=(MONO_FAMILY, 7)).pack(side="left")
        tk.Label(head, text=f"· {len(paths)}", bg=MENU_BG, fg=MENU_SUB,
                 font=(MONO_FAMILY, 7)).pack(side="left", padx=(6, 0))
        if not paths:
            tk.Label(self.body, text="Nothing pinned — take a screenshot",
                     bg=MENU_BG, fg=MENU_DIM,
                     font=(UI_FAMILY, 10)).pack(padx=16, pady=12)
        else:
            grid = tk.Frame(self.body, bg=MENU_BG)
            grid.pack()
            for i, p in enumerate(paths):
                self._tile(grid, p, i)
        foot = tk.Frame(self.body, bg=MENU_BG)
        foot.pack(fill="x", pady=(8, 0))
        tk.Label(foot, text="drag out · click = copy",
                 bg=MENU_BG, fg=MENU_DIM,
                 font=(MONO_FAMILY, 7)).pack(side="left")
        if paths:
            self._foot_btn(foot, "Clear all", MENU_RED, self._clear)
            self._foot_btn(foot, "Open folder", MENU_SUB, self._open_folder)
        self.win.update_idletasks()

    def _foot_btn(self, foot, text, fg, cmd):
        b = tk.Label(foot, text=text, bg=MENU_BG, fg=fg,
                     font=(MONO_FAMILY, 7), cursor="hand2")
        b.pack(side="right", padx=(14, 0))
        b.bind("<Enter>", lambda e: b.configure(fg=MENU_FG))
        b.bind("<Leave>", lambda e: b.configure(fg=fg))
        b.bind("<ButtonRelease-1>", lambda e: cmd())

    def _tile(self, grid, path, i):
        try:
            normal, hover, copied = self._thumbs_cached(path)
        except Exception:
            return                       # unreadable file — skip the tile
        self._photos += [normal, hover, copied]
        lab = tk.Label(grid, image=normal, bg=MENU_BG, bd=0, cursor="hand2")
        lab.grid(row=i // self.COLS, column=i % self.COLS, padx=4, pady=4)
        state = {"press": None, "dragged": False}

        def on_enter(e):
            lab.configure(image=hover)

        def on_leave(e):
            lab.configure(image=normal)

        def on_press(e):
            state["press"] = (e.x_root, e.y_root)
            state["dragged"] = False

        def on_motion(e):
            if state["press"] is None or state["dragged"]:
                return
            dx = e.x_root - state["press"][0]
            dy = e.y_root - state["press"][1]
            if abs(dx) > 5 or abs(dy) > 5:
                state["dragged"] = True
                self._drag_active = True
                try:
                    shots.drag_shots([path], dbg)
                finally:
                    self._drag_active = False
                    self.app.note_own_clipboard()

        def on_release(e):
            if state["press"] is None or state["dragged"]:
                state["press"] = None
                return
            state["press"] = None
            if e.x > self.TW - 24 and e.y < 24:      # the hover ✕
                self.app.shots.remove(path)
                self.app.refresh_shot_badge()
                self.rebuild()
                return
            if shots.copy_shots([path]):
                self.app.note_own_clipboard()
                lab.configure(image=copied)
                lab.after(450, lambda: lab.configure(image=normal))
                self.app.show_toast(
                    "Copied — Ctrl+V pastes the image (or the file)", 2200)
            else:
                self.app.show_toast("Couldn't copy that — clipboard busy",
                                    2200)

        lab.bind("<Enter>", on_enter)
        lab.bind("<Leave>", on_leave)
        lab.bind("<ButtonPress-1>", on_press)
        lab.bind("<B1-Motion>", on_motion)
        lab.bind("<ButtonRelease-1>", on_release)

    # ---- footer actions ----

    def _clear(self):
        self.app.shots.clear()
        self.app.refresh_shot_badge()
        self.close()

    def _open_folder(self):
        os.makedirs(self.app.shots.folder, exist_ok=True)
        os.startfile(self.app.shots.folder)

    # ---- window plumbing (same patterns as PopupMenu) ----

    def close(self):
        if self.closed or self._drag_active:
            return
        self.closed = True
        try:
            self.win.destroy()
        except Exception:
            pass
        if self.on_close:
            self.on_close()

    def show(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        r = self.app.root
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        x = r.winfo_x() + self.app.width - w        # right edges aligned
        y = r.winfo_y() - h - 10                    # prefer above the pill
        if y < 8:
            y = r.winfo_y() + self.app.height + 10
        x = max(8, min(x, sw - w - 8))
        y = max(8, min(y, sh - h - 8))
        self.win.geometry(f"+{x}+{y}")
        round_corners(self.win)
        fade_in(self.win)
        self.win.lift()
        try:
            self.win.focus_force()
        except Exception:
            pass
        self._watch_outside_click()

    def _watch_outside_click(self):
        if self.closed:
            return
        try:
            down = any(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
                       for vk in (0x01, 0x02, 0x04))
            if down and not self._prev_buttons and not self._drag_active:
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                x0, y0 = self.win.winfo_rootx(), self.win.winfo_rooty()
                if not (x0 <= pt.x < x0 + self.win.winfo_width()
                        and y0 <= pt.y < y0 + self.win.winfo_height()):
                    self.close()
                    return
            self._prev_buttons = down
        except Exception:
            pass
        try:
            self.win.after(80, self._watch_outside_click)
        except tk.TclError:
            pass


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------

IDLE, LOADING, STARTING, LISTENING, FINISHING, DOWNLOADING = (
    "idle", "loading", "starting", "listening", "finishing", "downloading")
TRANSPARENT_HEX = "#010203"


class DictationApp:
    def __init__(self):
        self.settings = load_settings()
        self.width = max(64, int(self.settings.get("size") or 84))
        self.height = max(24, round(self.width * 0.36))
        self.events = queue.Queue()     # UI events
        self.work = queue.Queue()       # audio phrases -> transcriber worker
        self.recorder = LiveRecorder(self.work)
        self.transcriber = ParakeetTranscriber(self.settings)
        self.session_text = []
        self.context = ""
        self.session_start = 0.0
        self.dl_frac = 0.0

        self.root = tk.Tk() if TkinterDnD is None else TkinterDnD.Tk()
        pick_ui_fonts(self.root)
        self.root.title("DictationMic")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT_HEX)
        self.root.configure(bg=TRANSPARENT_HEX)

        x, y = self.settings.get("x"), self.settings.get("y")
        if x is None or y is None:
            x = (self.root.winfo_screenwidth() - self.width) // 2
            y = self.root.winfo_screenheight() - self.height - 100
        x = max(0, min(int(x), self.root.winfo_screenwidth() - self.width))
        y = max(0, min(int(y), self.root.winfo_screenheight() - self.height))
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")

        self.renderer = PillRenderer(self.width, self.height)
        self.label = tk.Label(self.root, bg=TRANSPARENT_HEX, bd=0, cursor="hand2")
        self.label.pack()
        self._photo = None

        self.hover = False
        self.drop_hover = False        # a drag is over the pill right now
        self.drag_start = None
        self.dragging = False
        self.phase = 0.0
        self.level_hist = deque([0.0] * 24, maxlen=24)
        self.key_is_down = False
        self.key_press_time = 0.0
        self.key_started = False
        self._mod_down = False
        self._mod_t = 0.0
        self._mod_other = False
        self._mod_ptt = False
        self.toast = None
        self.tooltip = None
        self._tooltip_job = None
        self.flash_until = 0.0         # pill shows the blue "caught it" ring
        self._capturing = False
        self._stop_when_ready = False
        self._suppress_removers = []   # per-key hooks that swallow OS side-effects

        self.label.bind("<ButtonPress-1>", self.on_press)
        self.label.bind("<B1-Motion>", self.on_motion)
        self.label.bind("<ButtonRelease-1>", self.on_release)
        self.label.bind("<ButtonRelease-2>", lambda e: self.save_clipboard_note())
        self.label.bind("<ButtonRelease-3>", self.on_right_click)
        self.label.bind("<Enter>", self.on_enter)
        self.label.bind("<Leave>", self.on_leave)
        self._install_drop_targets()

        # screenshot shelf: real PNGs in shots\, watched off the clipboard
        self.shots = shots.ShotShelf(os.path.join(APP_DIR, "shots"),
                                     keep=self.settings.get("shots_keep"))
        self.shots_fresh = False
        self._shots_win = None
        self._clip_seq = shots.clip_seq()
        self._clip_last_check = 0.0
        self._clip_busy = False
        self._frame_key = None         # draw() skips repaints of the same frame

        self.local_server = None
        self.cloud = None
        self._menu = None
        # the reverse of shots_to_notes: an image note arriving from the
        # phone is pinned to the shelf like a local screenshot
        get_store().subscribe(self._on_remote_note)
        # "add to calendar" watcher: every note the store sees — dictated
        # here, typed in the web app, or arriving from the phone — runs
        # through one detector; a worker thread does the network bit
        self.gcal = GCal(self.settings, save_settings, dbg=dbg)
        self._cal_q = queue.Queue()
        self._cal_inflight = set()
        self._cal_notify_last = 0.0
        get_store().subscribe(self._on_note_calendar)
        threading.Thread(target=self._calendar_worker,
                         name="calendar-worker", daemon=True).start()
        # the pull half of two-way sync: events made straight in Google
        # Calendar become green-chip notes; moved/deleted events update theirs
        threading.Thread(target=self._calendar_poll_loop,
                         name="calendar-poll", daemon=True).start()
        self._start_cloud_sync()

        self.root.update_idletasks()
        make_non_activating(self.root)
        # real top-level HWND, cached so the keyboard-hook thread can hit-test
        # the pointer against the pill without touching Tk (not thread-safe)
        self._hwnd = (ctypes.windll.user32.GetParent(self.root.winfo_id())
                      or self.root.winfo_id())
        self._paste_t = 0.0

        self._badge = ShotBadge(self)
        self.root.after(400, self._badge.refresh)   # once geometry settles

        if parakeet_files_ready():
            self.state = LOADING
            threading.Thread(target=self._load_model, daemon=True).start()
        else:
            self.state = DOWNLOADING
            threading.Thread(target=self._download_model, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

        self.install_keyboard_hook()

        if not self.settings.get("seen_intro3"):
            self.settings["seen_intro3"] = True
            self.settings["seen_intro2"] = True
            self.settings["seen_intro"] = True
            save_settings(self.settings)
            self.root.after(1500, lambda: self.show_toast(
                "Tap CTRL (on its own) and just talk — words appear as you speak.\n"
                "Tap CTRL again to stop, or hold CTRL down while you speak.\n"
                "Right-click me to pick a different key.", 7000))

        self.tick()
        self.poll_events()
        self.assert_topmost()

    # ---------------- hotkeys ----------------

    def hotkey_label(self):
        parts = []
        for hk in self.settings["hotkeys"]:
            name = hk.get("name") if isinstance(hk, dict) else str(hk)
            name = mod_family(name or "") or name or f"key {hk.get('sc')}"
            if name.upper() not in parts:
                parts.append(name.upper())
        return " or ".join(parts)

    def _refresh_hotkey_codes(self):
        """Split hotkeys into modifier families (gesture keys) and direct keys.

        The hook can't reliably tell left from right modifiers, so a modifier
        hotkey means the whole family: tapped ALONE = start/stop, held ALONE =
        push-to-talk. Combos (Ctrl+C etc.) never trigger. Direct keys (F8, F9,
        captured keys) fire on plain press.
        """
        self._mod_names, self._mod_sc = set(), set()
        self._direct_names, self._direct_sc = set(), set()
        self._suppress_keys = set()   # direct keys whose OS side-effect we block
        for hk in self.settings["hotkeys"]:
            name = hk.get("name") if isinstance(hk, dict) else hk
            sc = hk.get("sc") if isinstance(hk, dict) else None
            fam = MOD_FAMILIES.get(mod_family(name or ""))
            if fam:
                self._mod_names |= fam
                for n in fam:
                    try:
                        self._mod_sc |= set(keyboard.key_to_scan_codes(n))
                    except Exception:
                        pass
                if sc:
                    self._mod_sc.add(sc)
            else:
                if name:
                    self._direct_names.add(name)
                    # A suppressed key runs its handler INLINE on the hook's
                    # listening thread; a normal direct key runs it on the
                    # processing thread. Keep a side-effect key the SOLE direct
                    # hotkey (the capture UI always writes a single-key list) —
                    # pairing it with another direct key via a hand-edited
                    # settings.json would race the shared key_is_down state.
                    if name in SIDE_EFFECT_KEYS:
                        self._suppress_keys.add(name)
                if sc:
                    self._direct_sc.add(sc)
                elif name:
                    try:
                        self._direct_sc |= set(keyboard.key_to_scan_codes(name))
                    except Exception:
                        pass

    def _global_kb(self, e):
        """Single always-on hook: hotkey gestures AND 'press any key' capture.
        Runs on the keyboard library's thread — it must stay fast (Windows
        drops hooks that stall) and never touch Tk directly."""
        try:
            if self._capturing:
                if e.event_type == "down" and (e.name or e.scan_code):
                    self._capturing = False
                    self.events.put(("hotkey_captured",
                                     {"name": e.name or "", "sc": e.scan_code or 0}))
                return
            name, sc, now = e.name, e.scan_code, time.time()
            # Ctrl+V while the mouse is over the pill = paste the clipboard
            # straight in as a note (same as middle-click). The pill never
            # has keyboard focus (WS_EX_NOACTIVATE), so this is the only way
            # a "paste into the pill" gesture can exist.
            if (e.event_type == "down" and name in ("v", "V")
                    and now - self._paste_t > 0.8
                    # VK_CONTROL via Win32 — keyboard.is_pressed misses odd
                    # scan codes (this laptop's ctrl reports several)
                    and ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000
                    and self._pointer_over_pill()):
                self._paste_t = now
                if self._mod_down:
                    self._mod_other = True   # it was a combo — no toggle
                self.save_clipboard_note()
                return
            is_mod = (name in self._mod_names) or (sc in self._mod_sc)
            is_direct = (not is_mod) and ((name in self._direct_names)
                                          or (sc in self._direct_sc))
            if is_mod or is_direct:
                dbg(f"hook {e.event_type} name={name!r} sc={sc}")
            if e.event_type == "down":
                if is_mod:
                    if not self._mod_down:
                        self._mod_down = True
                        self._mod_t = now
                        self._mod_other = False
                        self._mod_ptt = False
                elif is_direct:
                    if not self.key_is_down:    # ignore auto-repeat
                        self.key_is_down = True
                        self.key_press_time = now
                        self.key_started = (self.state == IDLE)
                        self.events.put(("toggle", None))
                elif self._mod_down:
                    self._mod_other = True      # it's a combo like Ctrl+C — stand down
            else:
                if is_mod and self._mod_down:
                    held = now - self._mod_t
                    self._mod_down = False
                    if not self._mod_other:
                        if self._mod_ptt:
                            if held > 0.9:      # push-to-talk finished
                                self.events.put(("stop_if_listening", None))
                        elif held < 0.45:       # clean tap
                            self.events.put(("toggle", None))
                    self._mod_ptt = False
                elif is_direct and self.key_is_down:
                    self.key_is_down = False
                    if self.key_started and now - self.key_press_time > 0.7:
                        self.events.put(("stop_if_listening", None))
        except Exception:
            pass

    def install_keyboard_hook(self):
        try:
            self._refresh_hotkey_codes()
            keyboard.hook(self._global_kb, suppress=False)
            self._sync_suppressed_keys()
            dbg("keyboard.hook installed")
        except Exception as ex:
            dbg(f"keyboard.hook FAILED: {ex!r}")
            self.events.put(("toast", "Couldn't attach the keyboard hook — "
                                      "hotkeys won't work, but clicking will"))

    def _sync_suppressed_keys(self):
        """Attach a dedicated suppressing hook for each talk key that would
        otherwise flip a sticky Windows state (Caps/Num/Scroll Lock, Insert).
        The hook forwards the event to the SAME brain (`_global_kb`, which
        already treats it as an instant direct key) and returns False so the
        OS side-effect is swallowed — the key does nothing but talk.

        If the suppressing hook can't attach, the key still works through the
        normal (unsuppressed) direct path — it just also toggles its state."""
        for rem in self._suppress_removers:
            try:
                rem()
            except Exception:
                pass
        self._suppress_removers = []
        for name in getattr(self, "_suppress_keys", ()):
            try:
                rem = keyboard.hook_key(name, self._suppress_hook, suppress=True)
                self._suppress_removers.append(rem)
                dbg(f"suppressing side-effect of talk key: {name}")
            except Exception as ex:
                dbg(f"couldn't suppress {name}: {ex!r}")

    def _suppress_hook(self, e):
        """Runs INLINE on the hook thread with the keystroke held pending, so
        it must stay fast and never touch Tk (same rules as _global_kb)."""
        try:
            self._global_kb(e)
        except Exception:
            pass
        return False    # block the Caps/Num/Scroll Lock / Insert state flip

    def start_capture(self):
        if self._capturing:
            return
        self._capturing = True
        self.show_toast("NOW PRESS the key you want as your talk button.\n"
                        "(Esc = keep " + self.hotkey_label() + ")", 15000)

        def timeout():
            if self._capturing:
                self._capturing = False
                self.show_toast("No key pressed — hotkey stays "
                                + self.hotkey_label(), 3000)
        self.root.after(15000, timeout)

    # ---------------- menu ----------------

    def _menu_items(self):
        s = self.settings
        items = [
            {"kind": "hero", "text": "Open DictationMic",
             "hint": "notes · shots · full screen",
             "command": self.open_app},
            {"kind": "sep"},
            {"kind": "header", "text": "Output"},
            {"kind": "item", "text": "Type into the box I'm working in",
             "radio": s["mode"] == "type",
             "command": lambda: self.set_mode("type")},
            {"kind": "item", "text": "Copy to the clipboard instead",
             "radio": s["mode"] == "clipboard",
             "command": lambda: self.set_mode("clipboard")},
            {"kind": "sep"},
            {"kind": "header", "text": "Notes"},
            {"kind": "item", "text": "My notes",
             "hint": "in your web browser",
             "command": self.open_notes},
            {"kind": "item", "text": "My files",
             "hint": "PDFs, docs & photos as real files",
             "command": self.open_files_folder},
            {"kind": "item", "text": "Save the clipboard as a note",
             "hint": "middle-click · Ctrl+V over me",
             "command": self.save_clipboard_note},
            {"kind": "item", "text": "Keep a copy of each dictation",
             "check": bool(s.get("save_notes", True)),
             "command": self.toggle_save_notes},
            {"kind": "sep"},
            {"kind": "header", "text": "Calendar"},
        ]
        if self.gcal.connected():
            items += [
                {"kind": "status", "text": "Google Calendar is connected",
                 "bullet": MENU_GREEN, "hint": s.get("gcal_email", "")},
                {"kind": "item",
                 "text": "Add events when I say “add to calendar”",
                 "check": bool(s.get("calendar_enabled", True)),
                 "command": self.toggle_calendar_enabled},
                {"kind": "item", "text": "Disconnect Google Calendar",
                 "command": self.calendar_off},
            ]
        else:
            items += [
                {"kind": "item", "text": "Connect Google Calendar…",
                 "hint": "say “add to calendar” while dictating",
                 "command": self.calendar_dialog},
                {"kind": "item", "text": "Apple / iCloud Calendar",
                 "hint": "coming soon",
                 "command": lambda: self.show_toast(
                     "Apple Calendar is on the way — Google Calendar "
                     "works today", 2800)},
            ]
        items += [
            {"kind": "sep"},
            {"kind": "header", "text": "Screenshots"},
            {"kind": "item",
             "text": f"Pinned shots ({self.shots.count()})",
             "hint": "drag them into anything",
             "command": self.toggle_shots_window},
            {"kind": "item", "text": "Catch screenshots & copied images",
             "check": bool(s.get("catch_shots", True)),
             "command": self.toggle_catch_shots},
            {"kind": "item", "text": "Save caught screenshots to my notes",
             "hint": "they sync like any note",
             "check": bool(s.get("shots_to_notes", True)),
             "command": self.toggle_shots_to_notes},
            {"kind": "item", "text": "Pin images from my phone",
             "hint": "phone photos land like screenshots",
             "check": bool(s.get("phone_shots", True)),
             "command": self.toggle_phone_shots},
            {"kind": "sep"},
            {"kind": "header", "text": "Phone sync"},
        ]
        if s.get("sync_enabled") and self.cloud is not None:
            state = self.cloud.status()["sync"]
            text, color = {
                "ok": ("Phone sync is on — notes flow both ways", MENU_GREEN),
                "offline": ("Phone sync: offline — will catch up", MENU_SUB),
                "needs-signin": ("Phone sync needs a fresh sign-in", MENU_RED),
                "error": ("Phone sync hiccup — retrying", MENU_SUB),
            }.get(state, ("Phone sync is starting…", MENU_SUB))
            items.append({"kind": "status", "text": text, "bullet": color,
                          "hint": s.get("sync_email", "")})
            if state == "needs-signin":
                items.append({"kind": "item", "text": "Sign in again…",
                              "command": self.sync_dialog})
            items += [
                {"kind": "item", "text": "Email me a password-reset link",
                 "hint": "forgot your sync password?",
                 "command": self.send_reset_link},
                {"kind": "item", "text": "Turn off phone sync",
                 "command": self.sync_off},
            ]
        else:
            items.append({"kind": "item", "text": "Set up phone sync…",
                          "hint": "your notes, on your phone",
                          "command": self.sync_dialog})
        items += [
            {"kind": "sep"},
            {"kind": "header", "text": f"Talk key — {self.hotkey_label()}"},
            {"kind": "status",
             "text": "Tap = start / stop · hold + speak = push-to-talk"},
            {"kind": "item", "text": "Change the talk key…",
             "command": self.start_capture},
            {"kind": "sep"},
            {"kind": "item", "text": "Exit DictationMic", "danger": True,
             "command": self.quit},
        ]
        return items

    def set_mode(self, mode):
        self.settings["mode"] = mode
        save_settings(self.settings)
        self.show_toast("Words will be typed where your cursor is"
                        if mode == "type"
                        else "Words will be copied to the clipboard", 2200)

    def toggle_save_notes(self):
        self.settings["save_notes"] = not self.settings.get("save_notes", True)
        save_settings(self.settings)
        self.show_toast("Keeping a copy of every dictation in your notes"
                        if self.settings["save_notes"]
                        else "Not saving dictations to notes any more", 2200)

    def send_reset_link(self):
        email = (self.settings.get("sync_email") or "").strip()
        if not email:
            self.sync_dialog()
            return
        self.show_toast(f"Sending a password-reset link to {email}…", 2500)

        def work():
            from cloudsync import send_password_reset
            ok, msg = send_password_reset(email)
            self.events.put(("toast", msg))
        threading.Thread(target=work, daemon=True).start()

    # ---------------- model download (first run only) ----------------

    def _download_model(self):
        def frac(f):
            self.dl_frac = f
        if download_parakeet(progress=frac,
                             notify=lambda m: self.events.put(("toast", m))):
            self.events.put(("dl_done", None))

    # ---------------- background: model + transcription worker ----------------

    def _load_model(self):
        self.transcriber.load()
        if self.transcriber.model is not None:
            try:    # warm-up so the first real phrase isn't the slow one
                self.transcriber.transcribe(np.zeros(SAMPLE_RATE // 2, np.float32))
            except Exception:
                pass
        self.events.put(("model_loaded", None))

    def _worker(self):
        while True:
            kind, audio = self.work.get()
            if kind == "end":
                self.events.put(("session_done", None))
                continue
            if self.transcriber.model is None:
                continue
            try:
                text = self.transcriber.transcribe(audio, self.context)
            except Exception:
                text = ""
            if not text:
                continue
            self.context += " " + text
            self.session_text.append(text)
            try:
                if self.settings["mode"] == "type":
                    wait_modifiers_up()
                    # delay=0 fire-hoses keystrokes and some apps drop
                    # characters from long phrases; 2ms/key is still instant
                    keyboard.write(text + " ", delay=0.002)
                else:
                    pyperclip.copy(" ".join(self.session_text))
            except Exception:
                pass

    # ---------------- events ----------------

    def poll_events(self):
        # one bad event must never kill the pump — it drives all hotkey actions
        try:
            while True:
                name, payload = self.events.get_nowait()
                try:
                    self._handle_event(name, payload)
                except Exception:
                    pass
        except queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(20, self.poll_events)

    def _handle_event(self, name, payload):
        if name == "model_loaded":
            self.state = IDLE
            if self.transcriber.error:
                dbg(f"model load error: {self.transcriber.error}")
                self.show_toast("The speech model failed to load — "
                                "exit and start me again", 4000)
        elif name == "dl_done":
            self.state = LOADING
            threading.Thread(target=self._load_model, daemon=True).start()
        elif name == "toggle":
            self.toggle()
        elif name == "stop_if_listening":
            if self.state == LISTENING:
                self.stop_listening()
            elif self.state == STARTING:
                self._stop_when_ready = True
        elif name == "mic_ready":
            if self.state != STARTING:
                # stopped/quit while the mic was opening — close it again
                threading.Thread(target=self.recorder.stop, daemon=True).start()
            elif self._stop_when_ready:
                self.state = LISTENING
                self.stop_listening()
            else:
                self.state = LISTENING
        elif name == "mic_failed":
            if self.state == STARTING:
                self.state = IDLE
            self.beep(300, 120)
            self.show_toast(f"Mic error: {payload}", 3000)
        elif name == "flash":
            self.flash_until = time.time() + 1.1
        elif name == "shot":
            self.flash_until = time.time() + 0.9
            self.refresh_shot_badge(fresh=True)
            if self._shots_win is not None:
                self._shots_win.rebuild()
            if not self.settings.get("seen_shots_hint"):
                self.settings["seen_shots_hint"] = True
                save_settings(self.settings)
                self.show_toast(
                    "Screenshot pinned to my shoulder — click the badge,\n"
                    "then drag it into Claude Code or click it to copy", 6000)
        elif name == "shots_changed":
            self.refresh_shot_badge(fresh=True)
            if self._shots_win is not None:
                self._shots_win.rebuild()
        elif name == "toast":
            self.show_toast(payload, 3500)
        elif name == "sync_status":
            if payload.get("sync") == "needs-signin":
                self.show_toast("Phone sync needs a fresh sign-in — "
                                "right-click me → Set up phone sync", 4000)
        elif name == "hotkey_captured":
            if payload.get("name") == "esc":
                self.show_toast("Hotkey unchanged — still " + self.hotkey_label(), 2500)
            else:
                self.settings["hotkeys"] = [payload]
                save_settings(self.settings)
                self._refresh_hotkey_codes()
                self._sync_suppressed_keys()
                self.show_toast("Your talk key is now " + self.hotkey_label()
                                + " — tap it and speak", 3500)
        elif name == "session_done":
            if self.state == FINISHING:
                self.state = IDLE
                if self.settings["mode"] == "clipboard" and self.session_text:
                    full = " ".join(self.session_text)
                    self.show_toast("Copied: " + (full[:70] + "…" if len(full) > 70 else full), 3000)
                elif not self.session_text:
                    self.show_toast("Didn't catch anything", 1800)
                full = " ".join(self.session_text).strip()
                if (full and not self.settings.get("save_notes", True)
                        and self.settings.get("calendar_enabled", True)
                        and self.gcal.connected()
                        and whenparse.has_trigger(full)):
                    # notes are off, but "add to calendar" is an explicit ask:
                    # make the event anyway (no note — nothing to highlight)
                    self._cal_q.put((None, full, 0))
                if full and self.settings.get("save_notes", True):
                    try:
                        save_note(full)
                    except OSError:
                        pass
                    else:
                        if not self.settings.get("seen_notes_hint"):
                            self.settings["seen_notes_hint"] = True
                            save_settings(self.settings)
                            self.show_toast("Kept a copy in your notes — "
                                            "right-click me → My notes", 3500)

    # ---------------- session control ----------------

    def toggle(self):
        if self._menu is not None:
            self._menu.close()        # give the pill back before it animates
        if self.state == DOWNLOADING:
            self.show_toast(f"Fetching the speech model — {int(self.dl_frac * 100)}% "
                            "(one-time download)", 2200)
            return
        if self.state == IDLE:
            if self.transcriber.model is None:
                self.show_toast("Still loading the speech model…", 1800)
                return
            self.session_text = []
            self.context = ""
            self.session_start = time.time()
            # Feedback FIRST: opening the input stream can take a second on
            # some audio drivers, and doing it inline froze the pill with no
            # sign it had heard the hotkey. Beep + go lime instantly, open
            # the mic on a thread, fall back to idle if it fails.
            self.state = STARTING
            self._stop_when_ready = False
            self.beep(880, 60)
            self.show_toast("Listening — talk away", 1200)
            self.draw()
            threading.Thread(target=self._open_mic, daemon=True).start()
        elif self.state == STARTING:
            self._stop_when_ready = True    # tapped off before the mic opened
        elif self.state == LISTENING:
            self.stop_listening()

    def _open_mic(self):
        try:
            self.recorder.start()
        except Exception as ex:
            self.events.put(("mic_failed", str(ex)))
        else:
            self.events.put(("mic_ready", None))

    def stop_listening(self):
        self.state = FINISHING
        self.beep(620, 60)
        threading.Thread(target=self.recorder.stop, daemon=True).start()

    # ---------------- pointer ----------------

    def on_enter(self, e):
        self.hover = True
        self._tooltip_job = self.root.after(650, self.show_tooltip)

    def on_leave(self, e):
        self.hover = False
        if self._tooltip_job:
            self.root.after_cancel(self._tooltip_job)
            self._tooltip_job = None
        self.hide_tooltip()

    def on_press(self, e):
        self.hide_tooltip()
        self.drag_start = (e.x_root, e.y_root,
                           self.root.winfo_x(), self.root.winfo_y())
        self.dragging = False

    def on_motion(self, e):
        if self.drag_start is None:
            return
        dx = e.x_root - self.drag_start[0]
        dy = e.y_root - self.drag_start[1]
        if abs(dx) > 4 or abs(dy) > 4:
            if not self.dragging and self._shots_win is not None:
                self._shots_win.close()     # shelf doesn't chase the pill
            self.dragging = True
        if self.dragging:
            self.root.geometry(f"+{self.drag_start[2] + dx}+{self.drag_start[3] + dy}")
            if self._badge.visible:
                self._badge.place()         # the badge rides the shoulder

    def on_release(self, e):
        if self.dragging:
            self.settings["x"] = self.root.winfo_x()
            self.settings["y"] = self.root.winfo_y()
            save_settings(self.settings)
        elif e.state & 0x0005:      # Shift (0x1) or Ctrl (0x4) held — open the
            if self._mod_down:      # menu; laptop touchpads make right-click hard
                self._mod_other = True   # modifier+click is a combo — no tap-toggle
            self.on_right_click(e)
        else:
            self.toggle()
        self.drag_start = None
        self.dragging = False

    def on_right_click(self, e):
        self.hide_tooltip()
        if self._menu is not None:
            self._menu.close()
        self._badge.hide()            # the card takes the shoulder space

        def closed():
            self._menu = None
            try:                      # the card folds back into the pill
                self.root.deiconify()
                self.root.attributes("-topmost", True)
                make_non_activating(self.root)
                self.root.lift()
            except Exception:
                pass
            self._badge.refresh()

        r = self.root
        anchor = (r.winfo_x(), r.winfo_y(), self.width, self.height)
        self._menu = PopupMenu(self.root, self._menu_items(), on_close=closed)
        # hide the pill BEFORE the card takes focus — withdrawing after
        # would fire the card's FocusOut and close it on arrival
        self.root.withdraw()          # the card IS the pill while it's open
        self._menu.show(e.x_root, e.y_root, anchor=anchor)

    # ---------------- throw things at the pill ----------------
    # Drag files, images or selected text onto the pill and each becomes its
    # own note (images compressed to a data URL body — see dropnotes.py).
    # Middle-click does the same for whatever is on the clipboard.

    def _install_drop_targets(self):
        if TkinterDnD is None:
            dbg("tkdnd unavailable — drag-and-drop disabled")
            return
        try:
            for w in (self.root, self.label):
                w.drop_target_register(DND_FILES, DND_TEXT)
                w.dnd_bind("<<Drop:DND_Files>>", self._on_drop_files)
                w.dnd_bind("<<Drop:DND_Text>>", self._on_drop_text)
                w.dnd_bind("<<DropEnter>>", self._on_drop_enter)
                w.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            dbg("tkdnd drop targets installed")
        except Exception as ex:
            dbg(f"tkdnd register failed: {ex!r}")

    def _on_drop_enter(self, event):
        self.drop_hover = True              # lime ring + tray: "I'll catch that"
        return event.action

    def _on_drop_leave(self, event):
        self.drop_hover = False
        return event.action

    def _on_drop_files(self, event):
        self.drop_hover = False
        try:
            paths = list(self.root.tk.splitlist(event.data or ""))
        except Exception:
            paths = []
        if paths:
            threading.Thread(target=self._ingest_paths, args=(paths,),
                             daemon=True).start()
        return getattr(event, "action", "copy")

    def _on_drop_text(self, event):
        self.drop_hover = False
        text = event.data or ""
        if text.strip():
            threading.Thread(target=self._ingest_text, args=(text,),
                             daemon=True).start()
        return getattr(event, "action", "copy")

    def _saved_toast(self, title, real_file=False):
        suffix = " — syncing to your phone" if (
            self.settings.get("sync_enabled")
            and self.settings.get("sync_refresh_token")) else ""
        if real_file:
            suffix = " (file kept — see My files)" + suffix
        preview = title[:44] + ("…" if len(title) > 44 else "")
        self.events.put(("flash", None))   # blue ring: the pill caught it
        self.events.put(("toast", f"Saved to notes: {preview}{suffix}"))

    def _ingest_paths(self, paths):
        import dropnotes
        saved_title, saved, real = "", 0, False
        pinned = False
        for p in paths[:10]:
            # images thrown at the pill also land on the shelf, ready to
            # drag back out — independent of whether the note saves
            if (os.path.splitext(str(p))[1].lower() in shots.IMAGE_EXTS
                    and self.shots.pin_file(str(p))):
                pinned = True
        if pinned:
            self.events.put(("shots_changed", None))
        for p in paths[:10]:
            try:
                title, body = dropnotes.note_from_path(p)
                get_store().create(title, body, src_path=p)
                saved += 1
                saved_title = title
                real = real or body.startswith("data:")
            except ValueError as ex:
                self.events.put(("toast", str(ex)))
            except Exception:
                self.events.put(("toast", "Couldn't save "
                                          + os.path.basename(str(p))))
        if saved == 1:
            self._saved_toast(saved_title, real_file=real)
        elif saved:
            self._saved_toast(f"{saved} files", real_file=real)

    def _ingest_text(self, text):
        import dropnotes
        try:
            title, body = dropnotes.note_from_dropped_text(text)
            get_store().create(title, body)
            self._saved_toast(title)
        except ValueError as ex:
            self.events.put(("toast", str(ex)))
        except Exception:
            self.events.put(("toast", "Couldn't save that"))

    def _pointer_over_pill(self):
        """Win32-only hit test (safe from the keyboard-hook thread)."""
        try:
            if not ctypes.windll.user32.IsWindowVisible(self._hwnd):
                return False          # hidden while the card is open
            pt = ctypes.wintypes.POINT()
            rect = ctypes.wintypes.RECT()
            if not (ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                    and ctypes.windll.user32.GetWindowRect(
                        self._hwnd, ctypes.byref(rect))):
                return False
            return (rect.left <= pt.x < rect.right
                    and rect.top <= pt.y < rect.bottom)
        except Exception:
            return False

    def save_clipboard_note(self):
        """Whatever is on the clipboard -> middle-click the pill (or the
        menu) and it lands in notes: a screenshot (Win+Shift+S), files
        copied in Explorer, or plain text — synced like a dictation."""
        def work():
            import dropnotes
            clip = None
            try:
                from PIL import ImageGrab
                clip = ImageGrab.grabclipboard()
            except Exception:
                clip = None
            try:
                if isinstance(clip, Image.Image):
                    body = dropnotes.compress_image(clip)
                    title = dropnotes.photo_title("Clipboard image")
                    get_store().create(title, body)
                    if self.shots.pin_image(clip):   # shelf too (dedupes if
                        self.events.put(("shots_changed", None))  # caught)
                    self._saved_toast(title)
                    return
                if isinstance(clip, list) and clip:
                    self._ingest_paths([p for p in clip if isinstance(p, str)])
                    return
            except ValueError as ex:
                self.events.put(("toast", str(ex)))
                return
            except Exception as ex:
                self.events.put(("toast", f"Couldn't save that image — {ex}"))
                return
            try:
                text = (pyperclip.paste() or "").strip()
            except Exception:
                text = ""
            if not text:
                self.events.put(("toast", "Nothing on the clipboard to save"))
                return
            try:
                title, body = dropnotes.note_from_dropped_text(text, fetch=False)
                get_store().create(title, body)
                trimmed = " ".join(text.split())
                self._saved_toast(trimmed)
            except Exception as ex:
                self.events.put(("toast", f"Couldn't save that — {ex}"))
        threading.Thread(target=work, daemon=True).start()

    # ---------------- screenshot shelf ----------------

    def _grab_clip(self):
        """The clipboard changed while 'catch screenshots' is on: if it's a
        bitmap (Win+Shift+S, PrtScn, any copied image) pin it to the shelf.
        Copied *files* are ignored here — throwing files at the pill stays
        a deliberate act (drop or middle-click)."""
        try:
            from PIL import ImageGrab
            clip = ImageGrab.grabclipboard()
            if isinstance(clip, Image.Image):
                path = self.shots.pin_image(clip)
                if path:
                    self.events.put(("shot", path))
                    # ... and into the main app: an image note, synced like
                    # any other (pin_image already deduped the double-fire)
                    if self.settings.get("shots_to_notes", True):
                        try:
                            import dropnotes
                            get_store().create(
                                dropnotes.photo_title("Screenshot"),
                                dropnotes.compress_image(clip))
                        except Exception as ex:
                            dbg(f"shot->note failed: {ex!r}")
        except Exception as ex:
            dbg(f"clip catch failed: {ex!r}")
        finally:
            self._clip_busy = False

    def note_own_clipboard(self):
        """We just wrote the clipboard ourselves (copy / drag bookkeeping) —
        don't let the watcher catch our own copy as a new screenshot."""
        self._clip_seq = shots.clip_seq()

    def refresh_shot_badge(self, fresh=None):
        if fresh is not None:
            self.shots_fresh = fresh
        self._badge.refresh()

    def toggle_shots_window(self):
        if self._shots_win is not None:
            self._shots_win.close()
            return
        self.refresh_shot_badge(fresh=False)     # looked at — lime settles
        self._shots_win = ShotsWindow(
            self, on_close=lambda: setattr(self, "_shots_win", None))
        self._shots_win.show()

    def toggle_catch_shots(self):
        on = not self.settings.get("catch_shots", True)
        self.settings["catch_shots"] = on
        save_settings(self.settings)
        if on:
            self._clip_seq = shots.clip_seq()    # don't swallow an old copy
            self.show_toast("Catching screenshots — Win+Shift+S pins "
                            "to my shoulder", 2600)
        else:
            self.show_toast("Not catching screenshots", 1800)

    def toggle_shots_to_notes(self):
        on = not self.settings.get("shots_to_notes", True)
        self.settings["shots_to_notes"] = on
        save_settings(self.settings)
        self.show_toast("Caught screenshots also land in your notes"
                        if on else
                        "Screenshots stay on the shelf only", 2400)

    def toggle_phone_shots(self):
        on = not self.settings.get("phone_shots", True)
        self.settings["phone_shots"] = on
        save_settings(self.settings)
        self.show_toast("Images saved on your phone now pin to my shoulder"
                        if on else
                        "Phone images go to notes only", 2400)

    # 24h: a first-sync reconcile replays the whole cloud library as remote
    # creates — only a genuinely fresh capture belongs on the shelf
    PHONE_SHOT_FRESH_MS = 24 * 3600 * 1000

    def _on_remote_note(self, kind, nid):
        """Store listener (sync worker thread): an image note that just
        arrived from the phone joins the shelf, exactly like a local
        screenshot — the reverse of shots_to_notes. Disk work only here;
        the flash + badge happen on the Tk thread via the "shot" event."""
        if kind != "remote_create" or not self.settings.get("phone_shots", True):
            return
        try:
            store = get_store()
            e = store.entry(nid)
            if not e or not e.get("file"):
                return                       # text/voice note — nothing to pin
            if (int(time.time() * 1000) - int(e.get("createdAt") or 0)
                    > self.PHONE_SHOT_FRESH_MS):
                return
            # pin_file accepts image extensions only — PDFs etc. fall through
            path = self.shots.pin_file(
                os.path.join(store.files_dir(), e["file"]))
            if path:
                self.events.put(("shot", path))
        except Exception as ex:
            dbg(f"phone shot pin failed: {ex!r}")

    # ---------------- "add to calendar" ----------------

    def _on_note_calendar(self, kind, nid):
        """Store listener (any thread): a text note that says "add to
        calendar" and isn't linked to an event yet gets queued for the
        calendar worker. Covers dictations, typed notes, and phone notes
        (voice notes arrive here after this laptop transcribes them)."""
        if kind not in ("create", "update", "remote_create", "remote_update"):
            return
        if not (self.settings.get("calendar_enabled", True)
                and self.gcal.connected()):
            return
        try:
            store = get_store()
            e = store.entry(nid)
            if not e or e.get("file") or e.get("deletedLocally"):
                return                     # image/file note — never a trigger
            cal = e.get("calendar")
            if cal and not (cal.get("status") == "failed"
                            and cal.get("bodyHash") != e.get("hash")):
                return                     # already linked (or failed as-is)
            note = store.get(nid)
            if (not note or note["body"].startswith("data:")
                    or not whenparse.has_trigger(note["body"])):
                return
            if nid in self._cal_inflight:
                return
            self._cal_inflight.add(nid)
            self._cal_q.put(nid)
        except Exception as ex:
            dbg(f"calendar detect failed: {ex!r}")

    @staticmethod
    def _fmt_event_time(start_ms, all_day):
        lt = time.localtime(start_ms / 1000)
        day = time.strftime("%a", lt) + f" {lt.tm_mday} " + time.strftime("%b", lt)
        today = time.localtime()
        if (lt.tm_year, lt.tm_yday) == (today.tm_year, today.tm_yday):
            day = "today"
        elif (lt.tm_year, lt.tm_yday) == (today.tm_year, today.tm_yday + 1):
            day = "tomorrow"
        if all_day:
            return f"{day}, all day"
        return f"{day} at " + time.strftime("%H:%M", lt)

    def _calendar_sweep(self):
        """Boot catch-up: recent notes that say "add to calendar" but never
        got their event (they arrived while the app was off/restarting, or
        before a trigger fix) get queued once. Capped at 48h old so ancient
        notes can never surprise-create events with today's date."""
        try:
            if not (self.settings.get("calendar_enabled", True)
                    and self.gcal.connected()):
                return
            store = get_store()
            cutoff = (time.time() - 48 * 3600) * 1000
            for n in store.all_notes():
                if (n.get("calendar") or int(n.get("createdAt") or 0) < cutoff
                        or n["body"].startswith("data:")
                        or not whenparse.has_trigger(n["body"])):
                    continue
                e = store.entry(n["id"])
                if not e or e.get("file") or n["id"] in self._cal_inflight:
                    continue
                self._cal_inflight.add(n["id"])
                self._cal_q.put(n["id"])
        except Exception as ex:
            dbg(f"calendar sweep failed: {ex!r}")

    def _calendar_worker(self):
        """One item at a time: parse the when, make the Google event, stamp
        the note. Never touches Tk directly — toasts go via the event queue."""
        time.sleep(8)                    # let sync/gcal settle after boot
        self._calendar_sweep()
        while True:
            item = self._cal_q.get()
            nid, text, attempts = (item if isinstance(item, tuple)
                                   else (item, None, 0))
            retrying = False
            try:
                # let a typed note settle (the editors save on a 700 ms
                # debounce — don't calendar half a sentence)
                time.sleep(2.5)
                store = get_store()
                e = None
                if nid is not None:
                    note = store.get(nid)
                    e = store.entry(nid)
                    if not note or e is None:
                        continue
                    text = note["body"]
                    cal = e.get("calendar")
                    if cal and not (cal.get("status") == "failed"
                                    and cal.get("bodyHash") != e.get("hash")):
                        continue
                if not text or not whenparse.has_trigger(text):
                    continue
                when = whenparse.parse_when(text)
                summary = note_title_from(whenparse.strip_trigger(text))
                start_ms = int(when["start"].timestamp() * 1000)
                end_ms = int(when["end"].timestamp() * 1000)
                try:
                    ev = self.gcal.create_event(
                        summary, when["start"], when["end"], when["all_day"],
                        description=text + "\n\n— dictated with DictationMic")
                except RuntimeError as ex:
                    # Google said no (auth gone, quota, bad request) — mark it
                    # so the note shows amber; a body edit re-arms it
                    if nid is not None:
                        store.set_calendar(nid, {
                            "status": "failed", "provider": "google",
                            "error": str(ex)[:140],
                            "addedAt": int(time.time() * 1000),
                            "bodyHash": e.get("hash") if e else None})
                    self.events.put(("toast", str(ex)))
                    continue
                except Exception as ex:
                    # network blip — retry a few times, then mark failed
                    dbg(f"calendar create retry: {ex!r}")
                    if attempts < 5:
                        retrying = True   # inflight stays set — no re-detects
                        def requeue(item=(nid, text, attempts + 1)):
                            time.sleep(20)
                            self._cal_q.put(item)
                        threading.Thread(target=requeue, daemon=True).start()
                        continue
                    if nid is not None:
                        store.set_calendar(nid, {
                            "status": "failed", "provider": "google",
                            "error": "offline",
                            "addedAt": int(time.time() * 1000),
                            "bodyHash": e.get("hash") if e else None})
                    self.events.put(("toast", "Couldn't reach Google Calendar "
                                     "— that note wasn't scheduled"))
                    continue
                if nid is not None:
                    store.set_calendar(nid, {
                        "status": "ok", "provider": "google",
                        "eventId": ev["eventId"], "link": ev["link"],
                        "start": start_ms, "end": end_ms,
                        "allDay": bool(when["all_day"]),
                        "addedAt": int(time.time() * 1000),
                        "bodyHash": e.get("hash") if e else None})
                self.events.put(("toast", "Added to your calendar — "
                                 + self._fmt_event_time(start_ms,
                                                        when["all_day"])))
            except Exception as ex:
                dbg(f"calendar worker: {ex!r}")
            finally:
                if not retrying:
                    self._cal_inflight.discard(nid)

    GCAL_POLL_S = 180                  # pull from Google Calendar every 3 min

    def _calendar_poll_loop(self):
        time.sleep(20)                 # let sync land the first snapshot
        while True:
            try:
                self._calendar_poll()
            except Exception as ex:
                dbg(f"calendar poll: {ex!r}")
            time.sleep(self.GCAL_POLL_S)

    def _calendar_poll(self):
        """Pull changes from Google Calendar. New events (made on the phone,
        the web, anywhere) become notes with the green chip; events that were
        moved update their note's chip; deleted events mark it. Never touches
        events — this is read-only towards Google."""
        if not (self.settings.get("calendar_enabled", True)
                and self.gcal.connected()):
            return
        from datetime import datetime, timedelta, timezone
        last = self.settings.get("gcal_last_poll") or (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        poll_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        items = self.gcal.list_updated(last)
        store = get_store()
        known = {}                     # eventId -> note id
        with store.lock:
            for nid, e in store.notes.items():
                cal = e.get("calendar") or {}
                if cal.get("eventId"):
                    known[cal["eventId"]] = nid

        def parse_side(raw, fallback=None):
            if not raw:
                return fallback, False
            if "date" in raw:          # all-day (end date is exclusive)
                return datetime.fromisoformat(raw["date"]).astimezone(), True
            try:
                return datetime.fromisoformat(raw.get("dateTime")), False
            except (TypeError, ValueError):
                return fallback, False

        for ev in items:
            eid = ev.get("id")
            if not eid:
                continue
            sdt, all_day = parse_side(ev.get("start"))
            edt, _ = parse_side(ev.get("end"), fallback=sdt)
            nid = known.get(eid)
            if nid is not None:
                # one of ours (or already imported): follow moves/deletes
                cal = (store.entry(nid) or {}).get("calendar") or {}
                new = dict(cal)
                if ev.get("status") == "cancelled":
                    new["status"] = "cancelled"
                elif sdt is not None:
                    new.update(status="ok",
                               start=int(sdt.timestamp() * 1000),
                               end=int((edt or sdt).timestamp() * 1000),
                               allDay=all_day,
                               link=ev.get("htmlLink") or cal.get("link", ""))
                if new != cal:
                    new["addedAt"] = int(time.time() * 1000)
                    store.set_calendar(nid, new)
                    dbg(f"calendar poll: updated note {nid[:8]} from event {eid[:10]}")
                continue
            # a brand-new event made directly in Google Calendar -> a note
            if ev.get("status") != "confirmed" or sdt is None:
                continue
            if ev.get("recurringEventId"):
                continue               # instances ride their series' note
            if "dictated with DictationMic" in (ev.get("description") or ""):
                continue               # our own notes-off dictation event
            if sdt.timestamp() < time.time() - 3600:
                continue               # already past — not worth a note
            summary = (ev.get("summary") or "Event").strip()
            start_ms = int(sdt.timestamp() * 1000)
            when_line = self._fmt_event_time(start_ms, all_day)
            note = store.create(
                summary, f"{summary}\n{when_line}\n\nAdded in Google Calendar")
            store.set_calendar(note["id"], {
                "status": "ok", "provider": "google", "eventId": eid,
                "link": ev.get("htmlLink") or "",
                "start": start_ms,
                "end": int((edt or sdt).timestamp() * 1000),
                "allDay": all_day, "addedAt": int(time.time() * 1000),
                "source": "gcal"})
            self.events.put(("toast", "From your calendar: " + summary))
            dbg(f"calendar poll: imported event {eid[:10]} as note")
        self.settings["gcal_last_poll"] = poll_started
        save_settings(self.settings)

    def _check_upcoming_events(self):
        """Every ~30 s from tick(): one pill heads-up, 15 minutes before a
        timed event made through "add to calendar"."""
        try:
            for nid, title, start in get_store().upcoming_calendar(
                    15 * 60 * 1000):
                get_store().set_calendar_notified(nid)
                self.show_toast("Coming up " + self._fmt_event_time(start, False)
                                + " — " + title, 9000)
                self.beep(880, 160)
                break                        # one at a time; next tick, next event
        except Exception as ex:
            dbg(f"calendar heads-up failed: {ex!r}")

    def toggle_calendar_enabled(self):
        on = not self.settings.get("calendar_enabled", True)
        self.settings["calendar_enabled"] = on
        save_settings(self.settings)
        self.show_toast("Say “add to calendar” in a dictation and "
                        "I'll make the event" if on
                        else "Not making calendar events any more", 2600)

    def calendar_off(self):
        def work():
            self.gcal.disconnect()
            self.events.put(("toast", "Google Calendar is disconnected"))
        threading.Thread(target=work, daemon=True).start()

    def calendar_dialog(self):
        win = tk.Toplevel(self.root, bg="#131512")
        win.title("DictationMic — Google Calendar")
        win.resizable(False, False)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"420x330+{(sw - 420) // 2}+{max(0, (sh - 330) // 3)}")
        try:
            win.iconbitmap(os.path.join(APP_DIR, "icon.ico"))
        except Exception:
            pass
        make_titlebar_dark(win)

        FG, SUB, LIME, FIELD = "#eceee7", "#8a919c", "#b6ee3f", "#1a1d18"
        tk.Label(win, text="Connect your Google Calendar",
                 bg="#131512", fg=FG, font=("Segoe UI Semibold", 12)
                 ).pack(pady=(16, 2))
        tk.Label(win, text="Say “add to calendar” in any dictation and the\n"
                           "event lands in your calendar, with the note kept.",
                 bg="#131512", fg=SUB, font=("Segoe UI", 9)).pack()

        id_var = tk.StringVar(value=self.settings.get("gcal_client_id") or "")
        sec_var = tk.StringVar(value=self.settings.get("gcal_client_secret") or "")
        for label, var, show in (("OAuth Client ID", id_var, None),
                                 ("Client secret", sec_var, "•")):
            tk.Label(win, text=label, bg="#131512", fg=SUB,
                     font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=36)
            tk.Entry(win, textvariable=var, show=show or "",
                     bg=FIELD, fg=FG, insertbackground=LIME,
                     relief="flat", font=("Segoe UI", 10)
                     ).pack(fill="x", padx=36, ipady=5, pady=(0, 6))

        status = tk.Label(win, text="", bg="#131512", fg=SUB,
                          font=("Segoe UI", 9), wraplength=360)
        status.pack()

        def guide(_e=None):
            import webbrowser
            webbrowser.open("https://console.cloud.google.com/apis/credentials")

        link = tk.Label(win, text="One-time setup: README · opens the "
                                  "Google Cloud console",
                        bg="#131512", fg=LIME, cursor="hand2",
                        font=("Segoe UI", 9, "underline"))
        link.pack(pady=(2, 0))
        link.bind("<ButtonRelease-1>", guide)

        def connect():
            cid, sec = id_var.get().strip(), sec_var.get().strip()
            if not cid or not sec:
                status.configure(text="Fill in both boxes", fg="#ff5c48")
                return
            btn.configure(state="disabled", text="Waiting for your browser…")
            status.configure(text="Approve DictationMic in the browser tab "
                                  "that just opened", fg=SUB)

            def work():
                ok, msg = self.gcal.connect(cid, sec)
                def done():
                    try:
                        if ok:
                            win.destroy()
                            who = self.gcal.email()
                            self.show_toast(
                                "Google Calendar connected"
                                + (f" as {who}" if who else "")
                                + " — say “add to calendar” while "
                                  "dictating", 4500)
                        else:
                            btn.configure(state="normal", text="Connect")
                            status.configure(text=msg, fg="#ff5c48")
                    except tk.TclError:
                        pass              # dialog closed meanwhile
                self.root.after(0, done)
            threading.Thread(target=work, daemon=True).start()

        btn = tk.Button(win, text="Connect", command=connect,
                        bg=LIME, fg="#0b0c0a", activebackground="#c9f56a",
                        relief="flat", font=("Segoe UI Semibold", 10),
                        cursor="hand2")
        btn.pack(pady=8, ipadx=26, ipady=3)
        win.bind("<Return>", lambda e: connect())
        win.bind("<Escape>", lambda e: win.destroy())
        win.lift()
        win.focus_force()

    def _sync_status(self):
        cloud = getattr(self, "cloud", None)
        if cloud is None:
            return {"sync": "off", "lastSync": 0}
        return cloud.status()

    def open_files_folder(self):
        """Explorer on notes\\files — the real PDFs, docs and photos behind
        file/image notes, ready to copy-paste anywhere."""
        d = get_store().files_dir()
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def _notes_url(self):
        # With phone sync on, the hosted app is the one source of truth for
        # every device — use it instead of the localhost viewer.
        if (self.settings.get("sync_enabled")
                and self.settings.get("sync_refresh_token")):
            return "https://dictationmic-sync.web.app/"
        if self.local_server is None:
            from localserver import LocalServer
            self.local_server = LocalServer(get_store(),
                                            status_fn=self._sync_status, dbg=dbg)
        if not self.local_server.start():
            return None
        return self.local_server.url()

    def open_notes(self):
        import webbrowser
        url = self._notes_url()
        if url is None:
            self.show_toast("Couldn't open your notes — try again", 3000)
            return
        webbrowser.open(url)

    @staticmethod
    def _edge_path():
        """msedge.exe, or None. Edge app-mode is how the full app gets a
        chromeless native window with zero new dependencies."""
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                    r"\App Paths\msedge.exe") as k:
                p = winreg.QueryValue(k, None)
            if p and os.path.isfile(p):
                return p
        except OSError:
            pass
        for base in (os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("ProgramFiles")):
            if base:
                p = os.path.join(base, "Microsoft", "Edge",
                                 "Application", "msedge.exe")
                if os.path.isfile(p):
                    return p
        return None

    def open_app(self):
        """The full-screen DictationMic app: the notes UI (text, image and
        file entries) in its own maximized app window — the hosted app when
        phone sync is on, the token-gated localhost viewer otherwise."""
        url = self._notes_url()
        if url is None:
            self.show_toast("Couldn't open the app — try again", 3000)
            return
        edge = self._edge_path()
        if edge:
            try:
                subprocess.Popen([edge, "--app=" + url, "--start-maximized"],
                                 close_fds=True)
                return
            except Exception as ex:
                dbg(f"edge app launch failed: {ex!r}")
        import webbrowser
        webbrowser.open(url)

    # ---------------- phone sync ----------------

    def _voice_stt(self, audio_bytes):
        """Turn a phone voice note (webm/mp4 bytes) into text with the same
        Parakeet model that does live dictation. None = busy/not ready,
        cloudsync retries in a few seconds."""
        if self.recorder.stream is not None:
            return None                    # live dictation owns the CPU
        if self.transcriber.model is None:
            return None                    # the model is still on its way
        from io import BytesIO
        # faster-whisper stays installed for exactly one thing: decode_audio,
        # a thin PyAV wrapper that turns any phone container into 16k PCM
        from faster_whisper.audio import decode_audio
        audio = decode_audio(BytesIO(audio_bytes), sampling_rate=SAMPLE_RATE)
        if len(audio) < SAMPLE_RATE * 0.3:
            return ""
        # quiet phone recordings (soft speaker, phone at arm's length) get
        # normalised up before the model hears them — same cure as the live
        # mic's software boost. Judged on the 95th percentile, not the max,
        # so one handling thump can't veto the boost for the whole note.
        body = float(np.percentile(np.abs(audio), 95))
        if 0 < body < 0.30:
            audio = np.clip(audio * min(MIC_MAX_BOOST, 0.30 / body), -1.0, 1.0)
        return self.transcriber.transcribe(audio, long=True) or ""

    def _start_cloud_sync(self):
        if not (self.settings.get("sync_enabled")
                and self.settings.get("sync_refresh_token")):
            return
        try:
            from cloudsync import CloudSync
            self.cloud = CloudSync(get_store(), self.settings, save_settings,
                                   self.events, dbg=dbg,
                                   voice_stt=self._voice_stt)
            self.cloud.start()
        except Exception as ex:
            dbg(f"cloud sync failed to start: {ex}")
            self.cloud = None

    def sync_off(self):
        if self.cloud is not None:
            try:
                self.cloud.disable()
            except Exception:
                pass
            self.cloud = None
        else:
            self.settings["sync_enabled"] = False
            save_settings(self.settings)
        self.show_toast("Phone sync is off — notes stay on this computer", 3000)

    def sync_dialog(self):
        try:
            from cloudsync import CloudSync
        except Exception:
            self.show_toast("Sync isn't available in this build", 3000)
            return

        win = tk.Toplevel(self.root, bg="#131512")
        win.title("DictationMic — phone sync")
        win.resizable(False, False)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"360x286+{(sw - 360) // 2}+{max(0, (sh - 286) // 3)}")
        try:
            win.iconbitmap(os.path.join(APP_DIR, "icon.ico"))
        except Exception:
            pass
        make_titlebar_dark(win)

        FG, SUB, LIME, FIELD = "#eceee7", "#8a919c", "#b6ee3f", "#1a1d18"
        tk.Label(win, text="Sync notes with your phone",
                 bg="#131512", fg=FG, font=("Segoe UI Semibold", 12)
                 ).pack(pady=(18, 2))
        tk.Label(win, text="Your own account — the same one you'll use on "
                           "your phone.\nFirst time? Signing in creates it.",
                 bg="#131512", fg=SUB, font=("Segoe UI", 9)).pack()

        email_var = tk.StringVar(value=self.settings.get("sync_email") or "")
        pw_var = tk.StringVar()
        for label, var, show in (("Email", email_var, None),
                                 ("Password", pw_var, "•")):
            tk.Label(win, text=label, bg="#131512", fg=SUB,
                     font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=36)
            tk.Entry(win, textvariable=var, show=show or "",
                     bg=FIELD, fg=FG, insertbackground=LIME,
                     relief="flat", font=("Segoe UI", 10)
                     ).pack(fill="x", padx=36, ipady=5, pady=(0, 6))

        status = tk.Label(win, text="", bg="#131512", fg=SUB,
                          font=("Segoe UI", 9), wraplength=300)
        status.pack()

        def forgot(_e=None):
            email = email_var.get().strip()
            if not email:
                status.configure(text="Type your email above first",
                                 fg="#ff5c48")
                return
            status.configure(text="Sending the reset link…", fg=SUB)

            def work():
                from cloudsync import send_password_reset
                ok, msg = send_password_reset(email)

                def done():
                    try:
                        status.configure(text=msg,
                                         fg=LIME if ok else "#ff5c48")
                    except tk.TclError:
                        pass          # dialog was closed meanwhile
                self.root.after(0, done)
            threading.Thread(target=work, daemon=True).start()

        link = tk.Label(win, text="Forgot password?  Email me a reset link",
                        bg="#131512", fg=LIME, cursor="hand2",
                        font=("Segoe UI", 9, "underline"))
        link.pack(pady=(2, 0))
        link.bind("<ButtonRelease-1>", forgot)

        def connect():
            email = email_var.get().strip()
            pw = pw_var.get()
            if not email or not pw:
                status.configure(text="Fill in both boxes", fg="#ff5c48")
                return
            btn.configure(state="disabled", text="Connecting…")
            status.configure(text="", fg=SUB)

            def work():
                cs = CloudSync(get_store(), self.settings, save_settings,
                               self.events, dbg=dbg)
                ok, msg = cs.setup(email, pw)
                def done():
                    if ok:
                        self.cloud = cs
                        cs.start()
                        win.destroy()
                        self.show_toast(
                            "Phone sync is on — open the same link on your "
                            "phone and sign in", 4500)
                    else:
                        btn.configure(state="normal", text="Connect")
                        status.configure(text=msg, fg="#ff5c48")
                self.root.after(0, done)
            threading.Thread(target=work, daemon=True).start()

        btn = tk.Button(win, text="Connect", command=connect,
                        bg=LIME, fg="#0b0c0a", activebackground="#c9f56a",
                        relief="flat", font=("Segoe UI Semibold", 10),
                        cursor="hand2")
        btn.pack(pady=8, ipadx=26, ipady=3)
        win.bind("<Return>", lambda e: connect())
        win.bind("<Escape>", lambda e: win.destroy())
        win.lift()
        win.focus_force()

    def quit(self):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.settings["x"] = self.root.winfo_x()
        self.settings["y"] = self.root.winfo_y()
        save_settings(self.settings)
        self.root.destroy()

    def assert_topmost(self):
        try:
            self.root.attributes("-topmost", True)
            if self._badge.visible:
                self._badge.win.attributes("-topmost", True)
                self._badge.win.lift()
        except Exception:
            pass
        self.root.after(2000, self.assert_topmost)

    # ---------------- feedback ----------------

    def beep(self, freq, ms):
        if self.settings.get("beeps"):
            threading.Thread(target=winsound.Beep, args=(freq, ms),
                             daemon=True).start()

    def _popup(self, text, font=None):
        t = tk.Toplevel(self.root, bg=MENU_BG)
        t.overrideredirect(True)
        try:
            t.title("DM|" + text.split("\n")[0][:80])
        except Exception:
            pass
        t.attributes("-topmost", True)
        t.configure(highlightthickness=1, highlightbackground=MENU_EDGE,
                    highlightcolor=MENU_EDGE)
        tk.Label(t, text=text, bg=MENU_BG, fg=MENU_FG,
                 font=font or (UI_FAMILY, 10),
                 padx=14, pady=8, justify="left").pack()
        t.update_idletasks()
        x = self.root.winfo_x() + self.width // 2 - t.winfo_width() // 2
        y = self.root.winfo_y() - t.winfo_height() - 10
        if y < 0:
            y = self.root.winfo_y() + self.height + 10
        x = max(0, min(x, self.root.winfo_screenwidth() - t.winfo_width()))
        t.geometry(f"+{x}+{y}")
        round_corners(t)
        fade_in(t)
        make_non_activating(t)
        return t

    def show_toast(self, text, ms=2400):
        if self.toast is not None:
            try:
                self.toast.destroy()
            except Exception:
                pass
        self.toast = self._popup(text)
        ref = self.toast

        def _expire():
            try:
                ref.destroy()
            except Exception:
                pass
            if self.toast is ref:
                self.toast = None
        ref.after(ms, _expire)

    def show_tooltip(self):
        if self.tooltip is not None or self.state not in (IDLE, LOADING, DOWNLOADING):
            return
        if self._menu is not None or self._shots_win is not None:
            return                 # never float help over an open card
        self.tooltip = self._popup(
            f"Click or tap {self.hotkey_label()} — start / stop\n"
            "Hold the key — push-to-talk\n"
            "Hold + drag — move me\n"
            "Drop files, text or images on me — synced as notes\n"
            "Ctrl+V over me (or middle-click) — save the clipboard\n"
            "Screenshots pin to my shoulder — click the badge\n"
            "Right-click or Shift+click — menu & the full app",
            (UI_FAMILY, 9))

    def hide_tooltip(self):
        if self._tooltip_job:      # a pending hover timer would re-show it
            self.root.after_cancel(self._tooltip_job)
            self._tooltip_job = None
        if self.tooltip is not None:
            try:
                self.tooltip.destroy()
            except Exception:
                pass
            self.tooltip = None

    # ---------------- animation ----------------

    def tick(self):
        try:
            self.phase += 0.16
            # lone-held modifier (e.g. Ctrl) => start push-to-talk
            if (self._mod_down and not self._mod_other and not self._mod_ptt
                    and self.state == IDLE
                    and time.time() - self._mod_t > 0.45):
                self._mod_ptt = True
                self.toggle()
            if self.state == LISTENING:
                self.level_hist.append(self.recorder.level)
                limit = self.settings.get("auto_stop_seconds") or 0
                if limit > 0:
                    quiet = time.time() - max(self.recorder.last_voice_time,
                                              self.session_start)
                    if quiet > limit:
                        self.stop_listening()
                        self.show_toast("Stopped listening (it went quiet)", 2200)
            else:
                self.level_hist.append(0.0)
            # screenshot watcher: a cheap sequence-number poll twice a second
            # (no clipboard open, no message pump) — grabbing the actual
            # bitmap happens off-thread only when the number moves
            now = time.time()
            # calendar heads-up: a cheap index scan twice a minute
            if now - self._cal_notify_last >= 30.0:
                self._cal_notify_last = now
                self._check_upcoming_events()
            if (now - self._clip_last_check >= 0.5 and not self._clip_busy
                    and self.settings.get("catch_shots", True)):
                self._clip_last_check = now
                seq = shots.clip_seq()
                if seq != self._clip_seq:
                    self._clip_seq = seq
                    self._clip_busy = True
                    threading.Thread(target=self._grab_clip,
                                     daemon=True).start()
            self.draw()
        except Exception:
            pass
        # 30fps only while something on the pill is actually moving (or a
        # held modifier needs tight push-to-talk timing); a sleeping pill
        # ticks at 10Hz and draw() below is a no-op repaint-wise
        animating = (self.state in (LISTENING, STARTING, FINISHING,
                                    DOWNLOADING)
                     or self.drop_hover or self._mod_down
                     or time.time() < self.flash_until)
        self.root.after(33 if animating else 100, self.tick)

    def _set_frame(self, key, maker):
        """Repaint only when the frame identity changed — a sleeping pill
        must not touch Tk at all. key=None means 'always animating'."""
        if key is not None and key == self._frame_key:
            return
        self._frame_key = key
        self._photo = maker()
        self.label.configure(image=self._photo)

    def draw(self):
        r = self.renderer
        if self.drop_hover:                 # a drag is over us — outrank all
            self._set_frame(("drop",), r.drop)
            return
        if time.time() < self.flash_until:  # just caught a drop/paste
            self._set_frame(("flash",), r.flash)
            return
        if self.state in (LISTENING, STARTING):
            hist = list(self.level_hist)[-r.nbars:]
            bars = []
            for i, v in enumerate(hist):
                breathe = 0.05 + 0.04 * math.sin(self.phase * 1.7 + i * 0.7)
                bars.append(max(breathe, min(1.0, v * 1.8)))
            pulse = 0.5 + 0.5 * math.sin(self.phase * 0.9)
            self._set_frame(None, lambda: r.listening(bars, pulse))
        elif self.state == FINISHING:
            self._set_frame(None, lambda: r.dots(self.phase))
        elif self.state == DOWNLOADING:
            self._set_frame(None, lambda: r.downloading(self.dl_frac))
        elif self.state == LOADING:
            self._set_frame(("loading",), lambda: r.idle(False, dim=True))
        else:
            self._set_frame(("idle", self.hover),
                            lambda: r.idle(self.hover))

    def run(self):
        self.root.mainloop()

# ----------------------------------------------------------------------------

def selftest(wav_path):
    """Transcribe a wav file and write the result next to the exe (build check)."""
    import wave
    with wave.open(wav_path, "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        audio = audio.astype(np.float32) / 32768.0
    t = ParakeetTranscriber(load_settings())
    t.load()
    out = os.path.join(APP_DIR, "selftest_out.txt")
    with open(out, "w", encoding="utf-8") as f:
        if t.error:
            f.write("LOAD ERROR: " + t.error)
        else:
            f.write("live: " + (t.transcribe(audio) or "(empty)") + "\n")
            f.write("long: " + (t.transcribe(audio, long=True) or "(empty)"))


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
        return
    if already_running():
        ctypes.windll.user32.MessageBoxW(
            0, "DictationMic is already running — look for the small dark pill "
               "floating on your screen.", "DictationMic", 0x40)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    DictationApp().run()


if __name__ == "__main__":
    main()
