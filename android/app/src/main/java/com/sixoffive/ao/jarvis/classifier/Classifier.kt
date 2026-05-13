package com.sixoffive.ao.jarvis.classifier

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Intent classifier: given a transcript, decide whether the user is
 * addressing Jarvis. Backed by a tiny instruct LLM (Qwen2.5-0.5B at this
 * tag) via llama.cpp; runs CPU-only on the phone.
 *
 * "Learning" here means: the few-shot example pool is a plain list that
 * can grow over time as the user marks misclassifications. For phase 1
 * the seed list is hard-coded below; a follow-up adds a feedback UI and
 * persists corrections to disk.
 */
class Classifier private constructor(private val handle: Long) {

    /** @return Result.Yes if the model said yes; Result.No otherwise.
     *  Falls back to Result.No on any error so we don't fire commands
     *  on a malfunctioning classifier. */
    suspend fun classify(transcript: String): Result {
        if (transcript.isBlank()) return Result.No
        val prompt = buildPrompt(transcript)
        val raw = withContext(Dispatchers.Default) {
            try {
                LlamaNative.nativeGenerate(handle, prompt, MAX_TOKENS)
            } catch (exc: Throwable) {
                Log.w(TAG, "classifier generation failed", exc)
                ""
            }
        }
        val verdict = raw.trim().lowercase()
        Log.i(TAG, "classify(${transcript.take(60)}) -> '$verdict'")
        // Be conservative: only treat a clean "yes" as positive. Anything
        // else — empty, "no", "i'm not sure", garbage — defaults to No.
        return if (verdict.startsWith("yes")) Result.Yes else Result.No
    }

    fun close() {
        if (handle != 0L) LlamaNative.nativeFree(handle)
    }

    enum class Result { Yes, No }

    companion object {
        private const val TAG = "Classifier"
        private const val MAX_TOKENS = 4   // we only need YES or NO

        /** Construct on a worker thread — model load takes ~1s. */
        suspend fun load(modelPath: String): Classifier? = withContext(Dispatchers.IO) {
            val h = LlamaNative.nativeInit(modelPath)
            if (h == 0L) {
                Log.w(TAG, "nativeInit failed for $modelPath")
                null
            } else {
                Classifier(h)
            }
        }

        // Qwen2.5 chat template. Few-shot examples teach the model what
        // "addressed to Jarvis" looks like vs. ambient speech. Keep the
        // list short — every example burns prompt tokens, which costs
        // latency.
        private val SEED_EXAMPLES: List<Pair<String, Boolean>> = listOf(
            "Hey Jarvis, what time is it"                              to true,
            "Jarvis turn off the lights"                               to true,
            "okay jarvis tell me a joke"                               to true,
            "jarvis what's the weather like"                           to true,
            "I was telling jarvis the cat to come inside"              to false,
            "did you see Iron Man, the part where jarvis hacks in"     to false,
            "I think I'll have coffee this morning"                    to false,
            "what time is the meeting again"                           to false,
        )

        private const val SYSTEM = (
            "You are a binary intent classifier. " +
            "Decide whether an utterance is directly addressed to a voice " +
            "assistant named Jarvis. Reply with exactly one word: YES or NO."
        )

        private fun buildPrompt(transcript: String): String {
            val sb = StringBuilder()
            sb.append("<|im_start|>system\n").append(SYSTEM).append("<|im_end|>\n")
            for ((utter, yes) in SEED_EXAMPLES) {
                sb.append("<|im_start|>user\n\"").append(utter).append("\"<|im_end|>\n")
                sb.append("<|im_start|>assistant\n")
                sb.append(if (yes) "YES" else "NO")
                sb.append("<|im_end|>\n")
            }
            sb.append("<|im_start|>user\n\"").append(transcript).append("\"<|im_end|>\n")
            sb.append("<|im_start|>assistant\n")
            return sb.toString()
        }
    }
}
