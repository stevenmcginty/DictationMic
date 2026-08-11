package org.dictationmic.android

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager

// "Are the buds in?" — the one signal that separates the two ways this app gets
// opened. Tapping the icon on the home screen means the screen is in your hand
// and dictation should wait for a tap. Saying "open DictationMic" with the buds
// in means the phone is in another room and it should already be listening by
// the time you draw breath. There is no reliable way to tell those apart from
// the launch intent, but the headphones are the thing that is actually
// different about the second one.
object AudioRoute {
    private val WEARABLE = setOf(
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP,
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
        AudioDeviceInfo.TYPE_WIRED_HEADSET,
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
        AudioDeviceInfo.TYPE_USB_HEADSET,
        AudioDeviceInfo.TYPE_HEARING_AID,
    )

    fun headphonesConnected(ctx: Context): Boolean {
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            ?: return false
        return runCatching {
            am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).any { it.type in WEARABLE }
        }.getOrDefault(false)
    }
}
