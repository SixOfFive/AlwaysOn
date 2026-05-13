package com.sixoffive.ao.jarvis.audio

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import android.util.Log
import java.nio.FloatBuffer
import java.nio.LongBuffer

/**
 * Speech segmenter via Silero VAD (ONNX). Equivalent of the
 * `client/jarvis_client/segmenter.py` SpeechSegmenter, ported to Kotlin.
 *
 * Feed it 512-sample @ 16 kHz int16 chunks; it returns a finished segment
 * (FloatArray normalized to [-1, 1]) when speech ends, otherwise null.
 *
 * Silero is stateful — RNN hidden state is carried across calls.
 */
class VadSegmenter(
    context: Context,
    private val speechThreshold: Float = 0.5f,
    private val minSilenceMs: Int = 700,
    private val speechPadMs: Int = 200,
    private val maxSegmentMs: Int = 20_000,
) : AutoCloseable {

    private val env: OrtEnvironment = OrtEnvironment.getEnvironment()
    private val session: OrtSession

    // Silero ONNX state: h, c — 2x batched RNN hidden state.
    private val stateShape = longArrayOf(2, 1, 128)
    private var state: OnnxTensor = OnnxTensor.createTensor(
        env,
        FloatBuffer.wrap(FloatArray(2 * 1 * 128)),
        stateShape,
    )
    private val srTensor: OnnxTensor =
        OnnxTensor.createTensor(env, LongBuffer.wrap(longArrayOf(SAMPLE_RATE.toLong())), longArrayOf(1))

    private val preRoll: ArrayDeque<ShortArray> = ArrayDeque()
    private val preRollMax: Int = maxOf(1, speechPadMs / CHUNK_MS)

    private val buffer = ArrayList<Short>(SAMPLE_RATE * 5)
    private var inSpeech = false
    private var silentStreakMs = 0
    private var segmentMs = 0

    init {
        // Copy silero_vad.onnx from assets to a file ORT can mmap.
        val onnxBytes = context.assets.open("silero_vad.onnx").use { it.readBytes() }
        session = env.createSession(onnxBytes)
        Log.i(TAG, "silero-vad loaded (silence>=${minSilenceMs}ms ends a segment)")
    }

    /** @return finished segment (16 kHz float32 in [-1,1]) when speech ends, else null. */
    fun feed(chunk: ShortArray): FloatArray? {
        require(chunk.size == CHUNK_SAMPLES) { "expected $CHUNK_SAMPLES samples, got ${chunk.size}" }

        if (!inSpeech) {
            preRoll.addLast(chunk)
            while (preRoll.size > preRollMax) preRoll.removeFirst()
        }

        val prob = run(chunk)
        val isSpeech = prob >= speechThreshold

        if (isSpeech) {
            if (!inSpeech) {
                inSpeech = true
                buffer.clear()
                // Include pre-roll so we don't clip the first phoneme.
                for (c in preRoll) {
                    for (s in c) buffer.add(s)
                }
                segmentMs = preRoll.size * CHUNK_MS
                preRoll.clear()
            }
            for (s in chunk) buffer.add(s)
            segmentMs += CHUNK_MS
            silentStreakMs = 0
        } else if (inSpeech) {
            for (s in chunk) buffer.add(s)
            segmentMs += CHUNK_MS
            silentStreakMs += CHUNK_MS
        }

        if (inSpeech && (silentStreakMs >= minSilenceMs || segmentMs >= maxSegmentMs)) {
            return finish()
        }
        return null
    }

    private fun finish(): FloatArray {
        val out = FloatArray(buffer.size) { i ->
            buffer[i].toInt().toFloat() / 32768.0f
        }
        buffer.clear()
        inSpeech = false
        silentStreakMs = 0
        segmentMs = 0
        preRoll.clear()
        // Reset Silero state between utterances.
        state.close()
        state = OnnxTensor.createTensor(
            env,
            FloatBuffer.wrap(FloatArray(2 * 1 * 128)),
            stateShape,
        )
        return out
    }

    private fun run(chunk: ShortArray): Float {
        val input = FloatArray(chunk.size) { i -> chunk[i].toInt().toFloat() / 32768.0f }
        val inputTensor = OnnxTensor.createTensor(
            env,
            FloatBuffer.wrap(input),
            longArrayOf(1, chunk.size.toLong()),
        )
        val feeds = mapOf(
            "input" to inputTensor,
            "state" to state,
            "sr" to srTensor,
        )
        val results = session.run(feeds)
        val prob = try {
            val raw = results[0].value
            when (raw) {
                is Array<*> -> ((raw[0] as FloatArray)[0])
                else -> 0.0f
            }
        } finally {
            // results[0] is the prob; results[1] is the new state.
            val newState = results[1] as OnnxTensor
            state.close()
            state = newState
            results.close()
            inputTensor.close()
        }
        return prob
    }

    override fun close() {
        try { state.close() } catch (_: Exception) {}
        try { srTensor.close() } catch (_: Exception) {}
        try { session.close() } catch (_: Exception) {}
    }

    companion object {
        private const val TAG = "VadSegmenter"
        const val SAMPLE_RATE = 16_000
        const val CHUNK_SAMPLES = 512
        const val CHUNK_MS = 32
    }
}
