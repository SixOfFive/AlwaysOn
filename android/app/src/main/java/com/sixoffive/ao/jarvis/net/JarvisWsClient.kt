package com.sixoffive.ao.jarvis.net

import android.os.Build
import android.util.Log
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import java.util.concurrent.TimeUnit

/**
 * Single-connection WebSocket client. Speaks the same JSON protocol as
 * the Python client. Reconnect logic is intentionally minimal — the
 * service starts/stops this; if the connection drops we surface it and
 * let the user retry.
 */
class JarvisWsClient(
    private val url: String,
    private val clientId: String,
) {
    private val http = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.SECONDS)   // ws stays open indefinitely
        .pingInterval(20, TimeUnit.SECONDS) // keepalive
        .build()

    private var ws: WebSocket? = null
    @Volatile private var ready = false

    private val _events = MutableSharedFlow<Event>(
        extraBufferCapacity = 16,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    val events: Flow<Event> get() = _events.asSharedFlow()

    sealed interface Event {
        data class Welcomed(val sessionId: String) : Event
        data class Transcribed(val text: String, val final: Boolean) : Event
        /** `muteMic` tells the service to drop mic chunks for the
         *  duration of TTS playback so it doesn't transcribe its own voice. */
        data class Said(val text: String, val muteMic: Boolean) : Event
        data class Thinking(val note: String) : Event
        data class Errored(val code: String, val message: String) : Event
        data class Disconnected(val reason: String) : Event
        /** Server confirmed the conversation history was cleared in
         *  response to our sendResetContext(). UI can now reset its log. */
        object ContextCleared : Event
    }

    fun connect() {
        val req = Request.Builder().url(url).build()
        ws = http.newWebSocket(req, listener)
    }

    fun close() {
        ready = false
        ws?.close(1000, "client close")
        ws = null
    }

    /** Send a Command. No-op if the connection isn't ready yet. */
    fun sendCommand(text: String): Boolean {
        val w = ws ?: return false
        if (!ready) return false
        val json = protocolJson.encodeToString(Outgoing.serializer(), Command(text))
        return w.send(json)
    }

    /** Open an audio utterance. Send raw PCM bytes after this, then
     *  sendEndUtterance() to trigger server-side STT. `keyword` is the
     *  wake-word the client matched on; pass "" to ask the server to
     *  apply its own transcript-based trigger check. */
    fun sendWake(keyword: String = "", confidence: Float = 0.0f): Boolean {
        val w = ws ?: return false
        if (!ready) return false
        val msg = Wake(keyword, confidence, System.currentTimeMillis())
        val json = protocolJson.encodeToString(Outgoing.serializer(), msg)
        return w.send(json)
    }

    /** Send a raw PCM audio chunk (int16 little-endian, 16 kHz mono).
     *  Only meaningful between sendWake() and sendEndUtterance(). */
    fun sendAudio(pcm: ByteArray): Boolean {
        val w = ws ?: return false
        if (!ready) return false
        return w.send(ByteString.of(*pcm))
    }

    /** Close an audio utterance and ask the server to transcribe. */
    fun sendEndUtterance(): Boolean {
        val w = ws ?: return false
        if (!ready) return false
        val json = protocolJson.encodeToString(Outgoing.serializer(), EndUtterance())
        return w.send(json)
    }

    /** Ask the server to drop its conversation history for this
     *  session. Server replies with `ContextCleared` once done. */
    fun sendResetContext(): Boolean {
        val w = ws ?: return false
        if (!ready) return false
        val json = protocolJson.encodeToString(Outgoing.serializer(), ResetContext())
        return w.send(json)
    }

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            val hello = Hello(clientId = clientId, hostname = Build.MODEL)
            webSocket.send(
                protocolJson.encodeToString(Outgoing.serializer(), hello),
            )
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val msg = try {
                protocolJson.decodeFromString(Incoming.serializer(), text)
            } catch (exc: Exception) {
                Log.w(TAG, "bad frame: $text", exc)
                return
            }
            when (msg) {
                is Welcome -> {
                    ready = true
                    _events.tryEmit(Event.Welcomed(msg.sessionId))
                }
                is Say -> _events.tryEmit(Event.Said(msg.text, msg.muteMic))
                is Thinking -> _events.tryEmit(Event.Thinking(msg.note))
                is ErrorMsg -> _events.tryEmit(Event.Errored(msg.code, msg.message))
                is Transcript -> _events.tryEmit(Event.Transcribed(msg.text, msg.final))
                is ContextCleared -> _events.tryEmit(Event.ContextCleared)
                is Pong -> { /* keepalive */ }
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            // server doesn't send binary today
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            ready = false
            webSocket.close(code, reason)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            ready = false
            _events.tryEmit(Event.Disconnected("closed: $code $reason"))
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            ready = false
            _events.tryEmit(Event.Disconnected("failure: ${t.message}"))
        }
    }

    companion object {
        private const val TAG = "JarvisWsClient"
    }
}
