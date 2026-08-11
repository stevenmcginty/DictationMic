---
name: watch
description: Build, sideload and debug the Wear OS watch app (android/wear) on Steve's Pixel Watch 2. Use when Steve says "/watch", mentions the watch, the watch face, the tile, the complication, or asks to put a new build on his wrist.
---

# The watch

The watch app lives in `android/wear/` (Compose for Wear OS), sharing the
dictation engine, note store and cloud sync with the phone via `android/core/`.
There is **no updater on the watch** — building an APK ships nothing. The only
way a change reaches the wrist is a sideload, so finish every change by
installing it.

## Device

Google Pixel Watch 2 (`eos`), Wear OS SDK 37, connects over Wi-Fi adb
(wireless debugging). It usually shows up already connected as an mDNS device:

```
adb devices    # look for adb-38241RTJWR8BQP-...._adb-tls-connect._tcp
```

If not connected: Steve reads IP:port off the watch (Settings > Developer
options > Wireless debugging — the port changes every reboot), then
`adb connect <ip>:<port>`.

adb path: `C:\Users\steve\AppData\Local\Android\Sdk\platform-tools\adb.exe`.
Prefer PowerShell for adb — Git Bash mangles `/sdcard/...` paths.

## Build and sideload

```powershell
Set-Location <repo>\android
$env:JAVA_HOME = "C:\Program Files\Microsoft\jdk-21.0.11.10-hotspot"
& (Get-Item "$env:USERPROFILE\.gradle\wrapper\dists\gradle-8.11.1-all\*\gradle-8.11.1\bin\gradle.bat").FullName :wear:assembleRelease --console=plain
Copy-Item wear\build\outputs\apk\release\wear-release.apk ..\DictationMic-Wear.apk -Force
adb install -r ..\DictationMic-Wear.apk
```

Same keystore as the phone (`android/keystore.properties`), so `install -r`
keeps data and sign-in. Bump `versionCode`/`versionName` in
`android/wear/build.gradle.kts` on every sideloaded build.

## What the app puts on the watch

- **Watch face** (`DictationWatchFaceService`) — Steve's daily face: date, big
  lime time, steps (Fitbit provider) left, heart rate right, the mic dead
  centre (tap = start dictating), battery ring around the rim. Set it active
  from adb with:
  `adb shell am broadcast -a com.google.android.wearable.app.DEBUG_SURFACE --es operation set-watchface --ecn component org.dictationmic.wear/.DictationWatchFaceService`
- **Tile** (`DictationTileService`) — one swipe, one tap. Added to the
  carousel manually on the watch; no API can do it.
- **Complication** (`DictationComplicationService`) — mic slot for any face
  that takes third-party complications.
- **NetworkBoost** — holds LTE up while the app/tile/dictation needs it,
  because the watch otherwise takes ~1 min to fail over from a dead
  Bluetooth proxy after walking away from the phone.
- **WalkAwayGuard** — catches the walk-away at its earliest signals (a
  Bluetooth ACL disconnect, any non-cellular network lost) and holds LTE
  for two minutes, so the modem attaches while the routing layer is still
  waiting out its timeouts. Needs BLUETOOTH_CONNECT for the fast path
  (granted on Steve's watch via `pm grant`).

## Debugging

- `adb logcat -d | Select-String DictationMic` — one tag for the whole
  pipeline (recogniser errors, sync results, cellular holds).
- `adb exec-out screencap -p > shot.png` — see the watch.
- `adb shell uiautomator dump /sdcard/ui.xml` then read bounds before blindly
  tapping UI — blind coordinate taps have hit "Force stop?" dialogs before.
- Network requests: `adb shell dumpsys connectivity | Select-String dictationmic`
  shows whether our cellular request is registered.
