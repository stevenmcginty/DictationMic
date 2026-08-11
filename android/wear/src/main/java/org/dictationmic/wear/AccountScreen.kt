package org.dictationmic.wear

import android.app.RemoteInput
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.material.Text
import androidx.wear.input.RemoteInputIntentHelper
import kotlinx.coroutines.launch
import org.dictationmic.android.CloudSync

// The two things the sign-in needs, asked for one at a time. They double as the
// result keys the input activity answers with.
private const val ASK_EMAIL = "email"
private const val ASK_PASSWORD = "password"

// Sign in, sign out, and nothing else. This screen is a one-time errand: once
// the watch has an account it is never opened again, so it is worth no more of
// the app than it takes to get through it.
@Composable
fun AccountScreen(onDone: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val account by CloudSync.email.collectAsState()
    val state by CloudSync.state.collectAsState()

    var typedEmail by remember { mutableStateOf<String?>(null) }
    // Which answer we are waiting on, and the trigger that opens the dialog for
    // it. Held in state rather than called directly because the reply from one
    // pass is what starts the next, and a launcher cannot be called from the
    // callback that created it.
    var asking by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }

    // The edge swipe on a watch arrives as a back press. Without this it would
    // leave the app entirely instead of returning to the mic button.
    BackHandler(onBack = onDone)

    val input = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()) { result ->
        val key = asking
        val answer = result.data
            ?.let { data -> key?.let { RemoteInput.getResultsFromIntent(data)?.getCharSequence(it) } }
            ?.toString()?.trim()
        when {
            // Dismissed, or answered with nothing: drop the whole attempt
            // rather than carrying half of it into the next tap.
            key == null || answer.isNullOrEmpty() -> {
                asking = null
                typedEmail = null
            }
            key == ASK_EMAIL -> {
                typedEmail = answer
                asking = ASK_PASSWORD
            }
            else -> {
                val mail = typedEmail
                asking = null
                typedEmail = null
                if (mail != null) {
                    busy = true
                    error = null
                    scope.launch {
                        // allowCreate false: on a watch an unknown address is
                        // a typo, not a new user. Left on, one wrong character
                        // opens an empty account and syncs to it — which is
                        // exactly what happened the first time this shipped.
                        val failure = CloudSync.signIn(
                            ctx, mail, answer, allowCreate = false)
                        busy = false
                        error = failure
                        if (failure == null) {
                            // Anything dictated before the account was right is
                            // queued behind this sign-in; send it now rather
                            // than waiting for the next dictation to carry it.
                            scope.launch { runCatching { CloudSync.syncNow(ctx) } }
                            onDone()
                        }
                    }
                }
            }
        }
    }

    // Wear's substitute for a keyboard: a full-screen system activity offering
    // scribble, the tiny keyboard and voice, which hands the typed string back
    // as an extra. It is the only way to get an arbitrary password onto a watch
    // without pairing a phone, and it does not mask what it collects — worth
    // knowing before typing one in front of anybody.
    LaunchedEffect(asking) {
        val key = asking ?: return@LaunchedEffect
        val label = if (key == ASK_EMAIL) "Email" else "Password"
        val remoteInput = RemoteInput.Builder(key).setLabel(label).build()
        val intent = RemoteInputIntentHelper.putRemoteInputsExtra(
            RemoteInputIntentHelper.createActionRemoteInputIntent(), listOf(remoteInput))
        RemoteInputIntentHelper.putTitleExtra(intent, label)
        input.launch(intent)
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Ink)
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 24.dp, vertical = 32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(text = "Sync", color = Volt, fontSize = 15.sp)
        Spacer(Modifier.height(10.dp))

        val signedIn = account
        when {
            busy -> Text(text = "Signing in…", color = Muted, fontSize = 12.sp)

            signedIn != null && state != "off" -> {
                Text(
                    text = signedIn,
                    color = Chalk,
                    fontSize = 12.sp,
                    textAlign = TextAlign.Center,
                    lineHeight = 15.sp,
                )
                Spacer(Modifier.height(12.dp))
                ActionRow(label = "Sign out") {
                    CloudSync.signOut(ctx)
                    error = null
                }
            }

            else -> {
                Text(
                    text = "Sign in and every note goes straight to your laptop.",
                    color = Muted,
                    fontSize = 11.sp,
                    textAlign = TextAlign.Center,
                    lineHeight = 14.sp,
                )
                Spacer(Modifier.height(12.dp))
                ActionRow(label = "Sign in") { asking = ASK_EMAIL }
            }
        }

        error?.let {
            Spacer(Modifier.height(10.dp))
            Text(
                text = it,
                color = Warn,
                fontSize = 11.sp,
                textAlign = TextAlign.Center,
                lineHeight = 14.sp,
            )
        }

        Spacer(Modifier.height(12.dp))
        ActionRow(label = "Back", colour = Muted, onClick = onDone)
    }
}

// A full-width tap target, because Wear's own Chip is taller than a third of
// this screen and there are three of these on it.
@Composable
private fun ActionRow(label: String, colour: Color = Chalk, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(24.dp))
            .background(Panel)
            .clickable(onClick = onClick)
            .padding(vertical = 11.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text = label, color = colour, fontSize = 13.sp)
    }
}
