package org.dictationmic.android

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject

// Files on their way into the web app.
//
// Two things hand files to this app and neither of them is a web page: Android's
// share sheet ("Share > DictationMic" on a screenshot), and the file picker the
// "+ Image" / "+ File" buttons open. Both arrive in Kotlin as a content:// URI,
// and the page can't read one of those — a content URI means nothing to
// JavaScript, and the WebView's own file-chooser plumbing hands the page a File
// whose type and bytes depend on the provider that answered.
//
// So the shell reads the bytes itself, where it has the ContentResolver and can
// ask for the real MIME type, and parks them here. The page drains them through
// the bridge (shellshare.js) and saves them as notes exactly as it does for a
// dropped or pasted file. One pipe for both features: if the share sheet works,
// the picker works.
//
// Bytes live in memory rather than a cache file on purpose. They're read once,
// claimed once, and dropped — a shared screenshot that never reaches the page
// because the app was killed is one the share sheet can simply send again.
object SharedInbox {

    // Both ends of the bridge are bounded: a share of fifty photos is a
    // mistake, and holding a hundred megabytes of them is an OOM kill.
    private const val MAX_ITEMS = 10
    private const val MAX_BYTES = 24 * 1024 * 1024

    // Multiple of 3, so each chunk encodes to base64 with no "=" padding in the
    // middle — the page can join the chunks into one valid base64 string.
    private const val CHUNK_BYTES = 3 * 96 * 1024

    private class Item(
        val id: String,
        val name: String,
        val mime: String,
        val bytes: ByteArray,
    )

    private val items = mutableListOf<Item>()
    private var nextId = 1

    // Set by the page once it has wired its end of this up (shellshare.js).
    //
    // The APK and the web app update separately — an install can arrive before
    // the page it loads has been refreshed — so the shell only takes the file
    // picker over from the WebView once it knows there is something on the
    // other side to hand the bytes to. Until then the WebView's own path is
    // still there, exactly as it was.
    @Volatile
    var pageReady = false

    // Read every URI into memory. Blocking I/O — call it off the main thread.
    // Returns how many were actually readable; 0 means nothing arrived and the
    // caller should say so rather than leaving a silent dead share.
    fun accept(ctx: Context, uris: List<Uri>): Int {
        var taken = 0
        for (uri in uris) {
            if (count() >= MAX_ITEMS) break
            val bytes = runCatching {
                ctx.contentResolver.openInputStream(uri)?.use { input ->
                    val out = java.io.ByteArrayOutputStream()
                    val buf = ByteArray(64 * 1024)
                    var total = 0
                    while (true) {
                        val n = input.read(buf)
                        if (n <= 0) break
                        total += n
                        if (total > MAX_BYTES) return@runCatching null
                        out.write(buf, 0, n)
                    }
                    out.toByteArray()
                }
            }.getOrNull() ?: continue
            if (bytes.isEmpty()) continue

            // The provider's own MIME beats guessing from the name: the photo
            // picker hands back URIs with no extension at all.
            val mime = ctx.contentResolver.getType(uri)
                ?: guessMime(displayName(ctx, uri))
            synchronized(items) {
                items += Item("s${nextId++}", displayName(ctx, uri), mime, bytes)
            }
            taken++
        }
        return taken
    }

    private fun count(): Int = synchronized(items) { items.size }

    fun isEmpty(): Boolean = count() == 0

    // What's waiting, without the bytes — the page asks for those a chunk at a
    // time so a 20 MB share never becomes one enormous string.
    fun list(): String = synchronized(items) {
        JSONArray().apply {
            for (it in items) put(JSONObject().apply {
                put("id", it.id)
                put("name", it.name)
                put("mime", it.mime)
                put("size", it.bytes.size)
            })
        }.toString()
    }

    // Base64 of chunk `index`. "" once the item is exhausted or gone.
    fun chunk(id: String, index: Int): String {
        val item = synchronized(items) { items.firstOrNull { it.id == id } } ?: return ""
        val start = index.toLong() * CHUNK_BYTES
        if (index < 0 || start >= item.bytes.size) return ""
        val offset = start.toInt()
        val len = minOf(CHUNK_BYTES, item.bytes.size - offset)
        return Base64.encodeToString(item.bytes, offset, len, Base64.NO_WRAP)
    }

    // The page has it — let the memory go. Claiming is what stops a reload
    // mid-drain from saving the same screenshot twice.
    fun clear(id: String) {
        synchronized(items) { items.removeAll { it.id == id } }
    }

    private fun displayName(ctx: Context, uri: Uri): String {
        val fromProvider = runCatching {
            ctx.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME),
                null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0) else null
            }
        }.getOrNull()
        return fromProvider ?: uri.lastPathSegment ?: "shared"
    }

    private fun guessMime(name: String): String = when {
        name.endsWith(".png", true) -> "image/png"
        name.endsWith(".gif", true) -> "image/gif"
        name.endsWith(".webp", true) -> "image/webp"
        name.endsWith(".jpg", true) || name.endsWith(".jpeg", true) -> "image/jpeg"
        else -> "application/octet-stream"
    }
}
