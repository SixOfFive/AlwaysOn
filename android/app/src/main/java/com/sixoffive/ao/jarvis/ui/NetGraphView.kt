package com.sixoffive.ao.jarvis.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.view.View

/**
 * Minimal scrolling strip chart for WebSocket throughput.
 *
 * Holds a ring buffer of the last [HISTORY] one-second samples (upload
 * and download bytes/sec), draws them as two stacked line plots, and
 * shows the current rates plus the in-window peak as a small text
 * overlay. Y-axis auto-scales to the recent max so a quiet line stays
 * readable and a busy one doesn't clip.
 *
 * Push samples at 1 Hz from [push]. The view invalidates itself so
 * the caller doesn't need to.
 */
class NetGraphView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : View(context, attrs, defStyleAttr) {

    private val upSamples = LongArray(HISTORY)
    private val downSamples = LongArray(HISTORY)
    private var head = 0   // next index to overwrite
    private var filled = 0 // total samples ever pushed (clamped at HISTORY for window)

    private val upPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#80D8FF")  // cyan-ish
        style = Paint.Style.STROKE
        strokeWidth = 2.5f
        strokeJoin = Paint.Join.ROUND
    }
    private val downPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#69F0AE")  // green
        style = Paint.Style.STROKE
        strokeWidth = 2.5f
        strokeJoin = Paint.Join.ROUND
    }
    private val gridPaint = Paint().apply {
        color = Color.parseColor("#22FFFFFF")
        style = Paint.Style.STROKE
        strokeWidth = 1f
    }
    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#CCFFFFFF")
        textSize = 26f
        isFakeBoldText = false
    }
    private val labelPaintUp = Paint(labelPaint).apply { color = upPaint.color }
    private val labelPaintDown = Paint(labelPaint).apply { color = downPaint.color }

    fun push(uploadBps: Long, downloadBps: Long) {
        upSamples[head] = uploadBps
        downSamples[head] = downloadBps
        head = (head + 1) % HISTORY
        if (filled < HISTORY) filled++
        invalidate()
    }

    /** Wipe the window. Useful on disconnect or when the activity reattaches. */
    fun clear() {
        for (i in 0 until HISTORY) {
            upSamples[i] = 0L
            downSamples[i] = 0L
        }
        head = 0
        filled = 0
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0f || h <= 0f) return

        // Reserve a small strip at the top for labels.
        val labelStrip = 32f
        val plotTop = labelStrip
        val plotBottom = h - 4f
        val plotH = plotBottom - plotTop

        // Light grid: 4 horizontal divisions.
        for (i in 1..3) {
            val y = plotTop + plotH * i / 4f
            canvas.drawLine(0f, y, w, y, gridPaint)
        }

        // Auto-scale Y to the max in the current window. Floor at 1 KB/s
        // so an idle graph still has a sensible "ceiling" instead of
        // scaling pure noise up to fill the view.
        var maxBps = 1024L
        for (i in 0 until HISTORY) {
            if (upSamples[i] > maxBps) maxBps = upSamples[i]
            if (downSamples[i] > maxBps) maxBps = downSamples[i]
        }

        if (filled >= 2) {
            val count = filled
            val xStep = if (count > 1) w / (count - 1) else 0f
            val upPath = Path()
            val downPath = Path()
            // Oldest sample sits at the leftmost x; newest at the right.
            // The ring buffer's oldest is at (head - filled + HISTORY) % HISTORY.
            val start = (head - count + HISTORY) % HISTORY
            for (i in 0 until count) {
                val idx = (start + i) % HISTORY
                val x = i * xStep
                val upY = plotBottom - upSamples[idx].toFloat() / maxBps.toFloat() * plotH
                val downY = plotBottom - downSamples[idx].toFloat() / maxBps.toFloat() * plotH
                if (i == 0) {
                    upPath.moveTo(x, upY)
                    downPath.moveTo(x, downY)
                } else {
                    upPath.lineTo(x, upY)
                    downPath.lineTo(x, downY)
                }
            }
            canvas.drawPath(downPath, downPaint)
            canvas.drawPath(upPath, upPaint)
        }

        // Header text: current up / down / peak.
        val currentUp = if (filled > 0) upSamples[(head - 1 + HISTORY) % HISTORY] else 0L
        val currentDown = if (filled > 0) downSamples[(head - 1 + HISTORY) % HISTORY] else 0L
        val y = 24f
        canvas.drawText("↑", 6f, y, labelPaintUp)
        canvas.drawText(fmt(currentUp), 26f, y, labelPaint)
        val downX = w / 3f
        canvas.drawText("↓", downX, y, labelPaintDown)
        canvas.drawText(fmt(currentDown), downX + 20f, y, labelPaint)
        val peakX = 2f * w / 3f
        canvas.drawText("peak", peakX, y, labelPaint)
        canvas.drawText(fmt(maxBps), peakX + 64f, y, labelPaint)
    }

    private fun fmt(bps: Long): String {
        if (bps < 1024) return "${bps} B/s"
        if (bps < 1024 * 1024) {
            val kb = bps / 1024.0
            return if (kb >= 100) "${kb.toInt()} KB/s" else "%.1f KB/s".format(kb)
        }
        val mb = bps / 1048576.0
        return "%.2f MB/s".format(mb)
    }

    companion object {
        /** 60 samples × 1 s = 1-minute strip chart. */
        const val HISTORY = 60
    }
}
