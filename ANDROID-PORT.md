# DictationMic for Android — investigation (2026-07-10)

**Verdict: fully feasible, including Parakeet.** The exact model the desktop
app uses (Parakeet TDT 0.6B, int8 ONNX) runs on-device on Android today via
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) — real-time on a modern
phone CPU, ~232 MB RAM while running, completely offline. Background
recording with the screen off is a solved, Play-Store-sanctioned pattern
(it's exactly what Google Recorder does). And the beep problem disappears
automatically: the beep comes from Android's `SpeechRecognizer` /
Web Speech API; a native app reading the mic with `AudioRecord` is silent.

## Try it before we build anything (10 minutes)

sherpa-onnx publishes a prebuilt demo APK running Parakeet with VAD-chunked
"simulated streaming" (same phrase-at-a-pause behaviour as the desktop pill):

- English (same model as desktop):
  `https://huggingface.co/csukuangfj2/sherpa-onnx-apk/resolve/main/vad-asr-simulated-streaming/1.13.4/sherpa-onnx-1.13.4-arm64-v8a-simulated_streaming_asr-en-parakeet_tdt_0.6b_v2.apk`
- 25-language European variant (v3), if multilingual ever matters:
  same folder, `...-multi-parakeet_tdt_0.6b_v3.apk`

Sideload it (Chrome download → allow "install unknown apps") and talk at it.
That answers the only real open question — how Parakeet feels on *your*
phone — before a line of our code exists. It's a bare demo (no background
mode, no notes); we're testing the engine, not the app.

## What the app would be

A native **Kotlin** app (Jetpack Compose UI), replacing nothing on the
desktop — it's the phone-side peer of the pill:

1. **One-tap record, then walk away.** Tap the mic (in-app button, home
   screen widget, or Quick Settings tile). The app starts a **foreground
   service** with `foregroundServiceType="microphone"` — this is the
   official mechanism for "keep capturing after the user leaves / locks the
   screen", the same one Google Recorder uses. A persistent notification
   shows recording state + live transcript tail + Stop button. Screen off,
   other apps in front — recording and transcription keep going.
2. **On-device Parakeet, live.** `AudioRecord` (raw mic, **no beep**) →
   Silero VAD → phrase chunks → sherpa-onnx offline recognizer running
   parakeet-tdt-0.6b-v2 int8. Text accumulates live exactly like the pill.
   sherpa-onnx ships a Kotlin API + prebuilt `.aar`, and their
   `SherpaOnnxSimulateStreamingAsr` Android Studio example is essentially
   this pipeline ready-made.
3. **Notes + sync unchanged.** Finished dictations save locally and sync
   through the existing `dictationmic-sync` Firebase project (native
   Firebase Android SDK — better offline queueing than the PWA). Big win
   over today: the phone transcribes *itself*, so notes arrive as **text
   immediately** — no waiting for the computer to be on. The PC pill keeps
   working exactly as now.
4. **Model download on first run**, like desktop: ~660 MB one-time fetch
   with resume, stored in app storage. Keeps the store download tiny
   (the APK itself would be ~30–50 MB). RAM while dictating ≈ 232 MB —
   fine on any phone from the last ~6 years.

### Phase 2 (optional, the real "system-wide dictation" prize)

Android has a public API for **being the system voice-input engine**: a
`RecognitionService` + voice IME, which is how
[FUTO Voice Input](https://voiceinput.futo.org/) replaces Google voice
typing with on-device Whisper. We can do the same with Parakeet: press the
mic key on the keyboard in *any* app and our engine types into that app —
offline, beep-free, no switching apps. This is the phone equivalent of the
pill typing into whatever box has focus. It's a separate component in the
same APK, cleanly deferrable.

## Background-recording rules (Android 14/15) — all compatible with our flow

- Must declare `FOREGROUND_SERVICE_MICROPHONE` + service type in the
  manifest, and hold the `RECORD_AUDIO` runtime permission.
- The mic foreground service **must be started while the app is visible**
  (tap first, then background) — you cannot begin listening from the
  background or on boot. Our flow is user-initiated by definition, so this
  costs nothing. Widget/QS-tile starts route through a momentary trampoline
  activity (standard pattern).
- Some OEMs (Samsung/Xiaomi) aggressively kill services — the app should
  prompt once for battery-optimization exemption, like every recorder app.

## Play Store checklist

- **$25 one-time** developer account. NOTE: new *personal* accounts must run
  a **closed test with ~12 testers for 14 days** before production access —
  the main schedule risk. (Doesn't apply to organisation accounts.)
- **Foreground-service declaration** in Play Console: describe the mic FGS
  use + a short screen-recording video demonstrating it. "Voice recorder /
  dictation" is the textbook accepted use case for the microphone type.
- **Data safety form**: easy — audio never leaves the device; note text
  syncs to the user's own Firebase account (opt-in).
- Ship as AAB, arm64-v8a (+ x86_64 for emulators). Model downloaded at
  runtime, not bundled.
- Alternative/parallel channel: publish the APK on the website (like the
  Windows zip) and skip Play entirely — no 14-day testing gate, but no
  auto-updates or discoverability. Doing both is common.

## Why not upgrade the PWA instead

Already at its ceiling: web apps can't keep the mic in the background
(service killed / tab throttled), Web Speech API beeps on every restart and
needs Google's servers, and there's no WebGPU-class ASR that matches
Parakeet on-device reliably. The two genuine requirements — background mic
and silent capture — are native-only. Capacitor/React Native wrappers don't
help either: the hard 20% (foreground service, JNI to sherpa-onnx, voice
IME) is native regardless, so a plain Kotlin app is less total machinery.

## Status — Phase 1 BUILT (2026-07-10)

`android\` in this repo is now a working Kotlin/Compose app; the signed APK
is `DictationMic-Android.apk` in the repo root (36 MB, arm64). What's in:

- Tap the volt mic → **microphone foreground service** keeps dictating with
  the screen off / other apps in front; silent notification shows the
  growing transcript with a **Stop & save** action. No beeps anywhere.
- **Parakeet TDT 0.6B int8 on-device** (sherpa-onnx v1.13.4, same model as
  the desktop pill): Silero VAD phrase chunking, live partials every 300 ms,
  finals at each pause. Model (~660 MB) downloads on first run with resume.
- Notes save locally and **sync through the existing `dictationmic-sync`
  Firebase** (same REST protocol as cloudsync.py / the PWA — sign in with
  the same email/password). Phone transcribes itself, so notes reach the
  laptop as finished text; laptop notes appear in the app.
- "Stop after silence" setting: Off (default) / 10s / 30s / 2m.

Rebuild: `cd android && gradle assembleRelease` (Gradle 8.10.2 in
`%USERPROFILE%\gradle-8.10.2`, JDK 21, SDK in `%LOCALAPPDATA%\Android\Sdk`).
Signing: `android\dictationmic-release.jks` + `keystore.properties`
(gitignored — **back these up**; Play needs the same key for every update).

Still to do (Phases 2-4): Quick Settings tile + widget, battery-exemption
prompt, voice IME (system-wide dictation), Play Store listing.

## Proposed build order

| Phase | Deliverable |
|-------|-------------|
| 0 | Sideload the prebuilt demo APK on Steve's phone — judge speed/accuracy |
| 1 | Kotlin app: mic FGS + VAD + Parakeet live dictation + notes list + Firebase sync (the Google-Recorder-alike MVP, sideloadable) |
| 2 | Quick Settings tile, widget, live-transcript notification, battery-exemption prompt |
| 3 | Voice IME / RecognitionService — system-wide dictation into any app |
| 4 | Play listing: signing, FGS declaration + demo video, closed testing → production |

Sources: [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) ·
[prebuilt APKs](https://k2-fsa.github.io/sherpa/onnx/android/apk-simulate-streaming-asr.html) ·
[NeMo/parakeet models in sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html) ·
[Parakeet on Android guide (Soniqo)](https://soniqo.audio/guides/parakeet/android) ·
[FGS types (Android docs)](https://developer.android.com/develop/background-work/services/fgs/service-types) ·
[FGS types required, Android 14](https://developer.android.com/about/versions/14/changes/fgs-types-required) ·
[Play Console FGS declaration](https://support.google.com/googleplay/android-developer/answer/13392821) ·
[FUTO Voice Input](https://voiceinput.futo.org/)
