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
    private val speechThreshold: Float = 0.4f,
    private val minSilenceMs: Int = 700,
    private val speechPadMs: Int = 200,
    private val maxSegmentMs: Int = 20_000,
) : AutoCloseable {

    private var chunksSeen = 0
    private var probSum = 0.0f
    private var probMax = 0.0f

    /** Last speech probability returned by Silero, for the UI level meter. */
    @Volatile var lastProb: Float = 0.0f
        private set

    private val env: OrtEnvironment = OrtEnvironment.getEnvironment()
    private val session: OrtSession

    // Silero ONNX state: h, c — 2x batched RNN hidden state.
    private val stateShape = longArrayOf(2, 1, 128)
    private var state: OnnxTensor = OnnxTensor.createTensor(
        env,
        FloatBuffer.wrap(FloatArray(2 * 1 * 128)),
        stateShape,
    )
    // Silero's `sr` is a scalar (rank-0 tensor), not a 1-D tensor with one
    // element. Passing it as [1] crashes ORT with a segfault on ARM.
    private val srTensor: OnnxTensor =
        OnnxTensor.createTensor(
            env,
            LongBuffer.wrap(longArrayOf(SAMPLE_RATE.toLong())),
            longArrayOf(),
        )

    // Silero v5 expects a 64-sample (for 16 kHz) "context" window prepended
    // to each chunk so the model sees a continuous waveform across calls.
    // See silero-vad utils_vad.py: x = torch.cat([self._context, x], dim=1).
    // Without this, every chunk looks discontinuous and the model produces
    // near-zero speech probabilities even on loud speech.
    private val contextSize = 64
    private var context = FloatArray(contextSize)

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
        if (chunk.size != CHUNK_SAMPLES) return null  // partial chunk; skip

        // Maintain a rolling pre-roll buffer of the most recent chunks while
        // we're idle, so the first phoneme isn't clipped on speech-start.
        if (!inSpeech) {
            preRoll.addLast(chunk)
            while (preRoll.size > preRollMax) preRoll.removeFirst()
        }

        val prob = run(chunk)
        lastProb = prob
        val isSpeech = prob >= speechThreshold

        // Periodic diagnostic — every ~1s of audio, log avg & max prob.
        chunksSeen++
        probSum += prob
        if (prob > probMax) probMax = prob
        if (chunksSeen >= 32) {
            Log.i(TAG, "vad over last ~1s: avg=%.3f max=%.3f thr=%.2f"
                .format(probSum / chunksSeen, probMax, speechThreshold))
            chunksSeen = 0; probSum = 0.0f; probMax = 0.0f
        }

        if (isSpeech) {
            if (!inSpeech) {
                inSpeech = true
                buffer.clear()
                // Include pre-roll (already contains the current chunk).
                for (c in preRoll) {
                    for (s in c) buffer.add(s)
                }
                segmentMs = preRoll.size * CHUNK_MS
                preRoll.clear()
                Log.i(TAG, "speech start (prob=%.2f)".format(prob))
            } else {
                for (s in chunk) buffer.add(s)
                segmentMs += CHUNK_MS
            }
            silentStreakMs = 0
        } else if (inSpeech) {
            for (s in chunk) buffer.add(s)
            segmentMs += CHUNK_MS
            silentStreakMs += CHUNK_MS
        }

        if (inSpeech && (silentStreakMs >= minSilenceMs || segmentMs >= maxSegmentMs)) {
            val durationMs = segmentMs
            val segment = finish()
            Log.i(TAG, "speech end: ${durationMs}ms, ${segment.size} samples")
            return segment
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
        // Reset Silero state and context between utterances.
        state.close()
        state = OnnxTensor.createTensor(
            env,
            FloatBuffer.wrap(FloatArray(2 * 1 * 128)),
            stateShape,
        )
        context = FloatArray(contextSize)
        return out
    }

    private fun run(chunk: ShortArray): Float {
        // Build [context(64) || chunk(512)] = 576 samples, float32 [-1, 1].
        val input = FloatArray(contextSize + chunk.size)
        System.arraycopy(context, 0, input, 0, contextSize)
        for (i in chunk.indices) {
            input[contextSize + i] = chunk[i].toInt().toFloat() / 32768.0f
        }
        // Carry the last 64 samples forward as the next call's context.
        System.arraycopy(input, input.size - contextSize, context, 0, contextSize)

        val inputTensor = OnnxTensor.createTensor(
            env,
            FloatBuffer.wrap(input),
            longArrayOf(1, input.size.toLong()),
        )
        val feeds = mapOf(
            "input" to inputTensor,
            "state" to state,
            "sr" to srTensor,
        )
        val results = session.run(feeds)
        try {
            val rawOut = results[0].value
            val prob = when (rawOut) {
                is Array<*> -> ((rawOut[0] as FloatArray)[0])
                else -> 0.0f
            }

            // Copy stateN OUT before results.close() — we'd be using freed
            // memory next iteration otherwise. The state tensor's lifetime
            // is owned by `results`, not by us.
            val newStateTensor = results[1] as OnnxTensor
            val flat = FloatArray(2 * 1 * 128)
            newStateTensor.floatBuffer.get(flat)
            state.close()
            state = OnnxTensor.createTensor(
                env,
                FloatBuffer.wrap(flat),
                stateShape,
            )
            return prob
        } finally {
            inputTensor.close()
            results.close()
        }
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
