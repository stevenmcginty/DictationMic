package org.dictationmic.android

import android.content.Context
import android.media.AudioAttributes
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale

// Spoken status. The whole point of the hands-free flow is that the phone is in
// another room or a pocket, so every state change has to arrive in the
// headphones instead of on the screen.
//
// USAGE_MEDIA on purpose: assistant and notification usages get ducked, routed
// to the earpiece, or dropped entirely depending on the phone's audio policy,
// while media reliably lands on whatever the buds are connected as.
object Speaker {
    private var tts: TextToSpeech? = null
    private var ready = false
    private val pending = ArrayDeque<Pair<String, (() -> Unit)?>>()
    private val doneCallbacks = HashMap<String, () -> Unit>()
    private var seq = 0

    fun init(ctx: Context) {
        if (tts != null) return
        tts = TextToSpeech(ctx.applicationContext) { status ->
            ready = status == TextToSpeech.SUCCESS
            if (!ready) {
                // No engine or no voice data. Drain the queue by firing every
                // continuation, or a hands-free session would never start.
                while (pending.isNotEmpty()) pending.removeFirst().second?.invoke()
                return@TextToSpeech
            }
            tts?.language = Locale.UK
            tts?.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build())
            tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(id: String?) {}
                override fun onDone(id: String?) { fire(id) }
                @Deprecated("kept for older engines")
                override fun onError(id: String?) { fire(id) }
                override fun onError(id: String?, code: Int) { fire(id) }
            })
            while (pending.isNotEmpty()) {
                val (text, then) = pending.removeFirst()
                speak(text, then = then)
            }
        }
    }

    private fun fire(id: String?) {
        val cb = synchronized(doneCallbacks) { doneCallbacks.remove(id ?: "") }
        cb?.invoke()
    }

    // `then` runs once the words have finished playing. Callers that are about
    // to start recognising need that: the recogniser has the microphone to
    // itself and would happily transcribe our own announcement into the note.
    // `flush` false queues behind whatever is already playing. A closing line
    // ("Saved, forty words") must not cut off the line that preceded it
    // ("Got it, saving") — from the outside that sounds like a crash.
    fun speak(text: String, flush: Boolean = true, then: (() -> Unit)? = null) {
        if (text.isBlank()) { then?.invoke(); return }
        val engine = tts
        if (engine == null || !ready) { pending.addLast(text to then); return }
        val id = "dm-${seq++}"
        if (then != null) synchronized(doneCallbacks) { doneCallbacks[id] = then }
        val mode = if (flush) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD
        val res = engine.speak(text, mode, null, id)
        // A rejected utterance never produces a progress callback, so a caller
        // waiting to start dictating would wait forever.
        if (res != TextToSpeech.SUCCESS) fire(id)
    }

    fun shutdown() {
        runCatching { tts?.shutdown() }
        tts = null
        ready = false
    }
}
