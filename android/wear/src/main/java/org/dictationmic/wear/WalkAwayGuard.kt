package org.dictationmic.wear

import android.bluetooth.BluetoothDevice
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.Looper
import android.util.Log

// The moment of walking away, caught at its earliest signals.
//
// The 30 seconds Steve waits for LTE is mostly the watch not yet knowing the
// phone is gone: an idle Bluetooth link only discovers it died when the
// supervision timeout expires, and only then does Wear think about cellular.
// Two listeners shortcut that:
//
//   - a Bluetooth ACL disconnect — the stack's own "the link just dropped",
//     the earliest event there is (needs BLUETOOTH_CONNECT, asked once in
//     MainActivity; without it the second listener still works)
//   - any non-cellular network being lost (the phone proxy, Wi-Fi)
//
// Either one slams the cellular hold on for two minutes, so the modem is
// attaching while the rest of the system is still working out what happened.
// If the network comes straight back (walked past the router, not out of the
// house), the hold quietly expires and the radio goes back to sleep.
object WalkAwayGuard {
    private const val LOG = "DictationMic"
    private const val HOLD_MS = 120_000L

    private val handler = Handler(Looper.getMainLooper())
    private var drop: Runnable? = null
    private var started = false

    // Which networks are cellular, learned while they're alive — onLost hands
    // us a Network whose capabilities are already gone, and re-holding the
    // radio because the *cellular* network died would chase its own tail.
    private val cellular = mutableSetOf<Network>()

    fun start(ctx: Context) {
        if (started) return
        started = true
        val app = ctx.applicationContext
        val cm = app.getSystemService(ConnectivityManager::class.java) ?: return

        cm.registerNetworkCallback(
            NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build(),
            object : ConnectivityManager.NetworkCallback() {
                override fun onCapabilitiesChanged(
                    network: Network, caps: NetworkCapabilities,
                ) {
                    if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
                        cellular.add(network)
                    } else {
                        cellular.remove(network)
                    }
                }

                override fun onLost(network: Network) {
                    if (cellular.remove(network)) return
                    Log.i(LOG, "walk-away: network lost — warming LTE")
                    poke(app)
                }
            })

        // Delivered only with BLUETOOTH_CONNECT granted; registering without
        // it is harmless, the broadcasts just never arrive.
        runCatching {
            app.registerReceiver(
                object : BroadcastReceiver() {
                    override fun onReceive(c: Context, i: Intent) {
                        Log.i(LOG, "walk-away: bluetooth link dropped — warming LTE")
                        poke(c)
                    }
                },
                IntentFilter(BluetoothDevice.ACTION_ACL_DISCONNECTED),
                Context.RECEIVER_NOT_EXPORTED,
            )
        }.onFailure { Log.i(LOG, "walk-away: no bluetooth receiver ($it)") }
    }

    private fun poke(ctx: Context) {
        NetworkBoost.hold(ctx, "walk-away")
        drop?.let(handler::removeCallbacks)
        drop = Runnable {
            drop = null
            NetworkBoost.release("walk-away")
        }.also { handler.postDelayed(it, HOLD_MS) }
    }
}
