package com.sixoffive.ao.jarvis.stt

/** Low-level JNI handles for whisper.cpp. Always loaded via [WhisperStt]. */
internal object WhisperNative {
    init {
        System.loadLibrary("jarvis_native")
    }

    /** Called from whisper.cpp's worker thread during decoding. */
    interface Listener {
        /** progress 0..100 — fires several times per transcribe(). */
        fun onProgress(percent: Int)
    }

    /** @return native context handle, or 0 on failure. */
    @JvmStatic external fun nativeInit(modelPath: String): Long

    @JvmStatic external fun nativeFree(ctxHandle: Long)

    /**
     * @param audio 16 kHz mono PCM normalized to [-1, 1]
     * @param listener optional; receives progress updates from native side
     * @return transcript (may be empty)
     */
    @JvmStatic external fun nativeTranscribe(
        ctxHandle: Long,
        audio: FloatArray,
        nThreads: Int,
        languageCode: String,
        listener: Listener?,
    ): String
}
