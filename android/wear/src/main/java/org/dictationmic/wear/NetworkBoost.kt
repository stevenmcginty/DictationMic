package org.dictationmic.wear

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log

// Holds the cellular radio up while something is — or is about to be —
// talking to the network.
//
// Why this exists: off the phone and off Wi-Fi, the watch takes on the order
// of a minute to notice its Bluetooth proxy is gone and bring LTE up on its
// own. That minute lands exactly where it hurts: the walk away from the house
// is when the wrist gets used, and a dictation started in it hears nothing
// (no offline model — see DictationScreen) while the upload of the previous
// note sits queued. Requesting a cellular network explicitly makes the system
// attach the modem right away instead of on its own clock.
//
// Battery: the request is held only while the app is open, the tile is on
// screen, or a dictation is running. Released, the radio drops back to the
// system's own policy. Tags rather than a counter so a double hold from the
// same place can never wedge the radio on.
object NetworkBoost {
    private const val LOG = "DictationMic"

    private val holds = mutableSetOf<String>()
    private var cm: ConnectivityManager? = null
    private var callback: ConnectivityManager.NetworkCallback? = null
    private var bound = false

    @Synchronized
    fun hold(ctx: Context, tag: String) {
        holds.add(tag)
        if (callback != null) return
        val mgr = ctx.applicationContext
            .getSystemService(ConnectivityManager::class.java) ?: return
        cm = mgr
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                Log.i(LOG, "cellular attached (holds=$holds)")
                maybeBind(network)
            }

            override fun onLost(network: Network) {
                unbind()
            }
        }
        callback = cb
        runCatching {
            mgr.requestNetwork(
                NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
                    .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                    .build(),
                cb)
            Log.i(LOG, "cellular requested (holds=$holds)")
        }.onFailure {
            // No cellular on this watch, or the system said no — nothing to
            // hold, and nothing to release later.
            callback = null
            cm = null
            Log.i(LOG, "cellular request failed: $it")
        }
    }

    @Synchronized
    fun release(tag: String) {
        holds.remove(tag)
        if (holds.isNotEmpty()) return
        callback?.let { cb -> runCatching { cm?.unregisterNetworkCallback(cb) } }
        callback = null
        unbind()
        cm = null
        Log.i(LOG, "cellular released")
    }

    // The default network can sit "connected" on a Bluetooth proxy that no
    // longer reaches anything for the better part of a minute after walking
    // away. While that is the case, pin this process's traffic to the cellular
    // network we asked for, so uploads leave immediately. The recogniser lives
    // in Google's process and picks its own network — for it, the modem
    // already being attached is what shortens the failover.
    @Synchronized
    private fun maybeBind(network: Network) {
        val mgr = cm ?: return
        val def = mgr.activeNetwork
        val validated = def != null && mgr.getNetworkCapabilities(def)
            ?.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED) == true
        if (validated) return          // the normal route works; stay off LTE
        bound = runCatching { mgr.bindProcessToNetwork(network) }.getOrDefault(false)
        if (bound) Log.i(LOG, "process bound to cellular — default route is dead")
    }

    @Synchronized
    private fun unbind() {
        if (!bound) return
        bound = false
        runCatching { cm?.bindProcessToNetwork(null) }
        Log.i(LOG, "process unbound from cellular")
    }
}
