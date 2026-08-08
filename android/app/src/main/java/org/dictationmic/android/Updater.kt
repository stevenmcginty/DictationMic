package org.dictationmic.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.app.NotificationCompat
import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONArray

// The app's *contents* update themselves — the interface is the hosted web app,
// so a hosting deploy reaches the phone on its next launch with nothing to
// install. This is only for the rare change that can't be made in the web app:
// a new permission, a change to the recorder, a new Android version's rules.
//
// It never installs anything by itself. It notices a newer release on GitHub
// and posts a notification; tapping it opens the release page in the browser.
object Updater {
    private const val RELEASES =
        "https://api.github.com/repos/stevenmcginty/DictationMic/releases"
    private const val CHANNEL = "updates"
    private const val NOTIF_ID = 2
    private const val CHECK_GAP_MS = 21_600_000L      // 6 hours

    fun checkQuietly(ctx: Context, currentVersion: String) {
        val p = ctx.getSharedPreferences("app", Context.MODE_PRIVATE)
        val last = p.getLong("lastUpdateCheck", 0L)
        if (System.currentTimeMillis() - last < CHECK_GAP_MS) return
        p.edit().putLong("lastUpdateCheck", System.currentTimeMillis()).apply()

        val (tag, url) = latestAndroidRelease() ?: return
        val version = tag.removePrefix("android-v")
        if (!isNewer(version, currentVersion)) return
        notify(ctx, version, url)
    }

    // Releases are tagged android-v0.3 / windows-latest in the same repo, so
    // "latest" is no use here — walk the list for the Android ones.
    private fun latestAndroidRelease(): Pair<String, String>? = runCatching {
        val conn = URL(RELEASES).openConnection() as HttpURLConnection
        conn.connectTimeout = 10000
        conn.readTimeout = 15000
        conn.setRequestProperty("Accept", "application/vnd.github+json")
        val body = try {
            if (conn.responseCode != 200) return null
            conn.inputStream.bufferedReader().readText()
        } finally { conn.disconnect() }
        val all = JSONArray(body)
        for (i in 0 until all.length()) {
            val rel = all.optJSONObject(i) ?: continue
            if (rel.optBoolean("draft") || rel.optBoolean("prerelease")) continue
            val tag = rel.optString("tag_name", "")
            if (!tag.startsWith("android-v")) continue
            return tag to rel.optString("html_url", "")
        }
        null
    }.getOrNull()

    // Plain dotted numbers, compared piecewise. Anything unparseable is treated
    // as "not newer" — a bad tag must never nag.
    private fun isNewer(candidate: String, current: String): Boolean {
        val a = candidate.split(".").map { it.toIntOrNull() ?: return false }
        val b = current.split(".").map { it.toIntOrNull() ?: return false }
        for (i in 0 until maxOf(a.size, b.size)) {
            val x = a.getOrElse(i) { 0 }
            val y = b.getOrElse(i) { 0 }
            if (x != y) return x > y
        }
        return false
    }

    private fun notify(ctx: Context, version: String, url: String) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "App updates",
                NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(
            ctx, 2, Intent(Intent.ACTION_VIEW, Uri.parse(url)),
            PendingIntent.FLAG_IMMUTABLE)
        val n: Notification = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("DictationMic $version is out")
            .setContentText("Tap to download the new app")
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        nm.notify(NOTIF_ID, n)
    }
}
