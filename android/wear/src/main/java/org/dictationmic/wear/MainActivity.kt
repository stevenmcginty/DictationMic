package org.dictationmic.wear

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.TimeText
import androidx.wear.compose.material.TimeTextDefaults
import org.dictationmic.android.CloudSync

// The watch app is one screen: raise the wrist, tap, talk, and the words are on
// the laptop before the arm is back down. The note file and the upload are the
// phone app's, shared verbatim from :core, so the watch is a standalone client
// of the same account rather than a remote control for a phone in another room.
// The transcription is the watch's own voice input — see DictationScreen for
// why it cannot be the engine the phone uses.
class MainActivity : ComponentActivity() {

    // Set when something outside the app — the tile, a shortcut — has already
    // decided that this launch is a dictation, so the screen skips its own
    // button and goes straight to the voice input.
    private var dictateRequest by mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Read before the first frame is drawn. The account is what the status
        // line under the mic button reports, and a screen that opens saying
        // "sign in to sync" when you already are sends you hunting for a phone
        // this build exists to do without.
        CloudSync.loadAccount(applicationContext)

        dictateRequest = intent?.getBooleanExtra(EXTRA_DICTATE, false) == true

        // No keep-screen-on any more: the talking now happens on the system's
        // own voice screen, which manages its own timeout, and this activity is
        // only ever a button and a receipt.
        setContent {
            WatchApp(
                dictateRequest = dictateRequest,
                onDictateHandled = { dictateRequest = false },
            )
        }
    }

    // launchMode is singleTask, so the second and every later tap on the tile
    // arrives here rather than in onCreate. Without this the tile works once
    // per app lifetime and then silently becomes a plain launcher icon.
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.getBooleanExtra(EXTRA_DICTATE, false)) dictateRequest = true
    }

    companion object {
        const val EXTRA_DICTATE = "dictate"
    }
}

@Composable
fun WatchApp(
    dictateRequest: Boolean = false,
    onDictateHandled: () -> Unit = {},
) {
    // Two screens and no navigation library: the account screen is a one-time
    // errand, and a whole nav graph to reach it would be more code than the
    // screen itself.
    var showAccount by remember { mutableStateOf(false) }

    MaterialTheme(colors = WatchColors) {
        Scaffold(
            modifier = Modifier.fillMaxSize(),
            timeText = {
                TimeText(timeTextStyle = TimeTextDefaults.timeTextStyle(color = Muted))
            },
        ) {
            if (showAccount) {
                AccountScreen(onDone = { showAccount = false })
            } else {
                DictationScreen(
                    onAccount = { showAccount = true },
                    dictateRequest = dictateRequest,
                    onDictateHandled = onDictateHandled,
                )
            }
        }
    }
}
