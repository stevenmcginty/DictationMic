"""
bench_stt — race the speech engines on this machine.

    venv\\Scripts\\python.exe bench_stt.py clip.wav [clip.txt] [clip2.wav ...]

Each wav is decoded to 16 kHz mono and pushed through every engine that has
its model files on disk: Whisper small.en (live dictation), Whisper
medium.en (voice notes) and Parakeet (the optional engine). A .txt with the
same stem is treated as the reference read and scores a word error rate.
Clips longer than 30 s run the voice-note path (long=True: VAD + wide beam
for Whisper, chunked for Parakeet) — the same code the pill runs.
"""

import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402  — classes and paths only; no UI at import time


def load_wav(path):
    from faster_whisper.audio import decode_audio
    return decode_audio(path, sampling_rate=app.SAMPLE_RATE)


def norm_words(text):
    text = re.sub(r"[^\w\s']", " ", text.lower())
    return text.split()


def wer(ref, hyp):
    """Plain Levenshtein on normalised words, as a % of the reference."""
    r, h = norm_words(ref), norm_words(hyp)
    if not r:
        return 0.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (rw != hw))
        prev = cur
    return 100.0 * prev[-1] / len(r)


def engines(settings):
    if app.model_files_ready("small.en"):
        s = dict(settings, model="small.en")
        yield "whisper small.en (live)", app.Transcriber(s)
    if app.model_files_ready("medium.en"):
        s = dict(settings, voice_model="medium.en")
        yield "whisper medium.en (notes)", app.Transcriber(s, model_key="voice_model")
    if app.parakeet_files_ready():
        yield "parakeet tdt-0.6b-v2", app.ParakeetTranscriber(settings)


def main(paths):
    wavs = []
    for p in paths:
        if p.lower().endswith(".txt"):
            continue
        ref_path = os.path.splitext(p)[0] + ".txt"
        ref = ""
        if os.path.isfile(ref_path):
            with open(ref_path, "r", encoding="utf-8") as f:
                ref = f.read()
        wavs.append((p, load_wav(p), ref))
    if not wavs:
        print(__doc__)
        return

    settings = app.load_settings()
    for name, t in engines(settings):
        t0 = time.perf_counter()
        t.load()
        if t.model is None:
            print(f"\n== {name}: failed to load — {t.error}")
            continue
        load_s = time.perf_counter() - t0
        t.transcribe(np.zeros(app.SAMPLE_RATE // 2, np.float32))  # warm-up
        print(f"\n== {name}  (loaded in {load_s:.1f}s)")
        for path, audio, ref in wavs:
            dur = len(audio) / app.SAMPLE_RATE
            long = dur > 30
            t0 = time.perf_counter()
            text = t.transcribe(audio, long=long)
            dt = time.perf_counter() - t0
            score = f"  WER {wer(ref, text):4.1f}%" if ref else ""
            print(f"  {os.path.basename(path)}: {dur:5.1f}s audio -> "
                  f"{dt:5.2f}s  ({dur / max(dt, 1e-9):4.1f}x realtime)"
                  f"{score}{'  [long path]' if long else ''}")
            print(f"    {text[:160]}{'…' if len(text) > 160 else ''}")
        t.model = None   # release before the next engine loads


if __name__ == "__main__":
    main(sys.argv[1:])
