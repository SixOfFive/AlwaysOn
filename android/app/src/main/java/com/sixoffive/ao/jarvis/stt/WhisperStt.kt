package com.sixoffive.ao.jarvis.stt

import android.content.Context
import android.util.Log
import com.sixoffive.ao.jarvis.audio.AudioCapture
import com.sixoffive.ao.jarvis.audio.VadSegmenter
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * Always-on STT: continuous AudioRecord -> Silero VAD -> whisper.cpp.
 *
 * Equivalent of `client/jarvis_client/listen.py` on the Python side.
 * No gap between utterances — the mic never stops; VAD just decides when
 * an utterance is "done" enough to hand to Whisper.
 */
class WhisperStt(
    private val context: Context,
    private val modelName: String = ModelStore.DEFAULT_MODEL,
    private val language: String = "en",
    /** Called ~10x/sec with (peak amplitude 0..32767, VAD prob 0..1).
     *  Used by the UI to render live meters. */
    private val onMetric: ((peak: Int, prob: Float) -> Unit)? = null,
) : SpeechToText {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var pipeline: Job? = null
    private var capture: AudioCapture? = null
    private var segmenter: VadSegmenter? = null
    @Volatile private var ctxHandle: Long = 0L
    private val whisperLock = Mutex()  // whisper.cpp ctx isn't thread-safe

    private val transcripts = MutableSharedFlow<String>(
        extraBufferCapacity = 16,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )

    override fun start(): Flow<String> {
        if (pipeline != null) return transcripts.asSharedFlow()

        pipeline = scope.launch {
            val store = ModelStore(context)
            val modelFile = store.modelFile(modelName)
            if (!modelFile.exists()) {
                Log.e(TAG, "model not found: ${modelFile.absolutePath} — download it via ModelStore first")
                return@launch
            }

            ctxHandle = withContext(Dispatchers.IO) {
                WhisperNative.nativeInit(modelFile.absolutePath)
            }
            if (ctxHandle == 0L) {
                Log.e(TAG, "whisper init failed")
                return@launch
            }
            Log.i(TAG, "whisper ready: $modelName")

            val cap = AudioCapture().also { capture = it }
            val seg = VadSegmenter(context).also { segmenter = it }

            var chunksSinceMetric = 0
            var peakSinceMetric = 0
            cap.stream(context).collect { chunk ->
                // Cheap peak scan for the live meter.
                if (onMetric != null) {
                    for (s in chunk) {
                        val a = if (s < 0) -s.toInt() else s.toInt()
                        if (a > peakSinceMetric) peakSinceMetric = a
                    }
                    chunksSinceMetric++
                    // Emit every ~4 chunks (~128 ms) → ~8 Hz UI refresh.
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
                launch {
                    try { transcribe(segment) }
                    catch (exc: Throwable) { Log.w(TAG, "transcribe failed", exc) }
                }
            }
        }
        return transcripts.asSharedFlow()
    }

    private suspend fun transcribe(audio: FloatArray) {
        if (audio.size < 4_000) {
            Log.i(TAG, "segment too short (${audio.size} samples), dropping")
            return
        }
        whisperLock.withLock {
            val handle = ctxHandle
            if (handle == 0L) return
            val t0 = System.currentTimeMillis()
            val text = withContext(Dispatchers.Default) {
                WhisperNative.nativeTranscribe(
                    handle,
                    audio,
                    /* nThreads = */ Runtime.getRuntime().availableProcessors().coerceAtMost(4),
                    language,
                )
            }
            val elapsed = System.currentTimeMillis() - t0
            val cleaned = text.trim()
            Log.i(TAG, "transcribe: ${audio.size} samples in ${elapsed}ms -> ${cleaned.length} chars")
            if (cleaned.isNotEmpty()) {
                transcripts.tryEmit(cleaned)
            }
        }
    }

    override fun close() {
        pipeline?.cancel()
        pipeline = null
        segmenter?.close()
        segmenter = null
        capture = null
        val handle = ctxHandle
        if (handle != 0L) {
            ctxHandle = 0L
            WhisperNative.nativeFree(handle)
        }
        scope.cancel()
    }

    companion object {
        private const val TAG = "WhisperStt"
    }
}
