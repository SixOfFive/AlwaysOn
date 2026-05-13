package com.sixoffive.ao.jarvis.stt

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * On-device STT via Android's [SpeechRecognizer].
 *
 * Behavior:
 *   - We loop SpeechRecognizer manually. The system recognizer's "continuous"
 *     mode isn't a thing in stock Android, so we start a new session every
 *     time the previous one finishes (end-of-speech or error). The user-facing
 *     effect is identical to continuous transcription.
 *   - Offline mode is requested via EXTRA_PREFER_OFFLINE. If the user has the
 *     offline language pack installed (Settings -> System -> Languages -> Voice
 *     -> Offline speech recognition) it stays fully on-device. Otherwise it
 *     silently falls back to the network recognizer.
 *
 * Must be created and used from a Handler-bound thread. We pin to the main
 * looper since SpeechRecognizer's callbacks already arrive there.
 */
class SystemStt(private val context: Context) : SpeechToText {

    private val handler = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null
    private var closed = false

    private val transcripts = MutableSharedFlow<String>(
        extraBufferCapacity = 16,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )

    override fun start(): Flow<String> {
        handler.post { startSession() }
        return transcripts.asSharedFlow()
    }

    override fun close() {
        closed = true
        handler.post {
            recognizer?.stopListening()
            recognizer?.destroy()
            recognizer = null
        }
    }

    private fun startSession() {
        if (closed) return
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            Log.w(TAG, "SpeechRecognizer not available on this device")
            return
        }
        val r = SpeechRecognizer.createSpeechRecognizer(context)
        recognizer = r
        r.setRecognitionListener(listener)

        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
            )
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
            // Ask the system to stay on-device when an offline pack is available.
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE,
                java.util.Locale.getDefault().toLanguageTag(),
            )
        }
        r.startListening(intent)
    }

    private fun restartSoon() {
        if (closed) return
        // Tiny delay so we don't tight-loop on errors. Also lets the system
        // recognizer fully tear down before we ask for a new session.
        handler.postDelayed({
            recognizer?.destroy()
            recognizer = null
            startSession()
        }, 150)
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}

        override fun onError(error: Int) {
            // Common: ERROR_NO_MATCH (silence), ERROR_SPEECH_TIMEOUT, ERROR_CLIENT.
            // Log only the loud ones to keep logcat clean.
            if (error !in setOf(
                    SpeechRecognizer.ERROR_NO_MATCH,
                    SpeechRecognizer.ERROR_SPEECH_TIMEOUT,
                )
            ) {
                Log.w(TAG, "SpeechRecognizer error: $error")
            }
            restartSoon()
        }

        override fun onResults(results: Bundle?) {
            val matches = results
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                .orEmpty()
            val best = matches.firstOrNull()?.trim().orEmpty()
            if (best.isNotEmpty()) {
                transcripts.tryEmit(best)
            }
            restartSoon()
        }

        override fun onPartialResults(partialResults: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    companion object {
        private const val TAG = "SystemStt"
    }
}
