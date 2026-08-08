package org.dictationmic.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.core.content.FileProvider
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONArray

// The app's *contents* update themselves — the interface is the hosted web app,
// so a hosting deploy reaches the phone on its next launch with nothing to
// install. This is for the changes that can't ride a deploy: the recorder, a
// permission, a new Android version's rules.
//
// It updates in place. A newer android-v* release on GitHub becomes a
// notification; tapping it downloads the APK straight into the app's cache and
// hands it to Android's installer — one tap to confirm, no browser, no
// Downloads folder. (Same signing key, so it installs over the top and keeps
// everything.)
object Updater {
    private const val RELEASES =
        "https://api.github.com/repos/stevenmcginty/DictationMic/releases"
    // Not the old "updates" channel: importance is baked in at creation and
    // that one was LOW — a silent row folded into the shade, which is why an
    // update could sit there unseen. This one gets a heads-up banner.
    private const val CHANNEL = "app-updates"
    private const val NOTIF_ID = 2
    // Only rate-limits the GitHub call. A release that's already been found is
    // re-announced on every open regardless (see pendingUpdate).
    private const val CHECK_GAP_MS = 300_000L      // 5 minutes

    const val ACTION_INSTALL = "org.dictationmic.android.INSTALL_UPDATE"
    const val EXTRA_APK_URL = "apkUrl"
    const val EXTRA_VERSION = "version"

    data class Release(val version: String, val apkUrl: String?, val pageUrl: String)

    // Called on every resume. Refreshes the release cache (rate-limited), and
    // returns the update that should be offered right now — from the cache, so
    // closing and reopening the app re-offers it instantly instead of going
    // quiet inside the check gap. Also posts the notification as a fallback
    // for when the app isn't open.
    fun pendingUpdate(ctx: Context, currentVersion: String): Release? {
        val p = ctx.getSharedPreferences("app", Context.MODE_PRIVATE)
        val last = p.getLong("lastUpdateCheck", 0L)
        if (System.currentTimeMillis() - last >= CHECK_GAP_MS) {
            p.edit().putLong("lastUpdateCheck", System.currentTimeMillis()).apply()
            latestAndroidRelease()?.let { rel ->
                p.edit()
                    .putString("pendingVersion", rel.version)
                    .putString("pendingApkUrl", rel.apkUrl)
                    .putString("pendingPageUrl", rel.pageUrl)
                    .apply()
            }
        }
        val v = p.getString("pendingVersion", null) ?: return null
        if (!isNewer(v, currentVersion)) return null
        val rel = Release(v, p.getString("pendingApkUrl", null),
            p.getString("pendingPageUrl", "") ?: "")
        notify(ctx, rel)
        return rel
    }

    // Releases are tagged android-v0.3 / windows-latest in the same repo, so
    // "latest" is no use here — walk the list for the Android ones. The APK to
    // install is the release's first .apk asset.
    private fun latestAndroidRelease(): Release? = runCatching {
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
            var apk: String? = null
            val assets = rel.optJSONArray("assets") ?: JSONArray()
            for (j in 0 until assets.length()) {
                val a = assets.optJSONObject(j) ?: continue
                if (a.optString("name", "").endsWith(".apk")) {
                    apk = a.optString("browser_download_url", "").ifBlank { null }
                    break
                }
            }
            return Release(tag.removePrefix("android-v"), apk,
                rel.optString("html_url", ""))
        }
        null
    }.getOrNull()

    // Fetch the APK into cache. GitHub answers the asset URL with a redirect to
    // its CDN; HttpURLConnection follows it (https to https) on its own.
    fun download(ctx: Context, url: String): File? = runCatching {
        val dir = File(ctx.cacheDir, "updates").apply { mkdirs() }
        val f = File(dir, "DictationMic.apk")
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 15000
        conn.readTimeout = 120000
        try {
            if (conn.responseCode != 200) return null
            conn.inputStream.use { input ->
                f.outputStream().use { input.copyTo(it) }
            }
        } finally { conn.disconnect() }
        f
    }.getOrNull()

    // Hand the downloaded APK to the system installer. The FileProvider in the
    // manifest is what makes a file in our private cache readable to it.
    fun install(ctx: Context, apk: File) {
        val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".fileprovider", apk)
        ctx.startActivity(Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                or Intent.FLAG_ACTIVITY_NEW_TASK))
    }

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

    private fun notify(ctx: Context, rel: Release) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.deleteNotificationChannel("updates")    // the old silent channel
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "App updates",
                NotificationManager.IMPORTANCE_HIGH))
        // Tap installs in-app. Only a release with no APK attached — which the
        // ship checklist says should never happen — falls back to the browser.
        val open = if (rel.apkUrl != null) {
            PendingIntent.getActivity(
                ctx, 2,
                Intent(ctx, MainActivity::class.java)
                    .setAction(ACTION_INSTALL)
                    .putExtra(EXTRA_APK_URL, rel.apkUrl)
                    .putExtra(EXTRA_VERSION, rel.version),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        } else {
            PendingIntent.getActivity(
                ctx, 2, Intent(Intent.ACTION_VIEW, Uri.parse(rel.pageUrl)),
                PendingIntent.FLAG_IMMUTABLE)
        }
        val n: Notification = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("DictationMic ${rel.version} is out")
            .setContentText("Tap to install — it updates in place")
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        nm.notify(NOTIF_ID, n)
    }
}
