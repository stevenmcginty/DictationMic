package org.dictationmic.android

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import java.util.Locale

// Everything the UI needs to mirror a session, whether or not the activity
// is alive. Single-process app, so plain shared state flows are enough.
object DictationState {
    val running = MutableStateFlow(false)
    val finals = MutableStateFlow("")     // finished phrases so far
    val partial = MutableStateFlow("")    // phrase being spoken right now
    val level = MutableStateFlow(0f)      // mic level 0..1 for the meter
    val savedNoteAt = MutableStateFlow(0L)

    val fullText: String
        get() {
            val f = finals.value.trim()
            val p = partial.value.trim()
            return when {
                f.isEmpty() -> p
                p.isEmpty() -> f
                else -> "$f $p"
            }
        }
}

// The recorder: a microphone foreground service, so dictation keeps running
// with the screen off or another app in front — Google-Recorder style.
//
// Transcription is Android's own on-device speech recognition (the same
// engine Google Recorder and Gboard voice typing use), reached through
// SpeechRecognizer. It ends each utterance on a natural pause, so we keep it
// continuous by restarting it — accumulating finished phrases into `finals`
// and mirroring the phrase in flight into `partial`.
class DictationService : Service() {
    companion object {
        const val ACTION_START = "org.dictationmic.android.START"
        const val ACTION_STOP = "org.dictationmic.android.STOP"
        const val EXTRA_HANDS_FREE = "handsFree"
        private const val CHANNEL = "dictation"
        private const val NOTIF_ID = 1
        // Live upload cadence: at most one PATCH per gap, always carrying the
        // newest text. Quick enough that the laptop feels live, slow enough
        // that a long dictation isn't a hundred requests.
        private const val LIVE_PUSH_GAP_MS = 1500L
        // Hands-free is for talking with the phone out of sight, so a pause to
        // think must not end the session. Only a long true silence does.
        private const val HANDS_FREE_STOP_MS = 300_000L
        // Assistant is still saying "Opening DictationMic" when we get here, and
        // it holds audio focus — speak over it and the opening line is clipped
        // or dropped entirely, which reads as the app not having started.
        private const val ASSISTANT_HANDOVER_MS = 900L
        // Nothing heard at all for this long: say so. Without it, a session that
        // never got the microphone is indistinguishable from one that is
        // listening perfectly to someone who has no screen to check.
        private const val NUDGE_AFTER_MS = 25_000L

        // The only way to finish a note when the phone is in a pocket. Matched
        // at the end of a finished phrase so it can't fire off a word said
        // mid-sentence, and stripped out so it never lands in the note.
        val STOP_PHRASE = Regex(
            """[\s,.]*\b(?:end note|save note|stop dictation|finish note)\b[\s,.!]*$""",
            RegexOption.IGNORE_CASE)

        fun start(ctx: Context, handsFree: Boolean = false) {
            val i = Intent(ctx, DictationService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_HANDS_FREE, handsFree)
            ctx.startForegroundService(i)
        }

        fun stop(ctx: Context) {
            val i = Intent(ctx, DictationService::class.java).setAction(ACTION_STOP)
            ctx.startService(i)
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val main = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null
    private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var recording = false
    @Volatile private var handsFree = false  // opened by voice: speak the state
    private var listening = false            // a startListening is in flight
    private var lastVoiceAt = 0L             // for the "stop after silence" timer
    private var autoStopMs = 0L
    // the note this session is filling in, created on the first finished phrase
    @Volatile private var liveNoteId: String? = null
    private var pushTicks: Channel<Unit>? = null
    private var liveJob: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START ->
                if (!recording) startSession(intent.getBooleanExtra(EXTRA_HANDS_FREE, false))
            ACTION_STOP -> stopSession()
        }
        return START_NOT_STICKY
    }

    @SuppressLint("MissingPermission", "WakelockTimeout")
    private fun startSession(handsFreeLaunch: Boolean) {
        handsFree = handsFreeLaunch
        createChannel()
        ServiceCompat.startForeground(
            this, NOTIF_ID, buildNotification("Listening…"),
            if (Build.VERSION.SDK_INT >= 30)
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0,
        )
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "dictationmic:rec")
            .apply { acquire() }

        DictationState.finals.value = ""
        DictationState.partial.value = ""
        DictationState.running.value = true
        recording = true
        lastVoiceAt = System.currentTimeMillis()
        autoStopMs = getSharedPreferences("app", MODE_PRIVATE)
            .getInt("autoStopSecs", 0) * 1000L
        // With no screen there is nothing to notice a wedged session, so a
        // hands-free run always gets a ceiling even when the setting says never.
        if (handsFree && autoStopMs == 0L) autoStopMs = HANDS_FREE_STOP_MS

        liveNoteId = null
        // Mint the auth token now, while you're still drawing breath, so the
        // first upload isn't waiting on a round trip to Google.
        scope.launch { runCatching { CloudSync.warmToken(applicationContext) } }
        startLiveUploader()

        // SpeechRecognizer must be created and driven from a thread with a
        // Looper — the main thread. Its callbacks arrive there too.
        main.post {
            recognizer = makeRecognizer().also { it.setRecognitionListener(listener) }
            // Announce first, listen after it has finished playing — the
            // recogniser owns the microphone and would transcribe our own
            // "Listening" straight into the note.
            if (handsFree) {
                Speaker.init(applicationContext)
                main.postDelayed({
                    if (!recording) return@postDelayed
                    // Everything you need with no screen, in one breath: that it
                    // opened, that it's your turn, and how to end it.
                    Speaker.speak(
                        "DictationMic ready. Start talking whenever you like, " +
                            "and say end note when you're done."
                    ) { main.post { if (recording) { listen(); armNudge() } } }
                }, ASSISTANT_HANDOVER_MS)
            } else {
                listen()
            }
        }
    }

    // Prefer the guaranteed on-device recognizer where the platform exposes it
    // (API 33+); otherwise the default Google recognizer with EXTRA_PREFER_OFFLINE.
    private fun makeRecognizer(): SpeechRecognizer =
        if (Build.VERSION.SDK_INT >= 33 &&
            SpeechRecognizer.isOnDeviceRecognitionAvailable(this)) {
            SpeechRecognizer.createOnDeviceSpeechRecognizer(this)
        } else {
            SpeechRecognizer.createSpeechRecognizer(this)
        }

    private fun recognizerIntent(): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag())
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, packageName)
        }

    // "Is it actually hearing me?" is the one question you can't answer without
    // a screen. Stay quiet while words are arriving; speak up only if the very
    // start of a session produces nothing at all, which is what a refused
    // microphone or a recogniser wedged on another app looks like from outside.
    private fun armNudge() {
        main.removeCallbacks(nudge)
        main.postDelayed(nudge, NUDGE_AFTER_MS)
    }

    private val nudge = Runnable {
        if (!recording || !handsFree) return@Runnable
        if (DictationState.finals.value.isNotBlank() ||
            DictationState.partial.value.isNotBlank()) return@Runnable
        Speaker.speak("I'm not hearing anything yet. I'm still listening.")
    }

    private fun listen() {
        if (!recording || listening) return
        listening = true
        DictationState.partial.value = ""
        runCatching { recognizer?.startListening(recognizerIntent()) }
            .onFailure { listening = false; continueOrStop(recreate = true) }
    }

    // After each utterance (a result, or a silence/no-match error) the
    // recognizer has stopped — restart it unless we've been asked to stop or
    // the silence timer has elapsed. A busy/client state needs a fresh
    // recognizer instance to clear, so recreate in that case.
    private fun continueOrStop(recreate: Boolean) {
        if (!recording) return
        if (autoStopMs > 0 && System.currentTimeMillis() - lastVoiceAt > autoStopMs) {
            stopSession(); return
        }
        if (recreate) {
            runCatching { recognizer?.destroy() }
            recognizer = null
            main.postDelayed({
                if (!recording) return@postDelayed
                recognizer = makeRecognizer().also { it.setRecognitionListener(listener) }
                listen()
            }, 120)
        } else {
            main.postDelayed({ listen() }, 20)
        }
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() { lastVoiceAt = System.currentTimeMillis() }

        override fun onRmsChanged(rmsdB: Float) {
            // rmsdB runs roughly -2..10; map to a 0..1 meter level
            DictationState.level.value = ((rmsdB + 2f) / 12f).coerceIn(0f, 1f)
            if (rmsdB > 1.5f) lastVoiceAt = System.currentTimeMillis()
        }

        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() { DictationState.level.value = 0f }

        override fun onPartialResults(partialResults: Bundle?) {
            firstResult(partialResults)?.let {
                DictationState.partial.value = it
                lastVoiceAt = System.currentTimeMillis()
            }
        }

        override fun onResults(results: Bundle?) {
            var text = firstResult(results)
            var ending = false
            if (handsFree && !text.isNullOrBlank() && STOP_PHRASE.containsMatchIn(text)) {
                text = STOP_PHRASE.replace(text, "")
                ending = true
            }
            if (!text.isNullOrBlank()) {
                DictationState.finals.value =
                    (DictationState.finals.value + " " + text).trim()
                updateNotification(DictationState.finals.value)
                lastVoiceAt = System.currentTimeMillis()
                pushTicks?.trySend(Unit)      // send it on while we keep going
            }
            DictationState.partial.value = ""
            listening = false
            if (ending) {
                // Answer before the pause: saving pushes the note and waits on
                // the network, and silence in headphones reads as a failure.
                Speaker.speak("Got it. Saving.")
                stopSession()
                return
            }
            continueOrStop(recreate = false)
        }

        override fun onError(error: Int) {
            DictationState.partial.value = ""
            listening = false
            if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                if (handsFree) {
                    handsFree = false      // stopSession must not also announce
                    Speaker.speak("The microphone is blocked. Open DictationMic.")
                }
                stopSession(); return
            }
            // busy/client leaves the recognizer wedged — swap in a fresh one;
            // plain no-match / timeout just means a silent stretch, reuse it
            val recreate = error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY ||
                error == SpeechRecognizer.ERROR_CLIENT
            continueOrStop(recreate)
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun firstResult(b: Bundle?): String? =
        b?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()

    // The live uploader. Nothing used to leave the phone until the session
    // ended, so the laptop's pill was always at least a whole dictation behind.
    // Now every finished phrase lands in a note and goes straight up, and the
    // pill fills in while you're still talking.
    //
    // A conflated channel is the whole rate limiter: onResults just rings the
    // bell (cheap, main thread), and this loop does one push per
    // LIVE_PUSH_GAP_MS carrying whatever the newest text is by then. A burst of
    // short phrases collapses into a single upload instead of queueing.
    private fun startLiveUploader() {
        val ticks = Channel<Unit>(Channel.CONFLATED)
        pushTicks = ticks
        liveJob = scope.launch {
            for (tick in ticks) {
                if (!CloudSync.enabled) continue
                val text = DictationState.finals.value.trim()
                if (text.isBlank()) continue
                val id = liveNoteId
                    ?: NoteStore.saveNew(applicationContext, text).id
                        .also { liveNoteId = it }
                NoteStore.updateText(applicationContext, id, text)
                runCatching { CloudSync.pushNote(applicationContext, id) }
                delay(LIVE_PUSH_GAP_MS)
            }
        }
    }

    private fun stopSession() {
        if (!recording) { stopSelf(); return }
        recording = false
        main.post {
            listening = false
            runCatching { recognizer?.stopListening() }
            runCatching { recognizer?.destroy() }
            recognizer = null
        }
        pushTicks?.close()                   // the loop finishes its last pass
        val spoken = handsFree
        handsFree = false
        main.removeCallbacks(nudge)
        scope.launch {
            val text = DictationState.fullText.trim()
            DictationState.running.value = false
            DictationState.partial.value = ""
            DictationState.level.value = 0f
            if (text.isNotBlank()) {
                // fold the trailing partial into the note the session has been
                // filling in — or make one, if sync is off or nothing finalised
                val id = liveNoteId?.also {
                    NoteStore.updateText(applicationContext, it, text)
                } ?: NoteStore.saveNew(applicationContext, text).id
                DictationState.savedNoteAt.value = System.currentTimeMillis()
                // the finished wording goes up on its own first; the backlog
                // and the download follow behind it, off the critical path
                val landed = runCatching {
                    CloudSync.pushNote(applicationContext, id)
                }.getOrDefault(false)
                runCatching { CloudSync.syncNow(applicationContext, firstId = id) }
                // Said after the upload, not before it, so "on your computer"
                // is a fact rather than a hope. With no screen this is the only
                // way to know a dictation in a dead spot didn't reach the laptop.
                if (spoken) {
                    val words = Regex("""\S+""").findAll(text).count()
                    Speaker.speak(
                        if (landed) "Saved. $words words, on your computer."
                        else "Saved on the phone, $words words. It'll reach your " +
                            "computer when there's signal.",
                        flush = false)
                }
            } else if (spoken) {
                Speaker.speak("I didn't catch anything, so there's nothing to save.",
                    flush = false)
            }
            liveJob?.cancel()
            liveNoteId = null
            wakeLock?.let { if (it.isHeld) it.release() }
            wakeLock = null
            ServiceCompat.stopForeground(this@DictationService,
                ServiceCompat.STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    override fun onDestroy() {
        recording = false
        main.removeCallbacks(nudge)
        main.post { runCatching { recognizer?.destroy() }; recognizer = null }
        wakeLock?.let { if (it.isHeld) it.release() }
        scope.cancel()
        DictationState.running.value = false
        super.onDestroy()
    }

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Dictation",
                NotificationManager.IMPORTANCE_LOW).apply {
                setSound(null, null)               // silent, always
                enableVibration(false)
            })
    }

    private fun buildNotification(text: String): Notification {
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, DictationService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("DictationMic — listening")
            .setContentText(text.takeLast(80).ifBlank { "Listening…" })
            .setStyle(NotificationCompat.BigTextStyle().bigText(
                text.takeLast(400).ifBlank { "Listening…" }))
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(openApp)
            .addAction(0, "Stop & save", stop)
            .build()
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIF_ID, buildNotification(text))
    }
}
