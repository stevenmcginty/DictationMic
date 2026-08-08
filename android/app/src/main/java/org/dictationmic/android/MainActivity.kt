package org.dictationmic.android

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.content.IntentCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

// The app is the web app.
//
// There used to be a second, native implementation of the notes list, editor,
// sync and sign-in here, and it drifted from the hosted app the day it was
// written: features landed on the phone's web app and never reached the APK,
// and Steve had two accounts to sign into. So the shell now loads the same
// hosted origin the PWA does. Every hosting deploy is an update to this app,
// with no rebuild and nothing to install — the service worker the site already
// ships keeps it working offline exactly as it does in the browser.
//
// What the APK adds is everything a web page on Android can't do: a microphone
// that survives the screen going off, spoken status over the headphones, and a
// launch by voice. Those live in DictationService / Speaker and are reached
// from the page through NativeBridge.
class MainActivity : ComponentActivity() {
    companion object {
        private const val TRUSTED_ORIGIN = "https://dictationmic-sync.web.app"
        const val EXTRA_DICTATE = "org.dictationmic.android.DICTATE"

        private const val OFFLINE_PAGE = """
            <html><head><meta name=viewport content="width=device-width,initial-scale=1">
            <style>html,body{height:100%;margin:0;background:#0B0C0A;color:#E8E6E1;
            font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;text-align:center}
            div{padding:2rem;max-width:22rem}button{margin-top:1.5rem;padding:.7rem 1.4rem;
            border:1px solid #3A3D36;border-radius:999px;background:none;color:inherit;font:inherit}
            </style></head><body><div><p>DictationMic couldn't reach the network on
            this first launch.</p><p style="opacity:.6">Once it has loaded once it
            works offline.</p><button onclick="location.href='/'">Try again</button>
            </div></body></html>"""
    }

    private lateinit var web: WebView
    // Only set when a file pick is being handed back to the WebView the old
    // way — i.e. when the page is too old to drain the inbox itself.
    private var filePicker: ValueCallback<Array<Uri>>? = null
    private var pendingMicRequest: PermissionRequest? = null
    private var stateJob: Job? = null
    private var loaded = false
    // A dictation waiting on the mic-permission dialog: null = none, otherwise
    // the handsFree flag it should start with once the permission lands.
    private var pendingDictation: Boolean? = null

    private val micPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()) { granted ->
        val req = pendingMicRequest
        pendingMicRequest = null
        if (req != null) {
            if (granted) req.grant(req.resources) else req.deny()
        }
        val handsFree = pendingDictation
        pendingDictation = null
        if (handsFree == null) return@registerForActivityResult
        if (granted) {
            beginDictation(handsFree)
        } else {
            // Refusing must be visible, not a silent dead button.
            Toast.makeText(this,
                "DictationMic can't record without the microphone — " +
                    "allow it in Settings › Apps › DictationMic",
                Toast.LENGTH_LONG).show()
            if (handsFree) Speaker.speak("The microphone is blocked, so I can't record.")
        }
    }

    // The picker behind "+ Image" / "+ File". What comes back doesn't go to the
    // WebView — it goes to SharedInbox, the same place a share-sheet drop lands,
    // and the page drains it from there. See onShowFileChooser for why.
    private val filePick = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()) { result ->
        val uris = WebChromeClient.FileChooserParams
            .parseResult(result.resultCode, result.data)
        val cb = filePicker
        filePicker = null
        if (cb != null) { cb.onReceiveValue(uris ?: emptyArray()); return@registerForActivityResult }
        if (uris.isNullOrEmpty()) return@registerForActivityResult   // cancelled
        takeSharedUris(uris.toList())
    }

    private val notifPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()) { }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        CloudSync.loadAccount(applicationContext)
        Speaker.init(applicationContext)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            notifPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        web = WebView(this)
        web.layoutParams = ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        setContentView(web)
        configure(web)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        web.loadUrl(TRUSTED_ORIGIN + "/")
        handleIntent(intent)
    }

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    private fun configure(w: WebView) {
        WebView.setWebContentsDebuggingEnabled(true)
        w.setBackgroundColor(0xFF0B0C0A.toInt())
        with(w.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true                 // localStorage: the session
            // The mic screen and the spoken status must not need a tap first.
            mediaPlaybackRequiresUserGesture = false
            useWideViewPort = true
            loadWithOverviewMode = true
            setSupportMultipleWindows(false)
            // Same string shape as Chrome, plus a marker the site could branch
            // on. Keeping "Chrome" in it matters: Google's sign-in endpoints
            // and more than one CDN reject unrecognised agents outright.
            userAgentString = userAgentString + " DictationMicShell"
        }
        w.addJavascriptInterface(
            NativeBridge(
                applicationContext, appVersion(),
                onAccount = { syncSoon() },
                // Bridge calls land on a WebView thread, and the permission
                // dialog can only be launched from the UI thread.
                onStartDictation = { hf ->
                    runOnUiThread { startDictationWithPermission(hf) }
                }),
            "DictationMicNative")

        w.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url.toString()
                if (url.startsWith(TRUSTED_ORIGIN)) return false
                // Anything else — a download link, a help page — belongs in the
                // browser, not trapped in a shell with no address bar.
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, request.url)) }
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                loaded = true
                // Dictation no longer waits for this — the service is already
                // running. All the page has to do is catch up and show it.
                if (DictationState.running.value) {
                    view.evaluateJavascript("location.hash = '#/mic'", null)
                }
                // A share that arrived before the page existed — a cold start
                // from the share sheet. The page drains at boot too; this is
                // for anything that landed between boot and now.
                nudgeSharedFiles()
            }

            // The page's renderer process can be killed by the system (out of
            // memory, a WebView update mid-flight). Unhandled, that takes the
            // whole app down with it — rebuild the activity instead.
            override fun onRenderProcessGone(
                view: WebView, detail: android.webkit.RenderProcessGoneDetail,
            ): Boolean {
                recreate()
                return true
            }

            // Only the very first launch can land here: after that the site's
            // own service worker serves the shell from cache and an offline
            // start looks no different from a normal one.
            override fun onReceivedError(
                view: WebView, request: WebResourceRequest,
                error: android.webkit.WebResourceError) {
                if (!request.isForMainFrame) return
                view.loadDataWithBaseURL(TRUSTED_ORIGIN, OFFLINE_PAGE,
                    "text/html", "utf-8", null)
            }
        }

        w.webChromeClient = object : WebChromeClient() {
            // The page asking for the microphone. The shell holds the Android
            // permission, so grant from it rather than making Steve answer two
            // prompts that mean the same thing.
            override fun onPermissionRequest(request: PermissionRequest) {
                val wantsMic = request.resources
                    .contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                if (!wantsMic) { request.deny(); return }
                if (hasMic()) { request.grant(request.resources); return }
                pendingMicRequest = request
                micPermission.launch(Manifest.permission.RECORD_AUDIO)
            }

            // "+ Image" and "+ File" are file inputs, and the WebView's own
            // answer to one is to hand the page a File built from whatever the
            // picker returned. That is where picking an image went quiet: the
            // photo picker answers with a URI carrying no filename and no
            // extension, the page gets a File with an empty type, and the app —
            // which decides what a file is by its type — dropped it without a
            // word.
            //
            // So the shell opens the picker (the intent the WebView builds
            // still honours accept= and multiple), reads the bytes itself with
            // the real MIME type from the ContentResolver, and pushes them into
            // the page through SharedInbox — the identical route a share-sheet
            // drop takes. The input itself is released straight away, so the
            // same button works again the next time it's pressed.
            override fun onShowFileChooser(
                view: WebView, callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams): Boolean {
                val handOff = SharedInbox.pageReady
                filePicker?.onReceiveValue(null)          // a pick left hanging
                filePicker = if (handOff) null else callback
                if (runCatching { filePick.launch(params.createIntent()) }.isFailure) {
                    filePicker = null
                    return false           // let the WebView try its own way
                }
                // Nothing is coming back to the input itself — release it now so
                // the same button works the next time it's pressed.
                if (handOff) callback.onReceiveValue(null)
                return true
            }
        }
    }

    // ---- launching ---------------------------------------------------------

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    // Two ways in. The launcher icon opens the notes list, same as tapping the
    // PWA. The "Dictate" shortcut — and a voice launch with the buds in — goes
    // straight to recording without waiting for a tap on a screen you can't see.
    private fun handleIntent(intent: Intent?) {
        // The update notification's tap: fetch the APK and hand it to the
        // installer, right here in the app.
        if (intent?.action == Updater.ACTION_INSTALL) {
            val url = intent.getStringExtra(Updater.EXTRA_APK_URL)
            if (url != null) {
                installUpdate(url, intent.getStringExtra(Updater.EXTRA_VERSION) ?: "")
            }
            return
        }
        // Sharing a screenshot to DictationMic. The PWA answered the share
        // sheet through its manifest's share_target; the APK isn't a WebAPK, so
        // it has to be in the share sheet on its own account (the SEND filters
        // in the manifest) and carry the file across itself.
        if (intent != null &&
            (intent.action == Intent.ACTION_SEND ||
                intent.action == Intent.ACTION_SEND_MULTIPLE)) {
            receiveShared(intent)
            return
        }
        val asked = intent?.getBooleanExtra(EXTRA_DICTATE, false) == true ||
            intent?.action == Intent.ACTION_ASSIST ||
            intent?.data?.getQueryParameter("dictate") == "1"
        val auto = asked || (autoStartWithHeadphones() &&
            AudioRoute.headphonesConnected(this) && !DictationState.running.value)
        if (!auto) return
        // Start the recorder NOW — it's a service and needs nothing from the
        // WebView. Waiting for the page to load here was seconds of talking to
        // a dead microphone on every voice launch.
        startDictationWithPermission(handsFree = true)
    }

    // ---- files from the share sheet and the file picker --------------------

    private fun receiveShared(intent: Intent) {
        val uris = if (intent.action == Intent.ACTION_SEND) {
            listOfNotNull(IntentCompat.getParcelableExtra(
                intent, Intent.EXTRA_STREAM, Uri::class.java))
        } else {
            IntentCompat.getParcelableArrayListExtra(
                intent, Intent.EXTRA_STREAM, Uri::class.java).orEmpty().filterNotNull()
        }
        // Claim it. The same intent is handed back on a recreate (process
        // death, a restore from Recents) and without this the shared image
        // would be saved a second time.
        intent.removeExtra(Intent.EXTRA_STREAM)
        if (uris.isEmpty()) {
            Toast.makeText(this, "Nothing came through from that share",
                Toast.LENGTH_SHORT).show()
            return
        }
        takeSharedUris(uris)
    }

    // Reading a content:// URI is disk (or network, for a cloud photo) — off
    // the main thread. Then wake the page, which pulls the bytes over the
    // bridge and saves them as notes.
    private fun takeSharedUris(uris: List<Uri>) {
        lifecycleScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val taken = SharedInbox.accept(applicationContext, uris)
            withContext(kotlinx.coroutines.Dispatchers.Main) {
                if (taken == 0) {
                    Toast.makeText(this@MainActivity,
                        "Couldn't read that file", Toast.LENGTH_LONG).show()
                    return@withContext
                }
                nudgeSharedFiles()
            }
        }
    }

    private fun nudgeSharedFiles() {
        if (!loaded || SharedInbox.isEmpty()) return
        web.evaluateJavascript(
            "window.DictationMicShell&&window.DictationMicShell.shared" +
                "&&window.DictationMicShell.shared()", null)
    }

    private fun autoStartWithHeadphones() =
        getSharedPreferences("app", MODE_PRIVATE).getBoolean("autoStartHeadphones", true)

    // Every way a dictation starts — voice launch, the page's mic buttons —
    // funnels through here, so the permission is always asked for before the
    // service runs. Starting the microphone service without it is a crash,
    // not a refusal, on Android 14+.
    private fun startDictationWithPermission(handsFree: Boolean) {
        if (hasMic()) { beginDictation(handsFree); return }
        pendingDictation = handsFree         // the permission result picks it up
        micPermission.launch(Manifest.permission.RECORD_AUDIO)
    }

    private fun beginDictation(handsFree: Boolean) {
        if (DictationState.running.value) return
        DictationService.start(this, handsFree)
        // Before the first page load this is a no-op; onPageFinished sees the
        // running service and routes to the mic screen itself.
        if (handsFree && loaded) web.evaluateJavascript("location.hash = '#/mic'", null)
    }

    private fun hasMic() = ContextCompat.checkSelfPermission(
        this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    // The unmissable version of "there's an update": a dialog in the app
    // itself, offered once per version per launch. "Later" costs nothing —
    // the next open offers it again.
    private var offeredUpdate: String? = null
    private fun offerUpdate(rel: Updater.Release) {
        if (isFinishing || offeredUpdate == rel.version) return
        if (DictationState.running.value) return   // never interrupt a dictation
        offeredUpdate = rel.version
        android.app.AlertDialog.Builder(this)
            .setTitle("DictationMic ${rel.version} is ready")
            .setMessage("Install it now? It updates in place and keeps your notes.")
            .setPositiveButton("Install") { _, _ ->
                val url = rel.apkUrl
                if (url != null) installUpdate(url, rel.version)
                else startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(rel.pageUrl)))
            }
            .setNegativeButton("Later", null)
            .show()
    }

    // Download in the background, then let Android's installer take over. Same
    // signing key, so it installs over the top and keeps the account and notes.
    private fun installUpdate(url: String, version: String) {
        Toast.makeText(this,
            "Downloading DictationMic ${version.ifBlank { "update" }}…",
            Toast.LENGTH_SHORT).show()
        lifecycleScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val apk = Updater.download(applicationContext, url)
            withContext(kotlinx.coroutines.Dispatchers.Main) {
                if (apk != null) Updater.install(this@MainActivity, apk)
                else Toast.makeText(this@MainActivity,
                    "Couldn't download the update — try again later",
                    Toast.LENGTH_LONG).show()
            }
        }
    }

    // ---- pushing recorder state into the page ------------------------------

    override fun onResume() {
        super.onResume()
        // Every return to the app re-offers a known update immediately — a
        // dialog, so it works even with notifications blocked. Only the
        // GitHub call itself is rate-limited.
        lifecycleScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            val rel = Updater.pendingUpdate(applicationContext, appVersion())
            if (rel != null) withContext(kotlinx.coroutines.Dispatchers.Main) {
                offerUpdate(rel)
            }
        }
        // The recorder runs in a service and keeps going with this activity
        // gone, so the page is only ever a mirror of it. Poll while we're on
        // screen: a flow collector would need the page to be alive to receive
        // it, and the page is exactly the thing that isn't, half the time.
        stateJob?.cancel()
        stateJob = lifecycleScope.launch {
            var last = ""
            while (true) {
                val snap = JSONObject().apply {
                    put("running", DictationState.running.value)
                    put("finals", DictationState.finals.value)
                    put("partial", DictationState.partial.value)
                    put("level", DictationState.level.value.toDouble())
                    put("savedAt", DictationState.savedNoteAt.value)
                }.toString()
                if (snap != last) {
                    last = snap
                    web.evaluateJavascript(
                        "window.DictationMicShell&&window.DictationMicShell.state($snap)",
                        null)
                }
                delay(200)
            }
        }
    }

    override fun onPause() {
        super.onPause()
        stateJob?.cancel()
        stateJob = null
    }

    override fun onDestroy() {
        stateJob?.cancel()
        // Deliberately not stopping the service: walking out of the activity is
        // the normal way a hands-free dictation continues.
        web.destroy()
        super.onDestroy()
    }

    // A fresh sign-in in the page means the recorder can upload for the first
    // time — pull the notes down so the two sides start from the same place.
    private fun syncSoon() {
        lifecycleScope.launch { runCatching { CloudSync.syncNow(applicationContext) } }
    }

    private fun appVersion(): String =
        runCatching { packageManager.getPackageInfo(packageName, 0).versionName ?: "" }
            .getOrDefault("")
}
