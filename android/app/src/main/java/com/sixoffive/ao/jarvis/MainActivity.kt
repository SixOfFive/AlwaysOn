package com.sixoffive.ao.jarvis

import android.Manifest
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.text.method.ScrollingMovementMethod
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.sixoffive.ao.jarvis.databinding.ActivityMainBinding
import kotlinx.coroutines.launch
import java.time.LocalTime
import java.time.format.DateTimeFormatter

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences

    private var listening = false
    private val transcriptLog = StringBuilder()
    private val timeFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm:ss")

    private val requestPerms = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { granted ->
        val ok = (granted[Manifest.permission.RECORD_AUDIO] == true) &&
                (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
                        granted[Manifest.permission.POST_NOTIFICATIONS] == true)
        if (ok) startListening()
        else appendLine("permissions denied — can't start listening")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = getSharedPreferences("jarvis", MODE_PRIVATE)
        binding.serverUrl.setText(prefs.getString(KEY_SERVER, ""))
        binding.transcript.movementMethod = ScrollingMovementMethod()

        binding.startStop.setOnClickListener {
            if (listening) {
                stopListening()
            } else {
                val url = binding.serverUrl.text?.toString().orEmpty().trim()
                prefs.edit().putString(KEY_SERVER, url).apply()
                requireAllPerms()
            }
        }

        binding.status.text = "idle"

        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                JarvisService.events.collect { event ->
                    when (event) {
                        is JarvisService.UiEvent.Transcript ->
                            appendLine(event.text)
                        is JarvisService.UiEvent.Triggered ->
                            appendLine("→ jarvis: ${event.command}")
                        is JarvisService.UiEvent.Said ->
                            appendLine("jarvis> ${event.text}")
                        is JarvisService.UiEvent.Status ->
                            binding.status.text = event.text
                    }
                }
            }
        }
    }

    private fun requireAllPerms() {
        val needed = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            needed += Manifest.permission.POST_NOTIFICATIONS
        }
        val missing = needed.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) startListening()
        else requestPerms.launch(missing.toTypedArray())
    }

    private fun startListening() {
        val url = binding.serverUrl.text?.toString().orEmpty().trim()
        JarvisService.start(this, url)
        listening = true
        binding.startStop.setText(R.string.stop)
        binding.status.text = if (url.isEmpty()) "listening (no server)" else "listening"
    }

    private fun stopListening() {
        JarvisService.stop(this)
        listening = false
        binding.startStop.setText(R.string.start)
        binding.status.text = "idle"
    }

    private fun appendLine(line: String) {
        val ts = LocalTime.now().format(timeFmt)
        transcriptLog.append('[').append(ts).append("] ").append(line).append('\n')
        binding.transcript.text = transcriptLog
        binding.transcriptScroll.post {
            binding.transcriptScroll.fullScroll(android.view.View.FOCUS_DOWN)
        }
    }

    companion object {
        private const val KEY_SERVER = "server_url"
    }
}
