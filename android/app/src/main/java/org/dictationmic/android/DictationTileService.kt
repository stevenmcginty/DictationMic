package org.dictationmic.android

import android.app.PendingIntent
import android.content.Intent
import android.graphics.drawable.Icon
import android.net.Uri
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

// A Quick Settings tile: start and stop dictation from the shade, including
// from the lock screen, without unlocking the phone.
//
// This exists because an assistant launch can't avoid the fingerprint. Asking
// an assistant to open an app on a locked device is a system-level unlock,
// not something the app can waive — showWhenLocked only governs how the window
// is drawn once the system has agreed to launch it. A toggleable tile is the
// one lock-screen entry point that belongs to the app, so it is the only way
// in that stays hands-free with the phone locked in a pocket.
class DictationTileService : TileService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var watcher: Job? = null

    override fun onStartListening() {
        render()
        // The session can end on its own — a stop phrase, the silence ceiling —
        // while the shade is still open, so the tile follows the service rather
        // than only its own clicks.
        watcher = scope.launch {
            DictationState.running.collectLatest { render() }
        }
    }

    override fun onStopListening() {
        watcher?.cancel()
        watcher = null
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    override fun onClick() {
        if (DictationState.running.value) {
            // Stopping never needs a screen: the service saves and uploads the
            // note by itself.
            DictationService.stop(applicationContext)
            render()
            return
        }
        if (isLocked) {
            // Deliberately not unlockAndRun: prompting for the fingerprint here
            // would reintroduce exactly the barrier this tile exists to remove.
            // The service is self-contained and speaks its own status, so a
            // locked start needs nothing from the activity.
            DictationService.start(applicationContext, handsFree = true)
            render()
            return
        }
        // Unlocked, so the mic screen is worth showing — the activity starts the
        // same session and routes the page to it.
        val intent = Intent(this, MainActivity::class.java)
            .setAction(Intent.ACTION_VIEW)
            .setData(Uri.parse("dictationmic://dictate?dictate=1"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) {
            // The Intent overload throws from Android 14 on.
            startActivityAndCollapse(
                PendingIntent.getActivity(
                    this, 0, intent,
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(intent)
        }
    }

    private fun render() {
        val tile = qsTile ?: return
        val on = DictationState.running.value
        tile.state = if (on) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        tile.label = getString(R.string.tile_label)
        tile.icon = Icon.createWithResource(this, R.drawable.ic_tile_mic)
        if (Build.VERSION.SDK_INT >= 29) {
            tile.subtitle = if (on) "Recording" else "Tap to dictate"
        }
        tile.updateTile()
    }
}
