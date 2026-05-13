package com.sixoffive.ao.jarvis.tts

import android.content.Context
import android.os.Build
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import kotlinx.coroutines.suspendCancellableCoroutine
import java.util.Locale
import kotlin.coroutines.resume

/**
 * Wraps Android's TextToSpeech in a coroutine-friendly API.
 *
 * On modern Android the system has on-device TTS engines (Google's Speech
 * Services, Samsung's, etc.), so this is local and free. The first speak()
 * may take a beat while the engine initializes.
 */
class Tts(context: Context) {

    private val ready = kotlinx.coroutines.CompletableDeferred<Boolean>()

    private val engine: TextToSpeech = TextToSpeech(context.applicationContext) { status ->
        if (status == TextToSpeech.SUCCESS) {
            ready.complete(true)
        } else {
            Log.w(TAG, "TTS init failed: status=$status")
            ready.complete(false)
        }
    }.apply {
        language = Locale.getDefault()
    }

    suspend fun say(text: String) {
        if (text.isBlank()) return
        if (!ready.await()) return

        // Each utterance gets a unique id so we can await completion.
        val id = "u-${System.nanoTime()}"
        suspendCancellableCoroutine<Unit> { cont ->
            engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) {}
                override fun onDone(utteranceId: String?) {
                    if (utteranceId == id && cont.isActive) cont.resume(Unit)
                }
                @Deprecated("required by base class")
                override fun onError(utteranceId: String?) {
                    if (utteranceId == id && cont.isActive) cont.resume(Unit)
                }
                override fun onError(utteranceId: String?, errorCode: Int) {
                    if (utteranceId == id && cont.isActive) cont.resume(Unit)
                }
            })

            val params = Bundle()
            @Suppress("DEPRECATION") // TextToSpeech.Engine.KEY_PARAM_UTTERANCE_ID is current
            val result = engine.speak(text, TextToSpeech.QUEUE_FLUSH, params, id)
            if (result != TextToSpeech.SUCCESS && cont.isActive) {
                cont.resume(Unit)
            }
            cont.invokeOnCancellation { engine.stop() }
        }
    }

    fun shutdown() {
        engine.stop()
        engine.shutdown()
    }

    companion object {
        private const val TAG = "Tts"
    }
}
