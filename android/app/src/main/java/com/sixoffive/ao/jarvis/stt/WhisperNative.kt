package com.sixoffive.ao.jarvis.stt

/** Low-level JNI handles for whisper.cpp. Always loaded via [WhisperStt]. */
internal object WhisperNative {
    init {
        System.loadLibrary("jarvis_native")
    }

    /** @return native context handle, or 0 on failure. */
    @JvmStatic external fun nativeInit(modelPath: String): Long

    @JvmStatic external fun nativeFree(ctxHandle: Long)

    /**
     * @param audio 16 kHz mono PCM normalized to [-1, 1]
     * @return transcript (may be empty)
     */
    @JvmStatic external fun nativeTranscribe(
        ctxHandle: Long,
        audio: FloatArray,
        nThreads: Int,
        languageCode: String,
    ): String
}
