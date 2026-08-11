package org.dictationmic.android

import android.content.Context
import android.util.Log
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject

// Same wire protocol as the desktop's cloudsync.py and the PWA's sync.js:
// Firebase email/password REST auth + the Realtime Database REST API at
// users/{uid}/notes. No Firebase SDK, no google-services.json.
object CloudSync {
    private const val API_KEY = "AIzaSyDmofv0p1-90ccEdsruYHQoTqDs5WpYQHU"
    private const val DB_URL =
        "https://dictationmic-sync-default-rtdb.europe-west1.firebasedatabase.app"
    private const val SIGNIN =
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=$API_KEY"
    private const val SIGNUP =
        "https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=$API_KEY"
    private const val REFRESH =
        "https://securetoken.googleapis.com/v1/token?key=$API_KEY"

    // off | ok | offline | needs-signin | syncing
    val state = MutableStateFlow("off")
    val email = MutableStateFlow<String?>(null)

    @Volatile private var idToken: String? = null
    @Volatile private var tokenExp = 0L
    private val mutex = Mutex()          // a full sync: push then pull
    // Any outbound PATCH. Separate from `mutex` on purpose: a live dictation
    // push must not have to wait behind a full snapshot download.
    private val pushMutex = Mutex()
    private val tokenLock = Any()

    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences("sync", Context.MODE_PRIVATE)

    fun loadAccount(ctx: Context) {
        val p = prefs(ctx)
        email.value = p.getString("email", null)
        state.value = if (p.getString("refreshToken", null) != null) "ok" else "off"
    }

    fun signOut(ctx: Context) {
        prefs(ctx).edit().clear().apply()
        idToken = null
        email.value = null
        state.value = "off"
    }

    // allowCreate is what the phone does: an unknown email means a new account,
    // because that is also how you sign up. The watch passes false, and it has
    // to. There, the address is thumbed in on an inch of glass with no second
    // field to check it against, and a single wrong character used to open a
    // brand-new empty account and sync to it perfectly happily — every note
    // saved, nothing ever arriving, and no way to tell from the wrist.
    suspend fun signIn(
        ctx: Context,
        mail: String,
        password: String,
        allowCreate: Boolean = true,
    ): String? =
        withContext(Dispatchers.IO) {
            val body = JSONObject()
                .put("email", mail).put("password", password)
                .put("returnSecureToken", true).toString()
            var (code, resp) = post(SIGNIN, body)
            if (code != 200) {
                val err = errOf(resp)
                if (err.startsWith("EMAIL_NOT_FOUND") ||
                    err.startsWith("INVALID_LOGIN_CREDENTIALS")) {
                    // Firebase folds "no such account" and "wrong password"
                    // into one error, so without signing up we can't tell them
                    // apart — say both, since on a watch either one is nearly
                    // always a mistyped character rather than a new account.
                    if (!allowCreate) {
                        return@withContext "No account with that email, or wrong password"
                    }
                    val r2 = post(SIGNUP, body)
                    code = r2.first; resp = r2.second
                    if (code != 200) {
                        val e2 = errOf(resp)
                        return@withContext if (e2.startsWith("EMAIL_EXISTS"))
                            "Wrong password for that account" else friendly(e2)
                    }
                } else return@withContext friendly(err)
            }
            val j = JSONObject(resp)
            // Deliberately not re-queueing the local notes on an account
            // switch. Most of what a device holds was pulled down rather than
            // dictated on it, so re-pushing the lot would stamp every one of
            // them with a fresh cloud timestamp and reshuffle the note list on
            // every other device. Signing into the wrong account is prevented
            // up front instead.
            prefs(ctx).edit()
                .putString("email", mail)
                .putString("refreshToken", j.getString("refreshToken"))
                .putString("uid", j.getString("localId"))
                .apply()
            idToken = j.getString("idToken")
            tokenExp = System.currentTimeMillis() +
                (j.optString("expiresIn", "3600").toLong() - 300) * 1000
            email.value = mail
            state.value = "ok"
            null                                  // null = success
        }

    private fun friendly(err: String): String = when {
        "WEAK_PASSWORD" in err -> "Password needs at least 6 characters"
        "INVALID_EMAIL" in err -> "That email doesn't look right"
        "TOO_MANY_ATTEMPTS" in err -> "Too many tries — wait a minute"
        else -> "Sign-in failed" + if (err.isNotEmpty()) " ($err)" else ""
    }

    private fun errOf(resp: String): String =
        runCatching { JSONObject(resp).getJSONObject("error").getString("message") }
            .getOrDefault("")

    // Blocking on purpose — every caller is already on Dispatchers.IO. The lock
    // stops a live push and a background sync minting two tokens at once and
    // clobbering each other's rotated refresh token.
    private fun token(ctx: Context): String? {
        synchronized(tokenLock) {
            if (idToken != null && System.currentTimeMillis() < tokenExp) return idToken
            val rt = prefs(ctx).getString("refreshToken", null) ?: return null
            val (code, resp) = post(
                REFRESH,
                "grant_type=refresh_token&refresh_token=" + URLEncoder.encode(rt, "UTF-8"),
                form = true,
            )
            if (code != 200) return null
            val j = JSONObject(resp)
            idToken = j.getString("id_token")
            tokenExp = System.currentTimeMillis() +
                (j.optString("expires_in", "3600").toLong() - 300) * 1000
            val newRt = j.optString("refresh_token", "")
            if (newRt.isNotEmpty() && newRt != rt) {
                prefs(ctx).edit().putString("refreshToken", newRt).apply()
            }
            return idToken
        }
    }

    // Mint the ID token ahead of time — called when a dictation starts, so the
    // first upload isn't paying for a round trip to Google (DNS + TLS on a cold
    // radio is routinely seconds) before the note even begins going up.
    // Failure doesn't matter here: the push will try again on its own.
    suspend fun warmToken(ctx: Context) {
        if (!enabled) return
        withContext(Dispatchers.IO) { runCatching { token(ctx) } }
    }

    private fun notesUrl(ctx: Context, token: String, noteId: String? = null): String {
        val uid = prefs(ctx).getString("uid", "") ?: ""
        val path = "/users/$uid/notes" + if (noteId != null) "/$noteId" else ""
        return "$DB_URL$path.json?auth=$token"
    }

    val enabled: Boolean get() = state.value != "off"

    // Push everything dirty, then pull the snapshot and reconcile (LWW,
    // echo-guarded by syncedRev — mirrors cloudsync.py's rules).
    // firstId jumps one note to the head of the push queue — the one just
    // dictated, so it never waits behind an older backlog.
    suspend fun syncNow(ctx: Context, firstId: String? = null) = withContext(Dispatchers.IO) {
        if (!enabled) return@withContext
        mutex.withLock {
            state.value = "syncing"
            try {
                val tok = token(ctx)
                if (tok == null) { state.value = "needs-signin"; return@withLock }
                pushDirty(ctx, tok, firstId)
                pullAll(ctx, tok)
                state.value = "ok"
            } catch (ex: Exception) {
                // Swallowed for the user's sake — a failed sync retries and
                // nothing is lost — but silent in the log meant a watch that
                // never delivered looked exactly like a watch with nothing to
                // deliver.
                Log.i(DictationService.LOG, "sync failed: $ex")
                state.value = "offline"
            }
        }
    }

    // Fast path for a live dictation: this note, one PATCH, no pull. The
    // backlog and the snapshot download can wait — what matters here is the
    // laptop seeing the words within a beat of them being spoken.
    suspend fun pushNote(ctx: Context, id: String): Boolean = withContext(Dispatchers.IO) {
        if (!enabled) return@withContext false
        pushMutex.withLock {
            val n = NoteStore.get(ctx, id) ?: return@withLock false
            if (!n.dirty) return@withLock true          // already up there
            try {
                val tok = token(ctx)
                if (tok == null) {
                    Log.i(DictationService.LOG, "push: no token — signed out or refresh refused")
                    state.value = "needs-signin"
                    return@withLock false
                }
                pushOne(ctx, tok, n)
                state.value = "ok"
                true
            } catch (ex: Exception) {
                Log.i(DictationService.LOG, "push of ${n.id} failed: $ex")
                state.value = "offline"
                false
            }
        }
    }

    private suspend fun pushDirty(ctx: Context, tok: String, firstId: String? = null) {
        // listFiles order is arbitrary, so without the sort a note just
        // dictated can end up queued behind every older unsynced one.
        val queue = NoteStore.dirtyNotes(ctx).sortedByDescending {
            if (it.id == firstId) Long.MAX_VALUE else it.updatedAt
        }
        pushMutex.withLock {
            for (n in queue) {
                // re-read: a live push may have sent this one already
                val fresh = NoteStore.get(ctx, n.id) ?: continue
                if (!fresh.dirty) continue
                pushOne(ctx, tok, fresh)
            }
        }
    }

    // One note, one PATCH. Always call with a note freshly read from disk —
    // that plus the markSynced stamp is what keeps repeated live pushes of the
    // same note from losing an edit made mid-request.
    private fun pushOne(ctx: Context, tok: String, n: Note) {
        val startedAt = n.updatedAt
        val payload = JSONObject().apply {
            if (n.deleted) {
                put("deleted", true); put("body", JSONObject.NULL)
                put("origin", "phone")
            } else {
                put("title", n.title); put("body", n.body)
                put("createdAt", n.createdAt); put("deleted", false)
                put("origin", "phone")
            }
            put("updatedAt", JSONObject().put(".sv", "timestamp"))
        }
        val (code, resp) = patch(notesUrl(ctx, tok, n.id), payload.toString())
        if (code != 200) throw RuntimeException("push ${n.id}: HTTP $code")
        val rev = JSONObject(resp).optLong("updatedAt", System.currentTimeMillis())
        NoteStore.markSynced(ctx, n.id, rev, startedAt)
    }

    private fun pullAll(ctx: Context, tok: String) {
        val (code, resp) = get(notesUrl(ctx, tok))
        if (code != 200) throw RuntimeException("pull: HTTP $code")
        if (resp == "null" || resp.isBlank()) return
        val snapshot = JSONObject(resp)
        for (id in snapshot.keys()) {
            val rec = snapshot.optJSONObject(id) ?: continue
            val rev = rec.optLong("updatedAt", 0L)
            val local = NoteStore.get(ctx, id)
            if (local != null && rev <= local.syncedRev) continue   // our own echo
            if (local != null && local.dirty && !rec.optBoolean("deleted")
                && local.updatedAt >= rev) continue                 // our edit is newer
            if (rec.optBoolean("deleted")) {
                if (local != null && !local.deleted) {
                    NoteStore.applyRemote(ctx, id, local.title, "", local.createdAt,
                        rev, deleted = true)
                }
                continue
            }
            // a phone voice note the laptop hasn't transcribed yet has no body
            val body = if (rec.isNull("body")) "" else rec.optString("body", "")
            val title = rec.optString("title", "Note")
            NoteStore.applyRemote(ctx, id, title, body,
                rec.optLong("createdAt", 0L), rev, deleted = false)
        }
    }

    // ---- tiny HTTP helpers -------------------------------------------------

    private fun post(url: String, body: String, form: Boolean = false) =
        request("POST", url, body,
            if (form) "application/x-www-form-urlencoded" else "application/json")

    private fun patch(url: String, body: String) =
        request("PATCH", url, body, "application/json")

    private fun get(url: String) = request("GET", url, null, null)

    private fun request(method: String, url: String, body: String?,
                        contentType: String?): Pair<Int, String> {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15000
        conn.readTimeout = 30000
        // HttpURLConnection has no native PATCH — the RTDB honours the
        // X-HTTP-Method-Override header for it.
        if (method == "PATCH") {
            conn.requestMethod = "POST"
            conn.setRequestProperty("X-HTTP-Method-Override", "PATCH")
        } else {
            conn.requestMethod = method
        }
        if (contentType != null) conn.setRequestProperty("Content-Type", contentType)
        try {
            if (body != null) {
                conn.doOutput = true
                conn.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = conn.responseCode
            val stream = if (code / 100 == 2) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.readText() ?: ""
            return code to text
        } finally {
            conn.disconnect()
        }
    }
}
