// JNI bridge to whisper.cpp. Three calls cover the entire API surface
// we need: load a model, transcribe a float[] of 16 kHz mono audio,
// free the context.

#include <jni.h>
#include <android/log.h>
#include <string>
#include <vector>
#include "whisper.h"

#define LOG_TAG "WhisperJni"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

extern "C" {

JNIEXPORT jlong JNICALL
Java_com_sixoffive_ao_jarvis_stt_WhisperNative_nativeInit(
        JNIEnv* env, jclass, jstring modelPath) {
    const char* path = env->GetStringUTFChars(modelPath, nullptr);
    LOGI("loading model: %s", path);

    whisper_context_params cparams = whisper_context_default_params();
    cparams.use_gpu = false;  // Android: CPU only

    whisper_context* ctx = whisper_init_from_file_with_params(path, cparams);
    env->ReleaseStringUTFChars(modelPath, path);

    if (!ctx) {
        LOGE("whisper_init_from_file_with_params returned null");
        return 0;
    }
    LOGI("model loaded; ctx=%p", (void*)ctx);
    return reinterpret_cast<jlong>(ctx);
}

JNIEXPORT void JNICALL
Java_com_sixoffive_ao_jarvis_stt_WhisperNative_nativeFree(
        JNIEnv*, jclass, jlong ctxHandle) {
    auto* ctx = reinterpret_cast<whisper_context*>(ctxHandle);
    if (ctx) whisper_free(ctx);
}

JNIEXPORT jstring JNICALL
Java_com_sixoffive_ao_jarvis_stt_WhisperNative_nativeTranscribe(
        JNIEnv* env, jclass,
        jlong ctxHandle,
        jfloatArray audio,
        jint nThreads,
        jstring languageCode) {

    auto* ctx = reinterpret_cast<whisper_context*>(ctxHandle);
    if (!ctx) {
        return env->NewStringUTF("");
    }

    const jsize n = env->GetArrayLength(audio);
    if (n <= 0) {
        return env->NewStringUTF("");
    }
    std::vector<float> samples(n);
    env->GetFloatArrayRegion(audio, 0, n, samples.data());

    const char* lang = env->GetStringUTFChars(languageCode, nullptr);

    whisper_full_params wparams = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    wparams.n_threads        = nThreads > 0 ? nThreads : 4;
    wparams.print_realtime   = false;
    wparams.print_progress   = false;
    wparams.print_timestamps = false;
    wparams.print_special    = false;
    wparams.translate        = false;
    wparams.language         = lang;
    wparams.no_context       = true;
    wparams.single_segment   = false;
    wparams.suppress_blank   = true;
    wparams.temperature      = 0.0f;

    if (whisper_full(ctx, wparams, samples.data(), n) != 0) {
        env->ReleaseStringUTFChars(languageCode, lang);
        LOGW("whisper_full failed");
        return env->NewStringUTF("");
    }
    env->ReleaseStringUTFChars(languageCode, lang);

    std::string out;
    const int nSeg = whisper_full_n_segments(ctx);
    for (int i = 0; i < nSeg; ++i) {
        const char* t = whisper_full_get_segment_text(ctx, i);
        if (t) {
            if (!out.empty()) out += ' ';
            out += t;
        }
    }
    // Trim leading whitespace whisper.cpp tends to emit.
    size_t start = out.find_first_not_of(" \t\n\r");
    if (start != std::string::npos) out = out.substr(start);

    return env->NewStringUTF(out.c_str());
}

}  // extern "C"
