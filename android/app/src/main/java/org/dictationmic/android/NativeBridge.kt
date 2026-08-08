package org.dictationmic.android

import android.content.Context
import android.webkit.JavascriptInterface
import org.json.JSONObject

// The seam between the hosted app and the phone.
//
// Everything the app *is* — notes, list, editor, search, sign-in, sync — is the
// same web app the browser loads, so the two can never drift apart and a
// hosting deploy updates this app too. What lives here is only what a web page
// on Android cannot do: keep the microphone after the screen goes off, speak
// over the headphones, and be started by voice.
//
// Exposed to one origin only (see MainActivity.TRUSTED_ORIGIN) — an injected
// interface is reachable by any script on the page, so it must never be
// attached to a document we don't serve.
class NativeBridge(
    private val ctx: Context,
    private val versionName: String,
    private val onAccount: () -> Unit,
    private val onStartDictation: (Boolean) -> Unit,
) {
    // Lets the web app tell a shell launch from a browser tab and switch the
    // mic screen over to the native recorder.
    @JavascriptInterface
    fun isShell(): Boolean = true

    @JavascriptInterface
    fun version(): String = versionName

    // Sign-in stays in the web app — one screen, one account, one place the
    // password is handled. It hands the minted refresh token down so the
    // recorder can upload notes on its own while the page is asleep.
    @JavascriptInterface
    fun setAccount(email: String, refreshToken: String, uid: String) {
        if (refreshToken.isBlank() || uid.isBlank()) return
        val p = ctx.getSharedPreferences("sync", Context.MODE_PRIVATE)
        if (p.getString("refreshToken", null) == refreshToken &&
            p.getString("uid", null) == uid) return          // nothing changed
        p.edit()
            .putString("email", email)
            .putString("refreshToken", refreshToken)
            .putString("uid", uid)
            .apply()
        CloudSync.loadAccount(ctx)
        onAccount()
    }

    @JavascriptInterface
    fun signOut() {
        if (DictationState.running.value) DictationService.stop(ctx)
        CloudSync.signOut(ctx)
    }

    // Routed through the activity rather than starting the service here: the
    // activity owns the mic-permission dialog, and starting the microphone
    // service without the permission crashes the app on Android 14+ instead
    // of refusing.
    @JavascriptInterface
    fun startDictation(handsFree: Boolean) {
        if (DictationState.running.value) return
        onStartDictation(handsFree)
    }

    @JavascriptInterface
    fun stopDictation() {
        if (!DictationState.running.value) return
        DictationService.stop(ctx)
    }

    @JavascriptInterface
    fun speak(text: String) {
        Speaker.init(ctx)
        Speaker.speak(text)
    }

    // A snapshot the page can ask for at any time — on load, on returning to
    // the foreground, or after a WebView reload — so the mic screen redraws
    // itself correctly against a session that has been running without it.
    @JavascriptInterface
    fun dictationState(): String = JSONObject().apply {
        put("running", DictationState.running.value)
        put("finals", DictationState.finals.value)
        put("partial", DictationState.partial.value)
        put("level", DictationState.level.value.toDouble())
        put("savedAt", DictationState.savedNoteAt.value)
    }.toString()

    // True when something is plugged in or paired — the signal that Steve is
    // wearing the buds and a launch should start dictating on its own.
    @JavascriptInterface
    fun headphonesConnected(): Boolean = AudioRoute.headphonesConnected(ctx)

    // ---- files handed to the app from outside the page ----------------------
    //
    // A screenshot shared in from Android's share sheet, or a photo picked with
    // "+ Image". Both reach the shell as a content:// URI that JavaScript can't
    // open, so SharedInbox reads the bytes and the page pulls them across here.
    // See shellshare.js for the other half.

    // The page announcing it can drain the inbox. Until this lands the shell
    // leaves the file picker to the WebView — see SharedInbox.pageReady.
    @JavascriptInterface
    fun sharedReady() {
        SharedInbox.pageReady = true
    }

    @JavascriptInterface
    fun sharedFiles(): String = SharedInbox.list()

    @JavascriptInterface
    fun sharedChunk(id: String, index: Int): String = SharedInbox.chunk(id, index)

    @JavascriptInterface
    fun clearShared(id: String) = SharedInbox.clear(id)
}
