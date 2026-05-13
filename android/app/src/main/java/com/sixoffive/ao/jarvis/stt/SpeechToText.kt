package com.sixoffive.ao.jarvis.stt

import kotlinx.coroutines.flow.Flow

/**
 * Streaming STT abstraction. Two implementations planned:
 *   - [SystemStt]: Android SpeechRecognizer (on-device when offline pack
 *     installed; falls back to network otherwise). Default.
 *   - WhisperStt (later): whisper.cpp via JNI for guaranteed-offline,
 *     phone-agnostic transcription.
 *
 * Implementations emit final transcripts. Partial / interim results are
 * intentionally not surfaced; the trigger logic only fires on finals.
 */
interface SpeechToText {
    /** Start listening. The returned Flow emits one item per completed
     *  utterance until [close] is called. */
    fun start(): Flow<String>

    /** Stop listening and release native resources. */
    fun close()
}
