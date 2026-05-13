package com.sixoffive.ao.jarvis.stt

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import java.io.File
import java.net.URL

/**
 * Downloads (and caches) whisper.cpp GGML models in the app's private files dir.
 *
 * GGML hosted by the whisper.cpp team on Hugging Face:
 *   https://huggingface.co/ggerganov/whisper.cpp
 *
 * We pull `ggml-<name>.bin` directly — no Hugging Face SDK, no auth. Anonymous
 * GETs work for the public files.
 */
class ModelStore(private val context: Context) {

    val modelsDir: File = File(context.filesDir, "models").apply { mkdirs() }

    fun modelFile(name: String): File = File(modelsDir, "ggml-$name.bin")

    fun isCached(name: String): Boolean {
        val f = modelFile(name)
        return f.exists() && f.length() > 1_000_000  // sanity: > 1 MB
    }

    /** Streams progress 0..100. Final emission of 100 means file is ready. */
    fun download(name: String): Flow<Int> = flow {
        val target = modelFile(name)
        if (isCached(name)) {
            emit(100)
            return@flow
        }
        val tmp = File(modelsDir, "ggml-$name.bin.part")
        if (tmp.exists()) tmp.delete()

        val url = URL("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$name.bin")
        Log.i(TAG, "downloading $url -> $tmp")

        val conn = url.openConnection().apply {
            connectTimeout = 15_000
            readTimeout = 30_000
        }
        val total = conn.contentLengthLong
        var read = 0L
        var lastPct = -1
        emit(0)

        conn.getInputStream().use { input ->
            tmp.outputStream().use { output ->
                val buf = ByteArray(64 * 1024)
                while (true) {
                    val n = input.read(buf)
                    if (n <= 0) break
                    output.write(buf, 0, n)
                    read += n
                    if (total > 0) {
                        val pct = ((read * 100) / total).toInt()
                        if (pct != lastPct) {
                            lastPct = pct
                            emit(pct)
                        }
                    }
                }
            }
        }
        if (!tmp.renameTo(target)) {
            tmp.delete()
            throw java.io.IOException("could not rename ${tmp.name} to ${target.name}")
        }
        Log.i(TAG, "downloaded $name: ${target.length() / 1_000_000} MB")
        emit(100)
    }.flowOn(Dispatchers.IO)

    companion object {
        private const val TAG = "ModelStore"
        // Quantized base.en (~57 MB at q5_1) — same accuracy class as f16
        // base.en, ~2-3x faster on CPU, less RAM. Quantization is loss-less
        // enough for command-style input.
        // Other options:
        //   f16:     tiny.en, base.en, small.en, medium.en
        //   q5_1:    tiny.en-q5_1, base.en-q5_1, small.en-q5_1
        //   q4_0:    even faster, lossier
        const val DEFAULT_MODEL = "base.en-q5_1"
    }
}
