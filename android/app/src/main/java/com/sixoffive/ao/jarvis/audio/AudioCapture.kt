package com.sixoffive.ao.jarvis.audio

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.ProducerScope
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.flowOn
import kotlin.concurrent.thread

/**
 * Continuous 16 kHz mono PCM capture via AudioRecord.
 *
 * Emits short[] chunks of [CHUNK_SAMPLES] (512 samples = 32 ms), matching
 * Silero VAD's expected window. Unlike the Windows client which uses a
 * sounddevice callback, AudioRecord on Android is a blocking read loop on
 * a dedicated thread; we bridge into a Flow.
 */
class AudioCapture {

    @SuppressLint("MissingPermission")
    fun stream(context: Context): Flow<ShortArray> = callbackFlow {
        if (ContextCompat.checkSelfPermission(context, android.Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            close(SecurityException("RECORD_AUDIO not granted"))
            return@callbackFlow
        }

        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
        )
        val bufBytes = maxOf(minBuf, CHUNK_BYTES * 8)

        val record = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufBytes,
        )
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            close(IllegalStateException("AudioRecord init failed"))
            return@callbackFlow
        }

        record.startRecording()
        Log.i(TAG, "AudioRecord started: ${SAMPLE_RATE} Hz, mono, 16-bit, buffer=$bufBytes bytes")

        val running = java.util.concurrent.atomic.AtomicBoolean(true)
        val readerThread = thread(name = "audio-capture", isDaemon = true) {
            val buf = ShortArray(CHUNK_SAMPLES)
            while (running.get()) {
                val n = record.read(buf, 0, CHUNK_SAMPLES, AudioRecord.READ_BLOCKING)
                if (n <= 0) {
                    Log.w(TAG, "AudioRecord.read returned $n")
                    continue
                }
                // Drop partial reads — downstream (Silero VAD) is strict
                // about chunk size and would crash if we emitted them.
                if (n == CHUNK_SAMPLES) {
                    trySendOrLog(this, buf.copyOf())
                } else {
                    Log.w(TAG, "skipping partial chunk of $n samples")
                }
            }
            Log.i(TAG, "audio-capture thread exiting")
        }

        awaitClose {
            running.set(false)
            try {
                record.stop()
            } catch (_: Exception) {}
            record.release()
            readerThread.join(500)
            Log.i(TAG, "AudioRecord released")
        }
    }.flowOn(Dispatchers.IO)

    private fun trySendOrLog(scope: ProducerScope<ShortArray>, chunk: ShortArray) {
        val r = scope.trySend(chunk)
        if (r.isFailure) {
            // Downstream is backed up; drop silently. With a 32 ms cadence this
            // would mean the consumer is wildly slow, not the normal case.
        }
    }

    companion object {
        private const val TAG = "AudioCapture"
        const val SAMPLE_RATE = 16_000
        const val CHUNK_SAMPLES = 512        // 32 ms @ 16 kHz, matches silero-vad
        const val CHUNK_BYTES = CHUNK_SAMPLES * 2
    }
}
