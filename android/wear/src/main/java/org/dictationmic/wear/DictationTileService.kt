package org.dictationmic.wear

import androidx.concurrent.futures.ResolvableFuture
import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.ColorBuilders
import androidx.wear.protolayout.DimensionBuilders
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.tiles.EventBuilders
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.ListenableFuture

// A tile: one swipe from the watch face, one tap, and the voice screen is open.
//
// This is the entry point the app is actually used through. Opening the app
// from the launcher is a crown scroll through an alphabetical list, which is
// three deliberate actions on a wrist that is mid-walk; the tile is a flick and
// a thumb. It also survives the case the launcher does not: the watch face is
// what is on screen when the wrist comes up, so the tile is always exactly one
// gesture away from wherever the watch already was.
//
// The tap goes straight to dictating rather than to the app's own mic button.
// A tile that opens a screen with a button on it has spent the user's tap and
// given them another one to make.
class DictationTileService : TileService() {

    // Swiping to the tile is the moment of intent — the tap is coming. Start
    // the LTE attach now and the recogniser isn't opening onto a watch that
    // still thinks a long-gone phone is its network (see NetworkBoost).
    override fun onTileEnterEvent(requestParams: EventBuilders.TileEnterEvent) {
        NetworkBoost.hold(this, "tile")
    }

    override fun onTileLeaveEvent(requestParams: EventBuilders.TileLeaveEvent) {
        // If the tap happened, the activity and the session carry their own
        // holds by now; this one has done its job either way.
        NetworkBoost.release("tile")
    }

    override fun onTileRequest(
        requestParams: RequestBuilders.TileRequest,
    ): ListenableFuture<TileBuilders.Tile> = immediate(
        TileBuilders.Tile.Builder()
            .setResourcesVersion(RESOURCES_VERSION)
            // Nothing on this tile changes on its own — it is a button, not a
            // readout — so the system never needs to ask for it again.
            .setFreshnessIntervalMillis(0)
            .setTileTimeline(
                TimelineBuilders.Timeline.Builder()
                    .addTimelineEntry(
                        TimelineBuilders.TimelineEntry.Builder()
                            .setLayout(
                                LayoutElementBuilders.Layout.Builder()
                                    .setRoot(tileLayout())
                                    .build())
                            .build())
                    .build())
            .build())

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest,
    ): ListenableFuture<ResourceBuilders.Resources> = immediate(
        ResourceBuilders.Resources.Builder()
            .setVersion(RESOURCES_VERSION)
            .addIdToImageMapping(
                MIC_IMAGE,
                ResourceBuilders.ImageResource.Builder()
                    .setAndroidResourceByResId(
                        ResourceBuilders.AndroidImageResourceByResId.Builder()
                            .setResourceId(R.drawable.ic_tile_mic)
                            .build())
                    .build())
            .build())

    // The whole tile is the target, not just the glyph. A wrist in motion is a
    // bad aim, and there is only one thing here to hit.
    private fun tileLayout(): LayoutElementBuilders.LayoutElement =
        LayoutElementBuilders.Box.Builder()
            .setWidth(DimensionBuilders.expand())
            .setHeight(DimensionBuilders.expand())
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setBackground(
                        ModifiersBuilders.Background.Builder()
                            .setColor(ColorBuilders.argb(INK))
                            .build())
                    .setClickable(
                        ModifiersBuilders.Clickable.Builder()
                            .setId("dictate")
                            .setOnClick(openDictating())
                            .build())
                    .setSemantics(
                        ModifiersBuilders.Semantics.Builder()
                            .setContentDescription("Start dictating")
                            .build())
                    .build())
            .addContent(
                LayoutElementBuilders.Column.Builder()
                    .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
                    .addContent(
                        LayoutElementBuilders.Image.Builder()
                            .setResourceId(MIC_IMAGE)
                            .setWidth(DimensionBuilders.dp(52f))
                            .setHeight(DimensionBuilders.dp(52f))
                            .build())
                    .addContent(
                        LayoutElementBuilders.Spacer.Builder()
                            .setHeight(DimensionBuilders.dp(10f))
                            .build())
                    .addContent(
                        LayoutElementBuilders.Text.Builder()
                            .setText("Dictate")
                            .setFontStyle(
                                LayoutElementBuilders.FontStyle.Builder()
                                    .setSize(DimensionBuilders.sp(16f))
                                    .setColor(ColorBuilders.argb(CHALK))
                                    .build())
                            .build())
                    .build())
            .build()

    // The extra is what tells MainActivity this launch is not a browse — it is
    // someone who has already decided to talk, and the voice screen should be
    // up by the time they look down.
    private fun openDictating(): ActionBuilders.Action =
        ActionBuilders.LaunchAction.Builder()
            .setAndroidActivity(
                ActionBuilders.AndroidActivity.Builder()
                    .setPackageName("org.dictationmic.wear")
                    .setClassName("org.dictationmic.wear.MainActivity")
                    .addKeyToExtraMapping(
                        MainActivity.EXTRA_DICTATE,
                        ActionBuilders.AndroidBooleanExtra.Builder()
                            .setValue(true)
                            .build())
                    .build())
            .build()

    private fun <T> immediate(value: T): ListenableFuture<T> =
        ResolvableFuture.create<T>().also { it.set(value) }

    private companion object {
        // Bumped only when the drawables below change; the system caches
        // resources against it and will not re-fetch while it is the same.
        const val RESOURCES_VERSION = "1"
        const val MIC_IMAGE = "mic"

        // Duplicated from Theme.kt rather than imported: those are Compose
        // Colors and protolayout takes a plain ARGB int, and converting one to
        // the other is more indirection than two constants are worth.
        const val INK = 0xFF0B0C0A.toInt()
        const val CHALK = 0xFFE8E6E1.toInt()
    }
}
