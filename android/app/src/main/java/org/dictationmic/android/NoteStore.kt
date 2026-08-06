package org.dictationmic.android

import android.content.Context
import java.io.File
import java.util.UUID
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONObject

data class Note(
    val id: String,
    var title: String,
    var body: String,
    var createdAt: Long,
    var updatedAt: Long,      // local edit time (ms) — LWW tiebreaker
    var dirty: Boolean,       // needs pushing
    var syncedRev: Long,      // cloud updatedAt of our last push / adopt
    var deleted: Boolean,
)

// One JSON file per note in filesDir/notes — the files plus dirty flags ARE
// the retry queue, exactly like the desktop's notestore. Deleted notes stay
// on disk as tombstones (hidden from the list) so cloud echoes are ignored.
object NoteStore {
    val changed = MutableStateFlow(0L)   // bumped on every mutation

    private fun dir(ctx: Context) = File(ctx.filesDir, "notes").apply { mkdirs() }
    private fun file(ctx: Context, id: String) = File(dir(ctx), "$id.json")

    fun newId(): String = UUID.randomUUID().toString().replace("-", "")

    // JS port of the desktop's note_title_from
    fun titleFrom(text: String): String {
        val words = text.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }
        var t = words.take(7).joinToString(" ")
            .replace(Regex("[<>:\"/\\\\|?*\\x00-\\x1f]+"), " ")
            .replace(Regex("\\s{2,}"), " ")
            .trim(' ', '.')
        t = t.take(60).trimEnd(' ', '.', ',', '!', '?', ';', ':')
        return t.ifBlank { "Note" }
    }

    fun all(ctx: Context): List<Note> =
        (dir(ctx).listFiles { f -> f.name.endsWith(".json") } ?: emptyArray())
            .mapNotNull { runCatching { fromJson(it.readText()) }.getOrNull() }
            .filter { !it.deleted }
            .sortedByDescending { it.createdAt }

    fun get(ctx: Context, id: String): Note? {
        val f = file(ctx, id)
        if (!f.exists()) return null
        return runCatching { fromJson(f.readText()) }.getOrNull()
    }

    fun dirtyNotes(ctx: Context): List<Note> =
        (dir(ctx).listFiles { f -> f.name.endsWith(".json") } ?: emptyArray())
            .mapNotNull { runCatching { fromJson(it.readText()) }.getOrNull() }
            .filter { it.dirty }

    fun saveNew(ctx: Context, body: String): Note {
        val now = System.currentTimeMillis()
        val note = Note(
            id = newId(), title = titleFrom(body), body = body,
            createdAt = now, updatedAt = now,
            dirty = true, syncedRev = 0L, deleted = false,
        )
        write(ctx, note)
        return note
    }

    fun update(ctx: Context, note: Note) {
        note.updatedAt = System.currentTimeMillis()
        note.dirty = true
        write(ctx, note)
    }

    // The live-dictation path: rewrite a note's text in place as more phrases
    // arrive. The title is recomputed each time so it settles on the finished
    // wording rather than sticking at whatever the first phrase happened to be.
    // A no-op when the text hasn't moved, so a re-push never bumps updatedAt.
    fun updateText(ctx: Context, id: String, body: String): Note? {
        val n = get(ctx, id) ?: return null
        if (n.body == body) return n
        n.body = body
        n.title = titleFrom(body)
        n.updatedAt = System.currentTimeMillis()
        n.dirty = true
        write(ctx, n)
        return n
    }

    fun delete(ctx: Context, id: String) {
        val n = get(ctx, id) ?: return
        n.deleted = true
        n.updatedAt = System.currentTimeMillis()
        n.dirty = true
        write(ctx, n)
    }

    // pushedUpdatedAt is the note's local edit stamp as it was when the push
    // started. If it has moved on since, the text changed while the request was
    // in flight, so record the revision but leave the note dirty — the newer
    // wording still has to go up. Live dictation pushes the same note over and
    // over, which makes that race the normal case rather than a corner one.
    fun markSynced(ctx: Context, id: String, rev: Long, pushedUpdatedAt: Long = -1L) {
        val n = get(ctx, id) ?: return
        val stale = pushedUpdatedAt >= 0 && n.updatedAt > pushedUpdatedAt
        if (!stale) n.dirty = false
        n.syncedRev = rev
        write(ctx, n, bump = false)
    }

    // A cloud record won: adopt it. rev is the cloud updatedAt.
    fun applyRemote(ctx: Context, id: String, title: String, body: String,
                    createdAt: Long, rev: Long, deleted: Boolean) {
        val existing = get(ctx, id)
        val n = existing ?: Note(id, "", "", createdAt, 0L, false, 0L, false)
        n.title = title
        n.body = body
        n.createdAt = if (createdAt > 0) createdAt else n.createdAt
        n.updatedAt = rev
        n.dirty = false
        n.syncedRev = rev
        n.deleted = deleted
        write(ctx, n)
    }

    private fun write(ctx: Context, n: Note, bump: Boolean = true) {
        file(ctx, n.id).writeText(toJson(n))
        if (bump) changed.value = System.currentTimeMillis()
    }

    private fun toJson(n: Note): String = JSONObject().apply {
        put("id", n.id); put("title", n.title); put("body", n.body)
        put("createdAt", n.createdAt); put("updatedAt", n.updatedAt)
        put("dirty", n.dirty); put("syncedRev", n.syncedRev)
        put("deleted", n.deleted)
    }.toString()

    private fun fromJson(s: String): Note {
        val j = JSONObject(s)
        return Note(
            id = j.getString("id"),
            title = j.optString("title", "Note"),
            body = j.optString("body", ""),
            createdAt = j.optLong("createdAt"),
            updatedAt = j.optLong("updatedAt"),
            dirty = j.optBoolean("dirty"),
            syncedRev = j.optLong("syncedRev"),
            deleted = j.optBoolean("deleted"),
        )
    }
}
