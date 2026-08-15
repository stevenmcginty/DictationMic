package org.dictationmic.android

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.webkit.JavascriptInterface
import android.widget.Toast
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File

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

    // ---- files handed the other way, out of the page to the phone -----------
    //
    // Open on a PDF used to do nothing at all: no viewer, no error, not even a
    // toast. The page had made a blob: URL and asked for a new window, and a
    // blob: URL is a thing that exists only inside the document that made it —
    // the shell dutifully offered it to Android, Android has never heard of the
    // scheme, and the tap died in the gap between them.
    //
    // So the bytes come across instead, the mirror image of the inbound route
    // above: base64, a chunk a call, joined here. They land in a cache file the
    // FileProvider can serve and Android opens it properly — real content://
    // URI, real MIME type, real filename, and whatever app Steve already opens
    // that kind of file with. See the handoff at the top of ui.js for the other
    // half, and note it checks for these methods by name: an APK can be older
    // than the page it loads, so the bridge existing proves nothing.

    // The page's own cap on a file note is 7 MB, so this only ever stops a
    // runaway — but it stops it before it becomes an OOM kill, the same reason
    // SharedInbox bounds the inbound side.
    private val maxOpenBytes = 24 * 1024 * 1024

    private val staged = ByteArrayOutputStream()
    private var staging = false

    // Index 0 starts a fresh file, so a push abandoned half way — an unreadable
    // note, a reload mid-send — can't leave its bytes stuck on the front of the
    // next one. Anything that doesn't decode, or that runs over the cap, drops
    // the whole staging: half a PDF is worse than no PDF, and openFile below
    // reports the failure honestly instead.
    @JavascriptInterface
    fun openFileChunk(index: Int, b64: String) {
        synchronized(staged) {
            if (index == 0) { staged.reset(); staging = true }
            if (!staging) return
            val part = runCatching { Base64.decode(b64, Base64.DEFAULT) }.getOrNull()
            if (part == null || staged.size() + part.size > maxOpenBytes) {
                staged.reset()
                staging = false
                return
            }
            staged.write(part)
        }
    }

    // False means the shell couldn't take the file at all and the page should
    // say so itself. Everything past that point — no app on this phone opens a
    // .xlsx, a viewer that refuses it — answers true and is explained here,
    // because only this side knows what happened.
    //
    // `share` picks Android's share sheet over its viewer. Open wants the
    // viewer; Share and Download want the sheet, since a phone has no downloads
    // folder a web page can reach and "send it to Files, or Drive" is what that
    // button is really being asked for.
    @JavascriptInterface
    fun openFile(name: String, mime: String, share: Boolean): Boolean {
        val bytes = synchronized(staged) {
            val b = if (staging) staged.toByteArray() else ByteArray(0)
            staged.reset()
            staging = false
            b
        }
        if (bytes.isEmpty()) return false
        val safe = safeName(name)
        val type = mime.ifBlank { "application/octet-stream" }
        val uri = runCatching {
            val dir = File(ctx.cacheDir, "open").apply { mkdirs() }
            // One at a time. The last file opened is of no use to anyone once
            // its viewer has been closed, and a cache that only ever grows is
            // the kind of thing that gets an app uninstalled.
            dir.listFiles()?.forEach { it.delete() }
            val f = File(dir, safe)
            f.writeBytes(bytes)
            FileProvider.getUriForFile(ctx, ctx.packageName + ".fileprovider", f)
        }.getOrNull() ?: return false

        // Bridge calls arrive on a WebView thread, and a Toast there is a crash
        // rather than a message — so the launch happens on the main thread,
        // which also means the answer above is "staged it", not "opened it".
        Handler(Looper.getMainLooper()).post {
            val intent = if (share) {
                Intent.createChooser(
                    Intent(Intent.ACTION_SEND)
                        .setType(type)
                        .putExtra(Intent.EXTRA_STREAM, uri)
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION),
                    null)
            } else {
                Intent(Intent.ACTION_VIEW).setDataAndType(uri, type)
            }
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                or Intent.FLAG_ACTIVITY_NEW_TASK)
            try {
                ctx.startActivity(intent)
            } catch (e: ActivityNotFoundException) {
                // The one failure that isn't a bug — Steve simply has no app
                // for this kind of file. Say which kind, or the button looks
                // broken again.
                val ext = safe.substringAfterLast('.', "")
                Toast.makeText(ctx,
                    if (ext.isBlank()) "Nothing on this phone opens that file"
                    else "Nothing on this phone opens a .$ext",
                    Toast.LENGTH_LONG).show()
            }
        }
        return true
    }

    // The filename is the page's, and it decides what the viewer thinks it has
    // been handed — so the extension is kept and only the parts that would name
    // a different file are taken out. "..", after all that, is still a
    // directory rather than a name.
    private fun safeName(name: String): String {
        val base = name.substringAfterLast('/').substringAfterLast('\\')
            .replace(Regex("[^A-Za-z0-9._ -]"), "_")
            .trim()
        return if (base.isBlank() || base.all { it == '.' }) "file" else base
    }
}
