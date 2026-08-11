package org.dictationmic.wear

import android.app.Application
import org.dictationmic.android.DictationService

class WatchApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        // DictationService is shared with the phone, which dictates offline
        // and has no radio to hurry — the hook stays null there. Here, a
        // running session is the strongest possible signal that the network
        // is about to matter: the recogniser streams over it and the note
        // uploads live. Hold LTE up for the whole session, even when the
        // screen has gone off and the activity with it.
        DictationService.networkHold = { on ->
            if (on) NetworkBoost.hold(this, "dictation")
            else NetworkBoost.release("dictation")
        }
        // Catch the walk-away the moment a link drops, not when the routing
        // layer finally notices. The process is alive whenever the watch face
        // is ours, which is always.
        WalkAwayGuard.start(this)
    }
}
