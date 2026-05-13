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
import com.sixoffive.ao.jarvis.net.JarvisWsClient
import com.sixoffive.ao.jarvis.stt.ModelStore
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
    private val speakLock = Mutex()  // serialize TTS, drop transcripts while speaking
    @Volatile private var speaking = false

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
        }
        return START_NOT_STICKY
    }

    private fun startListening(serverUrl: String, engine: String) {
        if (pipelineJob != null) return  // already running

        createNotificationChannel()
        startForeground(NOTIF_ID, buildNotification(), micForegroundType())

        transcriptLog = TranscriptLog(applicationContext)
        tts = Tts(applicationContext)
        val client = if (serverUrl.isNotBlank()) {
            JarvisWsClient(serverUrl, clientId = Build.MODEL ?: "android-mic").also { it.connect() }
        } else {
            null
        }
        ws = client

        val speech: SpeechToText = when (engine) {
            ENGINE_SYSTEM -> SystemStt(applicationContext)
            else -> WhisperStt(
                applicationContext,
                onMetric = { peak, prob ->
                    _events.tryEmit(UiEvent.AudioMetric(peak, prob))
                },
                onProgress = { pct ->
                    _events.tryEmit(UiEvent.Status("transcribing $pct%"))
                },
            )
        }
        stt = speech

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
                            is JarvisWsClient.Event.Said -> {
                                transcriptLog?.say(ev.text)
                                _events.tryEmit(UiEvent.Said(ev.text))
                                speakLock.withLock {
                                    speaking = true
                                    try {
                                        tts?.say(ev.text)
                                    } finally {
                                        speaking = false
                                    }
                                }
                            }
                            is JarvisWsClient.Event.Thinking ->
                                _events.tryEmit(UiEvent.Status("thinking… ${ev.note}"))
                            is JarvisWsClient.Event.Errored ->
                                _events.tryEmit(UiEvent.Status("server error ${ev.code}: ${ev.message}"))
                            is JarvisWsClient.Event.Disconnected ->
                                _events.tryEmit(UiEvent.Status("ws ${ev.reason}"))
                        }
                    }
                }
            }

            // STT transcripts -> print + trigger -> server.
            speech.start().collect { transcript ->
                if (speaking) return@collect  // ignore our own voice
                transcriptLog?.stt(transcript)
                _events.tryEmit(UiEvent.Transcript(transcript))

                val cmd = Trigger.extract(transcript) ?: return@collect
                transcriptLog?.cmd(cmd)
                _events.tryEmit(UiEvent.Triggered(cmd))
                Log.i(TAG, "trigger -> $cmd")
                client?.sendCommand(cmd)
            }
        }
    }

    private fun stopSelfCleanly() {
        pipelineJob?.cancel()
        pipelineJob = null
        stt?.close(); stt = null
        ws?.close(); ws = null
        tts?.shutdown(); tts = null
        transcriptLog = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
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
    }

    companion object {
        private const val TAG = "JarvisService"
        private const val CHANNEL_ID = "jarvis_listening"
        private const val NOTIF_ID = 1001

        const val ACTION_START = "com.sixoffive.ao.jarvis.START"
        const val ACTION_STOP = "com.sixoffive.ao.jarvis.STOP"
        const val EXTRA_SERVER_URL = "server_url"
        const val EXTRA_STT_ENGINE = "stt_engine"

        const val ENGINE_WHISPER = "whisper"
        const val ENGINE_SYSTEM = "system"

        private val _events = MutableSharedFlow<UiEvent>(
            extraBufferCapacity = 64,
            onBufferOverflow = BufferOverflow.DROP_OLDEST,
        )
        val events: SharedFlow<UiEvent> = _events.asSharedFlow()

        fun start(context: Context, serverUrl: String, engine: String = ENGINE_WHISPER) {
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
    }
}
