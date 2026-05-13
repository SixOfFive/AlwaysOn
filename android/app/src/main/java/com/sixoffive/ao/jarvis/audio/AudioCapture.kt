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

        // MIC is the rawest audio source; VOICE_RECOGNITION applies system
        // pre-processing that on some devices mutes the input when no
        // recognizer is bound, which gave us zero buffers.
        val record = AudioRecord(
            MediaRecorder.AudioSource.MIC,
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
            var chunksSinceLog = 0
            var peakSinceLog = 0
            while (running.get()) {
                val n = record.read(buf, 0, CHUNK_SAMPLES, AudioRecord.READ_BLOCKING)
                if (n <= 0) {
                    Log.w(TAG, "AudioRecord.read returned $n")
                    continue
                }
                if (n != CHUNK_SAMPLES) {
                    Log.w(TAG, "skipping partial chunk of $n samples")
                    continue
                }

                // Track peak amplitude to verify mic is actually delivering
                // sound, independent of any downstream VAD or scaling bugs.
                var peak = 0
                for (s in buf) {
                    val a = if (s < 0) -s.toInt() else s.toInt()
                    if (a > peak) peak = a
                }
                if (peak > peakSinceLog) peakSinceLog = peak
                chunksSinceLog++
                if (chunksSinceLog >= 32) {  // ~1s
                    Log.i(TAG, "mic peak over last ~1s: $peakSinceLog (max 32767)")
                    chunksSinceLog = 0; peakSinceLog = 0
                }

                trySendOrLog(this, buf.copyOf())
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
