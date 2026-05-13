package com.sixoffive.ao.jarvis.stt

import android.content.Context
import android.util.Log
import com.sixoffive.ao.jarvis.audio.AudioCapture
import com.sixoffive.ao.jarvis.audio.VadSegmenter
import com.sixoffive.ao.jarvis.net.JarvisWsClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.launch
import okio.ByteString.Companion.toByteString
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Server-side STT: audio in, transcript out — over the WebSocket.
 *
 * Runs AudioCapture + Silero VAD locally, then ships each end-of-speech
 * segment to the server as Wake → PCM payload → EndUtterance. The server
 * transcribes on CUDA-backed faster-whisper, applies the wake-word
 * trigger, and sends back Transcript / Say through the WS event flow.
 *
 * Why one-shot send instead of streaming chunk-by-chunk: VadSegmenter's
 * pre-roll buffer captures the few hundred ms before the prob crosses
 * the speech threshold, which is essential to avoid clipping the first
 * phoneme. Streaming would either clip that or require duplicating the
 * pre-roll logic. And faster-whisper isn't streaming anyway — it needs
 * the full segment. On LAN, a 5 s utterance is ~160 KB which uploads in
 * under 2 ms, so there's nothing to gain from chunked sends.
 *
 * Because transcripts arrive asynchronously through [JarvisWsClient.events],
 * [start] returns an empty Flow. The service routes server transcripts
 * directly from the ws event stream.
 */
class ServerStreamingStt(
    private val context: Context,
    private val ws: JarvisWsClient,
    /** Called ~8x/sec with (peak amplitude 0..32767, VAD prob 0..1).
     *  Used by the UI to render live meters. */
    private val onMetric: ((peak: Int, prob: Float) -> Unit)? = null,
    /** Returns true while TTS is playing AND the server requested
     *  mic-mute on that Say. Chunks are dropped entirely (not fed to
     *  VAD, not sent to the server) so the assistant can't transcribe
     *  its own voice. */
    private val isSpeaking: () -> Boolean = { false },
) : SpeechToText {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var pipeline: Job? = null
    private var segmenter: VadSegmenter? = null

    override fun start(): Flow<String> {
        if (pipeline != null) return emptyFlow()

        pipeline = scope.launch {
            val cap = AudioCapture()
            val seg = VadSegmenter(context).also { segmenter = it }

            var chunksSinceMetric = 0
            var peakSinceMetric = 0

            cap.stream(context).collect { chunk ->
                // Drop chunks entirely while TTS is playing (when the
                // server requested mute_mic on the Say — default true).
                // Don't feed VAD, don't ship — the mic effectively goes
                // dark for the spoken reply. Mid-thinking / mid-routing
                // chunks still flow; the server's utterance queue keeps
                // them ordered behind any in-flight reply.
                if (isSpeaking()) return@collect

                // Live mic meter — cheap peak over the chunk.
                if (onMetric != null) {
                    var peak = 0
                    for (s in chunk) {
                        val a = if (s < 0) -s.toInt() else s.toInt()
                        if (a > peak) peak = a
                    }
                    if (peak > peakSinceMetric) peakSinceMetric = peak
                    chunksSinceMetric++
                    if (chunksSinceMetric >= 4) {
                        onMetric.invoke(peakSinceMetric, seg.lastProb)
                        chunksSinceMetric = 0
                        peakSinceMetric = 0
                    }
                }

                val segment = try {
                    seg.feed(chunk)
                } catch (exc: Throwable) {
                    Log.w(TAG, "VAD threw on a chunk; dropping it", exc)
                    null
                } ?: return@collect

                // VAD just declared end-of-speech. Ship the segment.
                val bytes = floatToLePcmBytes(segment)
                Log.i(TAG, "ship segment: ${segment.size} samples = ${bytes.size} bytes")
                ws.sendWake(keyword = "", confidence = seg.lastProb)
                ws.sendAudio(bytes)
                ws.sendEndUtterance()
            }
        }
        return emptyFlow()
    }

    override fun close() {
        pipeline?.cancel()
        pipeline = null
        segmenter?.close()
        segmenter = null
        scope.cancel()
    }

    companion object {
        private const val TAG = "ServerStreamingStt"

        /** Convert VadSegmenter's float32 [-1, 1] segment to int16 LE PCM,
         *  the wire format the server expects. */
        private fun floatToLePcmBytes(samples: FloatArray): ByteArray {
            val out = ByteBuffer.allocate(samples.size * 2).order(ByteOrder.LITTLE_ENDIAN)
            for (s in samples) {
                val clipped = (s.coerceIn(-1.0f, 1.0f) * 32767.0f).toInt()
                out.putShort(clipped.toShort())
            }
            return out.array()
        }
    }
}
