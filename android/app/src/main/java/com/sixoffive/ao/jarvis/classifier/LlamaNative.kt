package com.sixoffive.ao.jarvis.classifier

/** JNI handle into llama.cpp for the on-device intent classifier. */
internal object LlamaNative {
    init {
        // Already loaded by WhisperNative, but System.loadLibrary is
        // idempotent so this is safe and avoids ordering issues if the
        // classifier is constructed before STT.
        System.loadLibrary("jarvis_native")
    }

    /** @return native handle, or 0 on failure. */
    @JvmStatic external fun nativeInit(modelPath: String): Long
    @JvmStatic external fun nativeFree(handle: Long)

    /** Greedy generate up to `maxTokens` from `prompt`. Resets KV cache
     *  each call, so each prompt is independent. */
    @JvmStatic external fun nativeGenerate(
        handle: Long,
        prompt: String,
        maxTokens: Int,
    ): String
}
