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
import com.sixoffive.ao.jarvis.stt.ModelStore
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.launch
import java.time.LocalTime
import java.time.format.DateTimeFormatter

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences
    private lateinit var modelStore: ModelStore

    private var listening = false
    private var downloadJob: Job? = null
    private val transcriptLog = StringBuilder()
    private val timeFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm:ss")

    private val requestPerms = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { granted ->
        val ok = (granted[Manifest.permission.RECORD_AUDIO] == true) &&
                (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
                        granted[Manifest.permission.POST_NOTIFICATIONS] == true)
        if (ok) ensureModelThenStart()
        else appendLine("permissions denied — can't start listening")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = getSharedPreferences("jarvis", MODE_PRIVATE)
        modelStore = ModelStore(this)

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

        val sttReady = modelStore.isCached(ModelStore.DEFAULT_MODEL)
        val clReady = modelStore.classifierIsCached()
        binding.status.text = when {
            sttReady && clReady -> "idle (models ready)"
            sttReady -> "idle (will download classifier on first start)"
            clReady -> "idle (will download STT model on first start)"
            else -> "idle (will download both models on first start)"
        }

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
                        is JarvisService.UiEvent.AudioMetric -> {
                            // peak is 0..32767. Use a log-ish curve so quiet
                            // speech still moves the bar instead of squashing
                            // into the bottom 5%.
                            val micPct = ((event.peak.coerceAtLeast(1)
                                .toFloat() / 32767f).coerceAtMost(1f)
                                .let { kotlin.math.sqrt(it) } * 100f).toInt()
                            binding.micMeter.progress = micPct
                            binding.vadMeter.progress =
                                (event.vadProb.coerceIn(0f, 1f) * 100f).toInt()
                        }
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
        if (missing.isEmpty()) ensureModelThenStart()
        else requestPerms.launch(missing.toTypedArray())
    }

    private fun ensureModelThenStart() {
        val sttName = ModelStore.DEFAULT_MODEL
        val needSt = !modelStore.isCached(sttName)
        val needCl = !modelStore.classifierIsCached()
        if (!needSt && !needCl) {
            startListening()
            return
        }
        binding.startStop.isEnabled = false
        downloadJob?.cancel()
        downloadJob = lifecycleScope.launch {
            if (needSt) {
                binding.status.text = "downloading STT model (ggml-$sttName)…"
                modelStore.download(sttName)
                    .catch { exc ->
                        binding.status.text = "STT model failed: ${exc.message}"
                        binding.startStop.isEnabled = true
                        return@catch
                    }
                    .collect { pct ->
                        binding.status.text = "downloading STT model… $pct%"
                    }
            }
            if (needCl) {
                binding.status.text = "downloading classifier (Qwen 0.5B)…"
                modelStore.downloadClassifier()
                    .catch { exc ->
                        binding.status.text = "classifier model failed: ${exc.message}"
                        binding.startStop.isEnabled = true
                        return@catch
                    }
                    .collect { pct ->
                        binding.status.text = "downloading classifier… $pct%"
                    }
            }
            binding.startStop.isEnabled = true
            startListening()
        }
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
