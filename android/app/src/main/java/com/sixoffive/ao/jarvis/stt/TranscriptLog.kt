package com.sixoffive.ao.jarvis.stt

import android.content.Context
import android.util.Log
import java.io.File
import java.io.FileWriter
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

/**
 * Append-only daily log of everything STT produces.
 *
 * Lives at `<app>/transcripts/YYYY-MM-DD.log` in the app's external
 * files dir, so you can pull it off the phone via USB without any
 * special permissions.
 *
 *   /sdcard/Android/data/com.sixoffive.ao.jarvis/files/transcripts/2026-05-13.log
 *
 * Each line is `HH:MM:SS.mmm  KIND  text`, where KIND is one of:
 *   stt    — final whisper transcript (verbatim)
 *   cmd    — text parsed as a jarvis command (after trigger regex)
 *   say    — server's spoken reply (TTS source text)
 *
 * Lockless single-writer: only the JarvisService calls these methods,
 * and the service runs everything on the same coroutine scope. No
 * concurrent writes possible in practice; FileWriter is reopened on
 * each line which is fine for low-volume command-style traffic.
 */
class TranscriptLog(context: Context) {

    private val baseDir: File = run {
        val ext = context.getExternalFilesDir(null) ?: context.filesDir
        File(ext, "transcripts").apply { mkdirs() }
    }

    private val dateFmt: DateTimeFormatter = DateTimeFormatter.ISO_LOCAL_DATE
    private val timeFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm:ss.SSS")

    init {
        Log.i(TAG, "transcripts logged to: ${baseDir.absolutePath}")
    }

    fun stt(text: String) = append("stt", text)
    fun cmd(text: String) = append("cmd", text)
    fun say(text: String) = append("say", text)

    private fun append(kind: String, text: String) {
        if (text.isBlank()) return
        val now = LocalDateTime.now()
        val file = File(baseDir, "${now.toLocalDate().format(dateFmt)}.log")
        val line = "${now.toLocalTime().format(timeFmt)}  $kind  ${text.replace("\n", " ")}\n"
        try {
            FileWriter(file, /* append = */ true).use { it.write(line) }
        } catch (exc: Exception) {
            Log.w(TAG, "could not write transcript log to ${file.path}: $exc")
        }
    }

    /** For diagnostic display. Returns null if no log file exists today. */
    fun todaysLogPath(): String {
        val f = File(baseDir, "${LocalDate.now().format(dateFmt)}.log")
        return if (f.exists()) f.absolutePath else baseDir.absolutePath
    }

    companion object {
        private const val TAG = "TranscriptLog"
    }
}
