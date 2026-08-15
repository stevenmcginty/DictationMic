package org.dictationmic.wear

import android.content.ComponentName
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.Typeface
import android.os.BatteryManager
import android.text.format.DateFormat
import android.view.SurfaceHolder
import androidx.wear.watchface.CanvasComplicationFactory
import androidx.wear.watchface.CanvasType
import androidx.wear.watchface.ComplicationSlot
import androidx.wear.watchface.ComplicationSlotsManager
import androidx.wear.watchface.Renderer
import androidx.wear.watchface.TapEvent
import androidx.wear.watchface.TapType
import androidx.wear.watchface.WatchFace
import androidx.wear.watchface.WatchFaceService
import androidx.wear.watchface.WatchFaceType
import androidx.wear.watchface.WatchState
import androidx.wear.watchface.complications.ComplicationSlotBounds
import androidx.wear.watchface.complications.DefaultComplicationDataSourcePolicy
import androidx.wear.watchface.complications.SystemDataSources
import androidx.wear.watchface.complications.data.ComplicationType
import androidx.wear.watchface.complications.rendering.CanvasComplicationDrawable
import androidx.wear.watchface.complications.rendering.ComplicationDrawable
import androidx.wear.watchface.style.CurrentUserStyleRepository
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlin.math.hypot
import org.dictationmic.android.DictationService

// Steve's face, rebuilt as ours. Same bones as the stock Utility face he
// already wears — date up top, a big lime time, steps on the left wrist of
// the dial, heart rate on the right — with the two changes that face could
// never be talked into:
//
//   - the middle slot is the mic. Wrist up, one press, dictating. Not a
//     shortcut to an app with a button in it — the tap starts the session.
//   - the battery lives on the rim: a ring around the whole face, full when
//     full, and never another swipe-down just to know.
//
// Steps and heart rate stay real complications wired to the system's own
// data sources, so they read the same numbers the Fitbit face showed and
// still open their apps on a tap.
class DictationWatchFaceService : WatchFaceService() {

    companion object {
        private const val LEFT_SLOT = 10      // steps
        private const val RIGHT_SLOT = 11     // heart rate

        // Colours, shared with the app's theme (Theme.kt) — the face and the
        // app should read as one thing.
        private const val INK = 0xFF0B0C0A.toInt()
        private const val VOLT = 0xFFC6EC4B.toInt()   // the Utility-face lime
        private const val CHALK = 0xFFE8E6E1.toInt()
        private const val MUTED = 0xFF9AA096.toInt()
        private const val TRACK = 0x33FFFFFF          // dim ring under arcs
        private const val AMBER = 0xFFFFB74D.toInt()  // battery running out
        private const val TEAL = 0xFF12D6DF.toInt()   // Knee Rebuild's accent

        // The knee-rehab app, if it's installed. Its launcher sits at the
        // bottom of the dial, under the mic.
        private const val KNEE_PACKAGE = "com.kneerebuild"
        private const val KNEE_ACTIVITY = "com.kneerebuild.wear.MainActivity"
    }

    private fun kneeInstalled(): Boolean =
        packageManager.getLaunchIntentForPackage(KNEE_PACKAGE) != null

    private fun styled(drawable: ComplicationDrawable): ComplicationDrawable =
        drawable.apply {
            activeStyle.apply {
                backgroundColor = Color.TRANSPARENT
                borderColor = Color.TRANSPARENT
                textColor = CHALK
                titleColor = MUTED
                iconColor = VOLT
                rangedValuePrimaryColor = VOLT
                rangedValueSecondaryColor = TRACK
            }
            ambientStyle.apply {
                backgroundColor = Color.TRANSPARENT
                borderColor = Color.TRANSPARENT
                textColor = MUTED
                titleColor = MUTED
                iconColor = MUTED
                rangedValuePrimaryColor = MUTED
                rangedValueSecondaryColor = Color.TRANSPARENT
            }
        }

    override fun createComplicationSlotsManager(
        currentUserStyleRepository: CurrentUserStyleRepository,
    ): ComplicationSlotsManager {
        val factory = CanvasComplicationFactory { watchState, listener ->
            CanvasComplicationDrawable(
                styled(ComplicationDrawable(this)), watchState, listener)
        }
        val types = listOf(
            ComplicationType.RANGED_VALUE,
            ComplicationType.SHORT_TEXT,
        )
        // Defaults are Fitbit's own providers — the exact numbers the stock
        // face showed (queried off the watch itself). The system's step
        // counter and the battery stand in if Fitbit ever disappears.
        // Bounds are fractions of the square face. Same row the Utility face
        // draws its rings on: centres at 22% and 78% across, 67% down.
        val left = ComplicationSlot.createRoundRectComplicationSlotBuilder(
            LEFT_SLOT, factory, types,
            DefaultComplicationDataSourcePolicy(
                ComponentName("com.fitbit.FitbitMobile",
                    "com.fitbit.complications.offloadable.steps"
                        + ".OffloadableStepsComplicationDataSourceService"),
                ComplicationType.RANGED_VALUE,
                SystemDataSources.DATA_SOURCE_STEP_COUNT,
                ComplicationType.RANGED_VALUE),
            ComplicationSlotBounds(RectF(0.085f, 0.535f, 0.355f, 0.805f)),
        ).build()
        val right = ComplicationSlot.createRoundRectComplicationSlotBuilder(
            RIGHT_SLOT, factory, types,
            DefaultComplicationDataSourcePolicy(
                ComponentName("com.fitbit.FitbitMobile",
                    "com.fitbit.complications.offloadable.heartrate"
                        + ".OffloadableHeartRateComplicationDataSourceService"),
                ComplicationType.SHORT_TEXT,
                SystemDataSources.DATA_SOURCE_WATCH_BATTERY,
                ComplicationType.RANGED_VALUE),
            ComplicationSlotBounds(RectF(0.645f, 0.535f, 0.915f, 0.805f)),
        ).build()
        return ComplicationSlotsManager(
            listOf(left, right), currentUserStyleRepository)
    }

    override suspend fun createWatchFace(
        surfaceHolder: SurfaceHolder,
        watchState: WatchState,
        complicationSlotsManager: ComplicationSlotsManager,
        currentUserStyleRepository: CurrentUserStyleRepository,
    ): WatchFace {
        val renderer = FaceRenderer(
            surfaceHolder, currentUserStyleRepository, watchState,
            complicationSlotsManager)
        return WatchFace(WatchFaceType.DIGITAL, renderer)
            .setTapListener(object : WatchFace.TapListener {
                override fun onTapEvent(
                    tapType: Int,
                    tapEvent: TapEvent,
                    complicationSlot: ComplicationSlot?,
                ) {
                    if (tapType != TapType.UP || complicationSlot != null) return
                    val b = renderer.screenBounds
                    if (b.isEmpty) return
                    // the mic circle, dead centre of the bottom row
                    val cx = b.exactCenterX()
                    val cy = b.height() * 0.67f
                    val r = b.width() * 0.135f
                    val d = hypot(
                        (tapEvent.xPos - cx).toDouble(),
                        (tapEvent.yPos - cy).toDouble())
                    if (d <= r) {
                        startActivity(
                            Intent(this@DictationWatchFaceService,
                                MainActivity::class.java)
                                .putExtra(MainActivity.EXTRA_DICTATE, true)
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                        return
                    }
                    // the knee launcher, bottom of the dial
                    if (kneeInstalled()) {
                        val kx = b.exactCenterX()
                        val ky = b.height() * 0.895f
                        val kr = b.width() * 0.085f
                        val kd = hypot(
                            (tapEvent.xPos - kx).toDouble(),
                            (tapEvent.yPos - ky).toDouble())
                        if (kd <= kr) {
                            try {
                                startActivity(
                                    Intent(Intent.ACTION_VIEW)
                                        .setClassName(KNEE_PACKAGE, KNEE_ACTIVITY)
                                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                            } catch (_: Exception) {
                                // app vanished between the draw and the tap;
                                // next render drops the icon
                            }
                        }
                    }
                }
            })
    }

    private inner class FaceRenderer(
        surfaceHolder: SurfaceHolder,
        currentUserStyleRepository: CurrentUserStyleRepository,
        private val watchState: WatchState,
        private val slots: ComplicationSlotsManager,
    ) : Renderer.CanvasRenderer2<Renderer.SharedAssets>(
        surfaceHolder, currentUserStyleRepository, watchState,
        CanvasType.HARDWARE,
        interactiveDrawModeUpdateDelayMillis = 60_000L,
        clearWithBackgroundTintBeforeRenderingHighlightLayer = false,
    ) {
        private val timePaint = Paint().apply {
            isAntiAlias = true
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            color = VOLT
            textAlign = Paint.Align.CENTER
        }
        private val datePaint = Paint().apply {
            isAntiAlias = true
            typeface = Typeface.create("sans-serif-medium", Typeface.BOLD)
            textAlign = Paint.Align.LEFT
            letterSpacing = 0.08f
        }
        private val arcPaint = Paint().apply {
            isAntiAlias = true
            style = Paint.Style.STROKE
            strokeCap = Paint.Cap.ROUND
        }
        private val fillPaint = Paint().apply { isAntiAlias = true }

        private val timeFmt24 = DateTimeFormatter.ofPattern("HH:mm")
        private val timeFmt12 = DateTimeFormatter.ofPattern("h:mm")
        private val dayFmt = DateTimeFormatter.ofPattern("EEE", Locale.getDefault())
        private val dateFmt = DateTimeFormatter.ofPattern("d MMM", Locale.getDefault())

        // Sticky broadcast — no receiver to keep alive, just read it when
        // drawing. One read a minute is nothing.
        private fun batteryFraction(): Float {
            val i = registerReceiver(null,
                IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return 1f
            val level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
            val scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
            if (level < 0 || scale <= 0) return 1f
            return level.toFloat() / scale
        }

        override suspend fun createSharedAssets(): SharedAssets =
            object : SharedAssets { override fun onDestroy() {} }

        override fun render(
            canvas: Canvas,
            bounds: Rect,
            zonedDateTime: ZonedDateTime,
            sharedAssets: SharedAssets,
        ) {
            val ambient = watchState.isAmbient.value == true
            val w = bounds.width().toFloat()
            val h = bounds.height().toFloat()
            canvas.drawColor(INK)

            // ---- battery on the rim -------------------------------------
            // Full battery, full circle. It drains clockwise from the top,
            // so a glance at where the lime stops IS the percentage — and
            // below one fifth it turns amber.
            val battery = batteryFraction()
            val stroke = w * 0.018f
            val inset = stroke / 2f + w * 0.008f
            val ring = RectF(inset, inset, w - inset, h - inset)
            arcPaint.strokeWidth = stroke
            if (!ambient) {
                arcPaint.color = TRACK
                canvas.drawArc(ring, 0f, 360f, false, arcPaint)
            }
            arcPaint.color = when {
                ambient -> MUTED
                battery <= 0.2f -> AMBER
                else -> VOLT
            }
            canvas.drawArc(ring, -90f, 360f * battery, false, arcPaint)

            // ---- date ---------------------------------------------------
            val day = dayFmt.format(zonedDateTime).uppercase(Locale.getDefault())
            val rest = dateFmt.format(zonedDateTime).uppercase(Locale.getDefault())
            datePaint.textSize = h * 0.062f
            val dayW = datePaint.measureText("$day ")
            val restW = datePaint.measureText(rest)
            val dateX = (w - dayW - restW) / 2f
            val dateY = h * 0.155f
            datePaint.color = if (ambient) MUTED else VOLT
            canvas.drawText(day, dateX, dateY, datePaint)
            datePaint.color = if (ambient) MUTED else CHALK
            canvas.drawText(rest, dateX + dayW, dateY, datePaint)

            // ---- time ---------------------------------------------------
            val is24 = DateFormat.is24HourFormat(this@DictationWatchFaceService)
            val time = (if (is24) timeFmt24 else timeFmt12).format(zonedDateTime)
            timePaint.textSize = h * 0.30f
            timePaint.color = if (ambient) CHALK else VOLT
            canvas.drawText(time, w / 2f, h * 0.475f, timePaint)

            // ---- the mic, dead centre of the bottom row -----------------
            val cx = w / 2f
            val cy = h * 0.67f
            val r = w * 0.115f
            if (!ambient) {
                fillPaint.color = 0xFF1C1E17.toInt()      // quiet dark pad
                canvas.drawCircle(cx, cy, r, fillPaint)
                drawMic(canvas, cx, cy, r * 0.98f, VOLT)
            } else {
                drawMic(canvas, cx, cy, r * 0.98f, MUTED)
            }

            // ---- the knee app, bottom of the dial -----------------------
            // Only drawn while Knee Rebuild is installed; tap opens today's
            // session. Teal on purpose — it's that app's accent, so the icon
            // reads as a doorway to it rather than a fourth face element.
            if (kneeInstalled()) {
                val kx = w / 2f
                val ky = h * 0.895f
                val kr = w * 0.068f
                if (!ambient) {
                    fillPaint.color = 0xFF14201F.toInt()  // quiet dark pad
                    canvas.drawCircle(kx, ky, kr, fillPaint)
                    drawKnee(canvas, kx, ky, kr * 0.95f, TEAL)
                } else {
                    drawKnee(canvas, kx, ky, kr * 0.95f, MUTED)
                }
            }

            // ---- steps & heart rate -------------------------------------
            for ((_, slot) in slots.complicationSlots) {
                if (!slot.enabled) continue
                // A ranged value draws its own arc; a short-text one (heart
                // rate) draws none, which unbalances the row — give it the
                // same quiet ring the stock face kept under its dials.
                if (!ambient && slot.complicationData.value.type
                    == ComplicationType.SHORT_TEXT) {
                    val b = slot.computeBounds(bounds)
                    arcPaint.color = TRACK
                    arcPaint.strokeWidth = w * 0.018f
                    canvas.drawCircle(b.exactCenterX(), b.exactCenterY(),
                        b.width() / 2f * 0.9f, arcPaint)
                }
                slot.render(canvas, zonedDateTime, renderParameters)
            }
        }

        override fun renderHighlightLayer(
            canvas: Canvas,
            bounds: Rect,
            zonedDateTime: ZonedDateTime,
            sharedAssets: SharedAssets,
        ) {
            for ((_, slot) in slots.complicationSlots) {
                if (slot.enabled) {
                    slot.renderHighlightLayer(canvas, zonedDateTime, renderParameters)
                }
            }
        }

        // A bent leg seen side-on — thigh, shin, foot, and the joint dot the
        // knee app marks on every drawing. Same visual language as its own
        // glyph, shrunk to a dial ornament.
        private fun drawKnee(canvas: Canvas, cx: Float, cy: Float, r: Float, colour: Int) {
            arcPaint.color = colour
            arcPaint.strokeWidth = r * 0.24f
            // thigh: upper-left down to the joint
            canvas.drawLine(cx - r * 0.75f, cy - r * 0.55f, cx + r * 0.15f, cy, arcPaint)
            // shin: joint down to the ankle
            canvas.drawLine(cx + r * 0.15f, cy, cx - r * 0.05f, cy + r * 0.75f, arcPaint)
            // foot
            canvas.drawLine(cx - r * 0.05f, cy + r * 0.75f, cx + r * 0.55f, cy + r * 0.75f, arcPaint)
            // the joint dot
            fillPaint.color = colour
            canvas.drawCircle(cx + r * 0.15f, cy, r * 0.2f, fillPaint)
        }

        // The same three shapes the app and the tile draw — capsule, cradle,
        // stem — so the face, the tile and the app read as one thing.
        private fun drawMic(canvas: Canvas, cx: Float, cy: Float, r: Float, colour: Int) {
            val s = r * 1.1f                       // glyph box half-size
            val top = cy - s * 0.78f
            val capW = s * 0.62f
            val capH = s * 1.0f
            fillPaint.color = colour
            canvas.drawRoundRect(
                cx - capW / 2f, top, cx + capW / 2f, top + capH,
                capW / 2f, capW / 2f, fillPaint)
            arcPaint.color = colour
            arcPaint.strokeWidth = s * 0.16f
            val cradle = RectF(cx - s * 0.62f, cy - s * 0.52f,
                cx + s * 0.62f, cy + s * 0.72f)
            canvas.drawArc(cradle, 10f, 160f, false, arcPaint)
            canvas.drawLine(cx, cy + s * 0.72f, cx, cy + s * 1.0f, arcPaint)
        }
    }
}
