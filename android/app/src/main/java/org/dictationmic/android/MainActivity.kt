package org.dictationmic.android

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.ui.platform.LocalLifecycleOwner
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.DateFormat
import java.util.Date

private val Volt = Color(0xFFB6EE3F)
private val Bg = Color(0xFF0B0C0A)
private val Surface1 = Color(0xFF131512)
private val Surface2 = Color(0xFF1A1D18)
private val Ink = Color(0xFFECEEE7)
private val InkDim = Color(0xFF8B9184)
private val Amber = Color(0xFFF4C752)

class MainActivity : ComponentActivity() {

    private var pendingStart = false

    private val permLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        if (grants[Manifest.permission.RECORD_AUDIO] == true && pendingStart) {
            pendingStart = false
            DictationService.start(this)
        }
    }

    fun startDictation() {
        val need = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) need += Manifest.permission.RECORD_AUDIO
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) need += Manifest.permission.POST_NOTIFICATIONS
        if (need.isEmpty()) {
            DictationService.start(this)
        } else {
            pendingStart = true
            permLauncher.launch(need.toTypedArray())
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        CloudSync.loadAccount(this)
        setContent {
            MaterialTheme(
                colorScheme = darkColorScheme(
                    primary = Volt, background = Bg, surface = Surface1,
                    onPrimary = Bg, onBackground = Ink, onSurface = Ink,
                )
            ) {
                Surface(Modifier.fillMaxSize(), color = Bg) { AppRoot(this) }
            }
        }
    }
}

@Composable
fun AppRoot(activity: MainActivity) {
    val scope = rememberCoroutineScope()

    // pull cloud notes whenever we come back to the foreground
    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(Unit) {
        val obs = LifecycleEventObserver { _, e ->
            if (e == Lifecycle.Event.ON_RESUME) {
                scope.launch { CloudSync.syncNow(activity.applicationContext) }
            }
        }
        lifecycleOwner.lifecycle.addObserver(obs)
    }

    // Transcription is Android's built-in speech recognition now — nothing to
    // download or warm up, so we open straight into the recorder.
    MainScreen(activity)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(activity: MainActivity) {
    val running by DictationState.running.collectAsState()
    val finals by DictationState.finals.collectAsState()
    val partial by DictationState.partial.collectAsState()
    val level by DictationState.level.collectAsState()
    val changed by NoteStore.changed.collectAsState()
    val syncState by CloudSync.state.collectAsState()
    val account by CloudSync.email.collectAsState()
    val scope = rememberCoroutineScope()

    var notes by remember { mutableStateOf(NoteStore.all(activity)) }
    // Off the main thread: a live dictation rewrites its note every second or
    // so, and re-reading every note file on the UI thread that often janks the
    // meter.
    LaunchedEffect(changed, syncState) {
        notes = withContext(Dispatchers.IO) { NoteStore.all(activity) }
    }
    // The service pushes as it goes and syncs again the moment a session ends,
    // so there's nothing to do here on savedNoteAt any more — a sync from this
    // side would only mean a second full snapshot download per dictation.

    var showAccount by remember { mutableStateOf(false) }
    var openNote by remember { mutableStateOf<Note?>(null) }

    Column(Modifier.fillMaxSize().padding(20.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("DictationMic", color = Ink, fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.weight(1f))
            val dot = when (syncState) {
                "ok" -> Volt; "syncing" -> Amber
                "off" -> InkDim; else -> Color(0xFFFF5C48)
            }
            Box(Modifier.size(8.dp).background(dot, CircleShape))
            Spacer(Modifier.width(8.dp))
            Text(
                account ?: "phone sync",
                color = InkDim, fontSize = 13.sp,
                modifier = Modifier.clickable { showAccount = true },
            )
        }

        Spacer(Modifier.height(20.dp))

        // The big mic. Volt when listening (with a soft pulse), dark when idle.
        val pulse = rememberInfiniteTransition(label = "pulse").animateFloat(
            initialValue = 1f, targetValue = 1.06f,
            animationSpec = infiniteRepeatable(tween(700), RepeatMode.Reverse),
            label = "pulse",
        )
        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            Box(
                Modifier
                    .size(112.dp)
                    .scale(if (running) pulse.value else 1f)
                    .background(if (running) Volt else Surface2, CircleShape)
                    .border(2.dp, if (running) Volt else InkDim.copy(alpha = 0.4f), CircleShape)
                    .clickable {
                        if (running) DictationService.stop(activity)
                        else activity.startDictation()
                    },
                contentAlignment = Alignment.Center,
            ) {
                if (running) {
                    // simple live bars driven by mic level
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        for (i in 0..4) {
                            val h = (10 + 26 * level * (1f - 0.15f * kotlin.math.abs(i - 2)))
                                .coerceIn(8f, 40f)
                            Box(
                                Modifier.padding(horizontal = 3.dp)
                                    .width(6.dp).height(h.dp)
                                    .background(Bg, RoundedCornerShape(3.dp))
                            )
                        }
                    }
                } else {
                    Text("🎙", fontSize = 40.sp)
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        Text(
            if (running) "Listening — you can leave the app or lock the screen"
            else "Tap to dictate",
            color = if (running) Volt else InkDim, fontSize = 13.sp,
            modifier = Modifier.fillMaxWidth(),
            textAlign = androidx.compose.ui.text.style.TextAlign.Center,
        )

        if (running || finals.isNotBlank() || partial.isNotBlank()) {
            Spacer(Modifier.height(16.dp))
            Surface(
                color = Surface1, shape = RoundedCornerShape(14.dp),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(14.dp)) {
                    Text(
                        buildString {
                            append(finals)
                            if (partial.isNotBlank()) {
                                if (isNotEmpty()) append(' ')
                                append(partial)
                            }
                            if (isEmpty()) append("…")
                        },
                        color = if (partial.isNotBlank()) Ink else InkDim,
                        fontSize = 15.sp, lineHeight = 21.sp,
                    )
                }
            }
        }

        Spacer(Modifier.height(20.dp))
        AutoStopRow(activity)
        Spacer(Modifier.height(12.dp))

        Text("Notes", color = InkDim, fontSize = 13.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(8.dp))
        LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(notes, key = { it.id }) { n ->
                Surface(
                    color = Surface1, shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.fillMaxWidth().clickable { openNote = n },
                ) {
                    Column(Modifier.padding(12.dp)) {
                        Text(n.title, color = Ink, fontSize = 15.sp,
                            maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Spacer(Modifier.height(2.dp))
                        Text(
                            DateFormat.getDateTimeInstance(
                                DateFormat.MEDIUM, DateFormat.SHORT)
                                .format(Date(n.createdAt)),
                            color = InkDim, fontSize = 12.sp,
                        )
                    }
                }
            }
        }
    }

    if (showAccount) {
        AccountSheet(activity, onDone = { showAccount = false })
    }

    openNote?.let { n ->
        val clipboard = LocalClipboardManager.current
        AlertDialog(
            onDismissRequest = { openNote = null },
            containerColor = Surface2,
            title = { Text(n.title, color = Ink) },
            text = {
                Text(n.body.ifBlank { "(no words captured)" },
                    color = Ink)
            },
            confirmButton = {
                TextButton(onClick = {
                    clipboard.setText(AnnotatedString(n.body))
                    openNote = null
                }) { Text("Copy", color = Volt) }
            },
            dismissButton = {
                TextButton(onClick = {
                    NoteStore.delete(activity, n.id)
                    openNote = null
                    scope.launch { CloudSync.syncNow(activity.applicationContext) }
                }) { Text("Delete", color = Color(0xFFFF5C48)) }
            },
        )
    }
}

@Composable
fun AutoStopRow(activity: MainActivity) {
    val prefs = activity.getSharedPreferences("app", 0)
    var current by remember { mutableStateOf(prefs.getInt("autoStopSecs", 0)) }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text("Stop after silence:", color = InkDim, fontSize = 13.sp)
        Spacer(Modifier.width(8.dp))
        for ((label, secs) in listOf("Off" to 0, "10s" to 10, "30s" to 30, "2m" to 120)) {
            val sel = current == secs
            Text(
                label,
                color = if (sel) Bg else InkDim, fontSize = 12.sp,
                modifier = Modifier
                    .padding(end = 6.dp)
                    .background(if (sel) Volt else Surface2, RoundedCornerShape(10.dp))
                    .clickable {
                        current = secs
                        prefs.edit().putInt("autoStopSecs", secs).apply()
                    }
                    .padding(horizontal = 10.dp, vertical = 4.dp),
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AccountSheet(activity: MainActivity, onDone: () -> Unit) {
    val account by CloudSync.email.collectAsState()
    val scope = rememberCoroutineScope()
    var mail by remember { mutableStateOf("") }
    var pass by remember { mutableStateOf("") }
    var msg by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    ModalBottomSheet(onDismissRequest = onDone, containerColor = Surface2) {
        Column(Modifier.padding(24.dp)) {
            if (account != null) {
                Text("Signed in as $account", color = Ink, fontSize = 15.sp)
                Spacer(Modifier.height(4.dp))
                Text(
                    "Notes sync with the computer through your own Firebase account.",
                    color = InkDim, fontSize = 13.sp,
                )
                Spacer(Modifier.height(16.dp))
                Button(
                    onClick = { CloudSync.signOut(activity); onDone() },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Surface1, contentColor = Ink),
                ) { Text("Sign out") }
            } else {
                Text("Phone sync", color = Ink, fontSize = 17.sp,
                    fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(4.dp))
                Text(
                    "Use the same email and password as on the computer " +
                        "(right-click the pill → Set up phone sync).",
                    color = InkDim, fontSize = 13.sp,
                )
                Spacer(Modifier.height(16.dp))
                OutlinedTextField(mail, { mail = it }, label = { Text("Email") },
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(pass, { pass = it }, label = { Text("Password") },
                    singleLine = true, modifier = Modifier.fillMaxWidth(),
                    visualTransformation = PasswordVisualTransformation())
                msg?.let {
                    Spacer(Modifier.height(8.dp))
                    Text(it, color = Amber, fontSize = 13.sp)
                }
                Spacer(Modifier.height(16.dp))
                Button(
                    enabled = !busy && mail.isNotBlank() && pass.isNotBlank(),
                    onClick = {
                        busy = true
                        scope.launch {
                            val err = CloudSync.signIn(activity, mail.trim(), pass)
                            busy = false
                            if (err == null) {
                                CloudSync.syncNow(activity.applicationContext)
                                onDone()
                            } else msg = err
                        }
                    },
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Volt, contentColor = Bg),
                ) { Text(if (busy) "Signing in…" else "Sign in", fontWeight = FontWeight.Bold) }
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}
