package com.sixoffive.ao.jarvis

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.sixoffive.ao.jarvis.classifier.Classifier
import com.sixoffive.ao.jarvis.net.JarvisWsClient
import com.sixoffive.ao.jarvis.stt.ModelStore
import com.sixoffive.ao.jarvis.stt.ServerStreamingStt
import com.sixoffive.ao.jarvis.stt.SpeechToText
import com.sixoffive.ao.jarvis.stt.SystemStt
import com.sixoffive.ao.jarvis.stt.TranscriptLog
import com.sixoffive.ao.jarvis.stt.WhisperStt
import com.sixoffive.ao.jarvis.trigger.Trigger
import com.sixoffive.ao.jarvis.tts.Tts
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * Foreground service that keeps the mic alive while the screen is locked.
 * Owns the STT, the WebSocket connection, the trigger logic, and TTS.
 *
 * Lifecycle:
 *   - Start with [start]. The persistent notification appears.
 *   - The activity observes [events] for the in-app transcript log.
 *   - Stop with [stop] or via the notification's "stop" action.
 */
class JarvisService : Service() {

    private val scope = CoroutineScope(SupervisorJob())
    private var stt: SpeechToText? = null
    private var ws: JarvisWsClient? = null
    private var tts: Tts? = null
    private var pipelineJob: Job? = null
    private var transcriptLog: TranscriptLog? = null
    private var classifier: Classifier? = null
    // Serialize TTS playback so back-to-back Say events don't overlap.
    // We no longer mute the mic during TTS — see the comments in
    // ServerStreamingStt and the speech.start() collect block.
    private val speakLock = Mutex()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelfCleanly()
                return START_NOT_STICKY
            }
            ACTION_START -> {
                val server = intent.getStringExtra(EXTRA_SERVER_URL).orEmpty()
                val engine = intent.getStringExtra(EXTRA_STT_ENGINE) ?: ENGINE_WHISPER
                startListening(server, engine)
                return START_STICKY
            }
            ACTION_RESET_CONTEXT -> {
                val sent = ws?.sendResetContext() ?: false
                if (!sent) {
                    // Not connected (or no ws yet) — there's nothing on the
                    // server to clear, but the user wants a clean slate, so
                    // emit the ack locally so the UI clears anyway.
                    Log.i(TAG, "reset_context: not connected — emitting local ack")
                    _events.tryEmit(UiEvent.ContextCleared)
                    _events.tryEmit(UiEvent.Status("context cleared (offline)"))
                }
                return START_STICKY
            }
        }
        return START_NOT_STICKY
    }

    private fun startListening(serverUrl: String, engine: String) {
        if (pipelineJob != null) return  // already running

        createNotificationChannel()
        startForeground(NOTIF_ID, buildNotification(), micForegroundType())

        transcriptLog = TranscriptLog(applicationContext)
        tts = Tts(applicationContext)

        // CLASSIFIER DISABLED for now. Even keeping the 500 MB GGUF model
        // file mmap'd on disk + a 1 GB resident llama context appears to
        // pressure whisper inference into pathological slowdown on this
        // hardware (5x slower than realtime). Until we find a lighter
        // classifier (sentence embeddings or similar), fall back to the
        // regex Trigger. The model file stays on disk — just not loaded.
        //
        // scope.launch {
        //     val store = ModelStore(applicationContext)
        //     if (!store.classifierIsCached()) {
        //         Log.w(TAG, "classifier model not present — using regex fallback")
        //         return@launch
        //     }
        //     val cls = Classifier.load(store.classifierFile.absolutePath)
        //     if (cls == null) {
        //         Log.w(TAG, "classifier failed to load — using regex fallback")
        //         return@launch
        //     }
        //     classifier = cls
        //     _events.tryEmit(UiEvent.Status("classifier loaded"))
        // }
        val client = if (serverUrl.isNotBlank()) {
            JarvisWsClient(serverUrl, clientId = Build.MODEL ?: "android-mic").also { it.connect() }
        } else {
            null
        }
        ws = client

        val speech: SpeechToText = when (engine) {
            ENGINE_SYSTEM -> SystemStt(applicationContext)
            ENGINE_WHISPER -> WhisperStt(
                applicationContext,
                onMetric = { peak, prob ->
                    _events.tryEmit(UiEvent.AudioMetric(peak, prob))
                },
                onProgress = { pct ->
                    _events.tryEmit(UiEvent.Status("transcribing $pct%"))
                },
            )
            else -> {
                // ENGINE_SERVER (default): stream audio to the server,
                // let it transcribe + apply the wake-word trigger + route.
                if (client == null) {
                    Log.e(TAG, "ENGINE_SERVER selected but no server URL — falling back to whisper")
                    WhisperStt(applicationContext)
                } else {
                    ServerStreamingStt(
                        applicationContext,
                        client,
                        onMetric = { peak, prob ->
                            _events.tryEmit(UiEvent.AudioMetric(peak, prob))
                        },
                    )
                }
            }
        }
        stt = speech
        val serverDrivesStt = (engine == ENGINE_SERVER && client != null)

        pipelineJob = scope.launch {
            // Server events -> TTS reply + UI emit.
            client?.let { c ->
                launch {
                    c.events.collect { ev ->
                        when (ev) {
                            is JarvisWsClient.Event.Welcomed -> {
                                Log.i(TAG, "ws: welcomed session=${ev.sessionId}")
                                _events.tryEmit(UiEvent.Status("connected: ${ev.sessionId}"))
                            }
                            is JarvisWsClient.Event.Transcribed -> {
                                // Server-side STT result. Surface for UI/log.
                                // The server applies its own trigger filter
                                // before routing, so we don't run Trigger here.
                                if (ev.final && ev.text.isNotBlank()) {
                                    transcriptLog?.stt(ev.text)
                                    _events.tryEmit(UiEvent.Transcript(ev.text))
                                }
                            }
                            is JarvisWsClient.Event.Said -> {
                                transcriptLog?.say(ev.text)
                                _events.tryEmit(UiEvent.Said(ev.text))
                                speakLock.withLock {
                                    tts?.say(ev.text)
                                }
                            }
                            is JarvisWsClient.Event.Thinking ->
                                _events.tryEmit(UiEvent.Status("thinking… ${ev.note}"))
                            is JarvisWsClient.Event.Errored ->
                                _events.tryEmit(UiEvent.Status("server error ${ev.code}: ${ev.message}"))
                            is JarvisWsClient.Event.Disconnected ->
                                _events.tryEmit(UiEvent.Status("ws ${ev.reason}"))
                            is JarvisWsClient.Event.ContextCleared -> {
                                Log.i(TAG, "ws: context cleared by server")
                                _events.tryEmit(UiEvent.ContextCleared)
                                _events.tryEmit(UiEvent.Status("context cleared"))
                            }
                        }
                    }
                }
            }

            if (serverDrivesStt) {
                // Server-side STT engine: ServerStreamingStt streams audio
                // to the server; transcripts come back via the ws event flow
                // above. The collect call here still runs so the audio
                // pipeline stays alive — it just never emits.
                speech.start().collect { /* unused */ }
            } else {
                // On-device STT engines (whisper, system): collect transcripts
                // locally, apply the wake-word trigger, send Command. We no
                // longer drop transcripts captured during TTS — the assistant
                // hearing itself is a fact of life, and the server's queue
                // sequences follow-ups correctly. The system prompt forbids
                // the model from saying "computer" in replies, which is the
                // main safeguard against self-triggering loops.
                speech.start().collect { transcript ->
                    transcriptLog?.stt(transcript)
                    _events.tryEmit(UiEvent.Transcript(transcript))

                    val cmd = decideCommand(transcript) ?: return@collect
                    transcriptLog?.cmd(cmd)
                    _events.tryEmit(UiEvent.Triggered(cmd))
                    Log.i(TAG, "trigger -> $cmd")
                    client?.sendCommand(cmd)
                }
            }
        }
    }

    private fun stopSelfCleanly() {
        pipelineJob?.cancel()
        pipelineJob = null
        stt?.close(); stt = null
        ws?.close(); ws = null
        tts?.shutdown(); tts = null
        classifier?.close(); classifier = null
        transcriptLog = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    /** Decide whether [transcript] is addressed to Jarvis. Returns the
     *  command text to dispatch, or null to ignore. Uses the LLM
     *  classifier when available; falls back to the regex trigger. */
    private suspend fun decideCommand(transcript: String): String? {
        val cls = classifier
        if (cls != null) {
            return when (cls.classify(transcript)) {
                Classifier.Result.Yes -> {
                    // Strip a leading "jarvis" / "hey jarvis" if the user
                    // included one, so the server gets just the command.
                    Trigger.extract(transcript) ?: transcript
                }
                Classifier.Result.No -> null
            }
        }
        // No classifier loaded — regex fallback.
        return Trigger.extract(transcript)
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    // --- notification plumbing ---

    private fun createNotificationChannel() {
        val mgr = getSystemService(NotificationManager::class.java)
        val existing = mgr.getNotificationChannel(CHANNEL_ID)
        if (existing != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notif_channel_listening),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.notif_channel_listening_desc)
            setShowBadge(false)
        }
        mgr.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = PendingIntent.getService(
            this, 1,
            Intent(this, JarvisService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(getString(R.string.notif_listening_title))
            .setContentText(getString(R.string.notif_listening_text))
            .setContentIntent(openIntent)
            .addAction(0, "Stop", stopIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun micForegroundType(): Int {
        // The FOREGROUND_SERVICE_TYPE_MICROPHONE constant has existed since
        // API 30. Without it, Android 12+ silently feeds AudioRecord a zero
        // buffer — the mic capture "works" but every sample is silence.
        return ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
    }

    // --- UI events the activity can observe ---

    sealed interface UiEvent {
        data class Transcript(val text: String) : UiEvent
        data class Triggered(val command: String) : UiEvent
        data class Said(val text: String) : UiEvent
        data class Status(val text: String) : UiEvent
        /** Live meter — peak amplitude (0..32767) and VAD prob (0..1).
         *  Emitted ~8 Hz while the service is listening. */
        data class AudioMetric(val peak: Int, val vadProb: Float) : UiEvent
        /** Server confirmed the reset-context request — MainActivity
         *  should wipe its transcript log. */
        object ContextCleared : UiEvent
    }

    companion object {
        private const val TAG = "JarvisService"
        private const val CHANNEL_ID = "jarvis_listening"
        private const val NOTIF_ID = 1001

        const val ACTION_START = "com.sixoffive.ao.jarvis.START"
        const val ACTION_STOP = "com.sixoffive.ao.jarvis.STOP"
        const val ACTION_RESET_CONTEXT = "com.sixoffive.ao.jarvis.RESET_CONTEXT"
        const val EXTRA_SERVER_URL = "server_url"
        const val EXTRA_STT_ENGINE = "stt_engine"

        const val ENGINE_WHISPER = "whisper"
        const val ENGINE_SYSTEM = "system"
        /** Stream audio to the server; CUDA-backed faster-whisper transcribes
         *  there. Recommended on phones where on-device whisper is too slow. */
        const val ENGINE_SERVER = "server"

        private val _events = MutableSharedFlow<UiEvent>(
            extraBufferCapacity = 64,
            onBufferOverflow = BufferOverflow.DROP_OLDEST,
        )
        val events: SharedFlow<UiEvent> = _events.asSharedFlow()

        fun start(context: Context, serverUrl: String, engine: String = ENGINE_SERVER) {
            val intent = Intent(context, JarvisService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_SERVER_URL, serverUrl)
                .putExtra(EXTRA_STT_ENGINE, engine)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, JarvisService::class.java).setAction(ACTION_STOP)
            context.startService(intent)
        }

        /** Ask the service to clear server-side conversation history.
         *  Activity will receive [UiEvent.ContextCleared] when it lands
         *  (or immediately if no ws is connected). */
        fun resetContext(context: Context) {
            val intent = Intent(context, JarvisService::class.java).setAction(ACTION_RESET_CONTEXT)
            context.startService(intent)
        }
    }
}
