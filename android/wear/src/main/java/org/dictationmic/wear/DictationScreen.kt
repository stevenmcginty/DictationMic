package org.dictationmic.wear

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material.Text
import kotlinx.coroutines.delay
import org.dictationmic.android.AudioRoute
import org.dictationmic.android.CloudSync
import org.dictationmic.android.DictationService
import org.dictationmic.android.DictationState

// How long the confirmation stays up. Long enough to read on the way down,
// short enough that the arm is never waiting on it.
private const val SAVED_FLASH_MS = 1800L

// The one screen: a mic button, and a line of sync status under it.
//
// The transcription is the phone's, shared verbatim from :core. That is a
// reversal: this screen used to hand off to the system's one-shot voice input
// screen, because the watch had no RecognitionService at all — SpeechRecognizer
// answered ERROR_CLIENT immediately, forever, which on the wrist looks exactly
// like a microphone that isn't listening.
//
// The watch has one now. Measured on a Pixel Watch 2, Wear on SDK 37:
// isRecognitionAvailable=true, the microphone opens with a real level on it,
// and five restart cycles ran back to back with a 251ms gap and no ERROR_CLIENT
// and no BUSY. A deliberate pause mid-sentence ended one cycle with a result
// and the next one caught the words after it, losing nothing.
//
// That restart loop is the whole point. The system's voice screen decides when
// you have stopped talking, so a pause to think ends the note — useless for
// dictating on a walk. DictationService restarts the recogniser instead, so a
// pause is a cycle boundary rather than an ending, and the note runs as long as
// you do. Saying "end note" finishes it without touching the watch.
//
// One thing the watch does not have is an offline model
// (isOnDeviceRecognitionAvailable=false), so it dictates over the network and
// passes offlineOnly=false — see EXTRA_OFFLINE_ONLY in DictationService.
@Composable
fun DictationScreen(
    onAccount: () -> Unit,
    dictateRequest: Boolean = false,
    onDictateHandled: () -> Unit = {},
    onSessionDone: () -> Unit = {},
) {
    val ctx = LocalContext.current
    val haptic = LocalHapticFeedback.current

    val syncState by CloudSync.state.collectAsState()
    val account by CloudSync.email.collectAsState()

    val running by DictationState.running.collectAsState()
    val partial by DictationState.partial.collectAsState()
    val finals by DictationState.finals.collectAsState()
    val level by DictationState.level.collectAsState()
    val savedAt by DictationState.savedNoteAt.collectAsState()
    val savedWords by DictationState.savedWords.collectAsState()
    val savedLanded by DictationState.savedLanded.collectAsState()

    var problem by remember { mutableStateOf<String?>(null) }
    // The gap between asking to stop and the note landing is a save and an
    // upload, and an unexplained blank second reads as a note that went nowhere.
    var stopping by remember { mutableStateOf(false) }
    // Only a save that happened after this screen appeared is worth flashing.
    // Without this the confirmation from the previous session is on screen the
    // moment the app opens.
    val firstSeen = remember { DictationState.savedNoteAt.value }
    var flashedAt by remember { mutableLongStateOf(firstSeen) }
    var showSaved by remember { mutableStateOf(false) }

    val begin = {
        problem = null
        // Hands-free is not a mode he picks, it is a fact about where the watch
        // is: buds in means the wrist is down and the only feedback that lands
        // is spoken. It also gives the session a silence ceiling, which matters
        // when nothing on screen is being watched.
        DictationService.start(
            ctx,
            handsFree = AudioRoute.headphonesConnected(ctx),
            // No offline model on this watch. Asking for one anyway doesn't
            // degrade, it just returns nothing forever.
            offlineOnly = false,
        )
    }

    val permission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) begin() else problem = "Microphone needed"
    }

    val start = {
        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
        if (ctx.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED) {
            begin()
        } else {
            permission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    val stop = {
        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
        stopping = true
        DictationService.stop(ctx)
    }

    // Arrived from the tile, which has already spent the user's tap. Clear the
    // request before starting, not after: a flag still set when the screen
    // recomposes would start a second session on top of the first.
    LaunchedEffect(dictateRequest) {
        if (!dictateRequest) return@LaunchedEffect
        onDictateHandled()
        if (!running) start()
    }

    // The service saves and uploads on its own — including when "end note" ended
    // the session and nobody touched the screen — so the confirmation follows
    // the timestamp rather than anything this screen did.
    LaunchedEffect(savedAt) {
        if (savedAt <= flashedAt) return@LaunchedEffect
        flashedAt = savedAt
        stopping = false
        showSaved = true
        delay(SAVED_FLASH_MS)
        showSaved = false
        // The receipt has been read; a face-tap launch now steps aside so the
        // wrist is back on the watch face, not parked on the idle mic screen.
        onSessionDone()
    }

    // A session can also end with nothing said, which saves nothing and stamps
    // no timestamp. Without this the screen sits on "Saving…" forever.
    LaunchedEffect(running) {
        if (!running && stopping) {
            delay(SAVED_FLASH_MS)
            stopping = false
            onSessionDone()      // ended empty — same exit as a saved one
        }
    }

    Box(
        modifier = Modifier.fillMaxSize().background(Ink),
        contentAlignment = Alignment.Center,
    ) {
        when {
            showSaved -> SavedFlash(words = savedWords, landed = savedLanded)
            running -> Listening(
                text = listOf(finals, partial).filter { it.isNotBlank() }.joinToString(" "),
                level = level,
                onStop = stop,
            )
            stopping -> Working()
            else -> Idle(
                status = problem ?: statusLine(syncState, account),
                warn = problem != null,
                onStart = start,
                onAccount = onAccount,
            )
        }
    }
}

// What the line under the mic button says. Sync is the whole point of the watch
// app — a note that never leaves the wrist is a note nobody can read — so its
// state is the one thing worth a permanent line of an inch-wide screen.
private fun statusLine(state: String, account: String?): String = when {
    account == null || state == "off" -> "Sign in to sync"
    state == "needs-signin" -> "Sign in again"
    state == "offline" -> "Offline — will sync later"
    state == "syncing" -> "Syncing…"
    else -> account
}

@Composable
private fun Idle(
    status: String,
    warn: Boolean,
    onStart: () -> Unit,
    onAccount: () -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxSize().padding(horizontal = 22.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Box(
            modifier = Modifier
                .size(92.dp)
                .clip(CircleShape)
                .background(Volt)
                .clickable(onClick = onStart),
            contentAlignment = Alignment.Center,
        ) {
            MicGlyph(colour = Ink)
        }
        Spacer(Modifier.height(10.dp))
        Text(
            text = status,
            modifier = Modifier.clickable(onClick = onAccount),
            color = if (warn) Warn else Muted,
            fontSize = 11.sp,
            textAlign = TextAlign.Center,
            maxLines = 2,
            lineHeight = 13.sp,
        )
    }
}

// The live screen. The one question a dictation cannot answer for itself is
// whether it is being heard, so this shows the words as they arrive and a ring
// that moves with the level — a ring that is still while you talk means the
// microphone is not yours, and that is worth being able to see at a glance.
//
// The tail of the text, not the head: what matters mid-sentence is the last
// thing transcribed, and an inch of screen holds a few words either way.
@Composable
private fun Listening(text: String, level: Float, onStop: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(horizontal = 18.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(
            text = text.ifBlank { "Listening…" },
            color = if (text.isBlank()) Muted else Chalk,
            fontSize = 13.sp,
            lineHeight = 16.sp,
            textAlign = TextAlign.Center,
            maxLines = 4,
        )
        Spacer(Modifier.height(12.dp))
        Box(
            modifier = Modifier.size(64.dp).clickable(onClick = onStop),
            contentAlignment = Alignment.Center,
        ) {
            LevelRing(level = level)
            // A square, the universal stop. A second mic glyph here would say
            // "start" to anyone glancing at it.
            Box(modifier = Modifier.size(20.dp).clip(CircleShape).background(Warn))
        }
        Spacer(Modifier.height(8.dp))
        Text(
            text = "Tap to finish, or say \"end note\"",
            color = Muted,
            fontSize = 10.sp,
            textAlign = TextAlign.Center,
            lineHeight = 12.sp,
        )
    }
}

@Composable
private fun LevelRing(level: Float) {
    Canvas(modifier = Modifier.size(64.dp)) {
        val stroke = size.width * 0.07f
        drawCircle(
            color = Panel,
            radius = (size.width - stroke) / 2f,
            style = Stroke(width = stroke),
        )
        drawArc(
            color = Volt,
            startAngle = -90f,
            sweepAngle = 360f * level.coerceIn(0f, 1f),
            useCenter = false,
            topLeft = Offset(stroke / 2f, stroke / 2f),
            size = Size(size.width - stroke, size.height - stroke),
            style = Stroke(width = stroke, cap = StrokeCap.Round),
        )
    }
}

// The gap between the session ending and the confirmation appearing is a save
// and a network round trip, and an unexplained blank second reads as a note that
// went nowhere.
@Composable
private fun Working() {
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(text = "Saving…", color = Muted, fontSize = 13.sp)
    }
}

// The receipt. A tick on its own only says the tap registered; what actually
// needs answering on the way down is that words were heard, and that they left
// the wrist — a dictation in a dead spot looks exactly like a successful one
// until you go looking for it on the laptop.
@Composable
private fun SavedFlash(words: Int, landed: Boolean) {
    Column(
        modifier = Modifier.fillMaxSize().padding(horizontal = 22.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(text = "Saved", color = Volt, fontSize = 20.sp)
        Spacer(Modifier.height(2.dp))
        Text(
            text = if (words == 1) "1 word" else "$words words",
            color = Chalk,
            fontSize = 13.sp,
        )
        Spacer(Modifier.height(4.dp))
        Text(
            text = if (landed) "On your laptop" else "On the watch — syncing",
            color = Muted,
            fontSize = 11.sp,
            textAlign = TextAlign.Center,
            lineHeight = 13.sp,
        )
    }
}

// Drawn rather than shipped as a vector: it is three shapes, and this way it
// takes its colour from the palette above with no drawable to keep in step.
@Composable
private fun MicGlyph(colour: Color) {
    Canvas(modifier = Modifier.size(38.dp)) {
        val w = size.width
        val h = size.height
        val capsule = w * 0.34f
        val stroke = w * 0.09f
        drawRoundRect(
            color = colour,
            topLeft = Offset((w - capsule) / 2f, h * 0.08f),
            size = Size(capsule, h * 0.48f),
            cornerRadius = CornerRadius(capsule / 2f, capsule / 2f),
        )
        drawArc(
            color = colour,
            startAngle = 0f,
            sweepAngle = 180f,
            useCenter = false,
            topLeft = Offset(w * 0.18f, h * 0.36f),
            size = Size(w * 0.64f, w * 0.5f),
            style = Stroke(width = stroke, cap = StrokeCap.Round),
        )
        drawLine(
            color = colour,
            start = Offset(w / 2f, h * 0.79f),
            end = Offset(w / 2f, h * 0.93f),
            strokeWidth = stroke,
            cap = StrokeCap.Round,
        )
    }
}
