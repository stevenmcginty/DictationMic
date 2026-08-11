package org.dictationmic.wear

import android.app.PendingIntent
import android.content.Intent
import android.graphics.drawable.Icon
import androidx.wear.watchface.complications.data.ComplicationData
import androidx.wear.watchface.complications.data.ComplicationType
import androidx.wear.watchface.complications.data.MonochromaticImage
import androidx.wear.watchface.complications.data.MonochromaticImageComplicationData
import androidx.wear.watchface.complications.data.PlainComplicationText
import androidx.wear.watchface.complications.data.ShortTextComplicationData
import androidx.wear.watchface.complications.datasource.ComplicationRequest
import androidx.wear.watchface.complications.datasource.SuspendingComplicationDataSourceService

// A complication: the mic on the watch face itself.
//
// The tile is one swipe away; this is zero. A face slot showing this glyph
// means the wrist comes up already holding the button — tap, talk, done —
// which on a walk is the difference between dictating the thought and
// deciding it can wait.
//
// Nothing here ever changes on its own (it is a button, not a readout), so
// the update period is 0 and the system never needs to ask twice.
class DictationComplicationService : SuspendingComplicationDataSourceService() {

    override suspend fun onComplicationRequest(
        request: ComplicationRequest,
    ): ComplicationData? = data(request.complicationType, tapAction())

    // What the face picker shows while the slot is being chosen — same glyph,
    // no live tap action needed.
    override fun getPreviewData(type: ComplicationType): ComplicationData? =
        data(type, tap = null)

    private fun tapAction(): PendingIntent = PendingIntent.getActivity(
        this, 0,
        Intent(this, MainActivity::class.java)
            .putExtra(MainActivity.EXTRA_DICTATE, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun data(type: ComplicationType, tap: PendingIntent?): ComplicationData? {
        val icon = MonochromaticImage.Builder(
            Icon.createWithResource(this, R.drawable.ic_tile_mic)).build()
        val label = PlainComplicationText.Builder("Dictate").build()
        return when (type) {
            ComplicationType.MONOCHROMATIC_IMAGE ->
                MonochromaticImageComplicationData.Builder(icon, label)
                    .setTapAction(tap)
                    .build()
            ComplicationType.SHORT_TEXT ->
                ShortTextComplicationData.Builder(
                    PlainComplicationText.Builder("Mic").build(), label)
                    .setMonochromaticImage(icon)
                    .setTapAction(tap)
                    .build()
            else -> null
        }
    }
}
