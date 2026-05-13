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
    // Try Vulkan if available (built in via GGML_VULKAN=ON). whisper.cpp
    // falls back to CPU automatically if the device or driver doesn't
    // support compute shaders.
    cparams.use_gpu = true;

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

// Callback context passed via progress_callback_user_data. Lives on the
// stack of nativeTranscribe(); the only thing inside that the callback
// reads is a JavaVM* + a global jobject + a cached method id, all of
// which we set up before whisper_full and tear down after.
struct ProgressCtx {
    JavaVM*   jvm;
    jobject   listener;     // global ref, may be null
    jmethodID on_progress;  // void onProgress(int)
    int       last_pct;     // throttle: only emit on change
};

static void progress_callback_thunk(
        struct whisper_context* /*ctx*/,
        struct whisper_state*   /*state*/,
        int progress,
        void* user_data) {
    auto* pc = static_cast<ProgressCtx*>(user_data);
    if (!pc || !pc->listener || !pc->jvm) return;
    if (progress == pc->last_pct) return;
    pc->last_pct = progress;

    JNIEnv* env = nullptr;
    bool need_detach = false;
    if (pc->jvm->GetEnv(reinterpret_cast<void**>(&env), JNI_VERSION_1_6) != JNI_OK) {
        if (pc->jvm->AttachCurrentThread(&env, nullptr) != JNI_OK) return;
        need_detach = true;
    }
    env->CallVoidMethod(pc->listener, pc->on_progress, (jint)progress);
    if (env->ExceptionCheck()) env->ExceptionClear();
    if (need_detach) pc->jvm->DetachCurrentThread();
}

JNIEXPORT jstring JNICALL
Java_com_sixoffive_ao_jarvis_stt_WhisperNative_nativeTranscribe(
        JNIEnv* env, jclass,
        jlong ctxHandle,
        jfloatArray audio,
        jint nThreads,
        jstring languageCode,
        jobject listener) {

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

    ProgressCtx pc{nullptr, nullptr, nullptr, -1};
    if (listener != nullptr) {
        env->GetJavaVM(&pc.jvm);
        pc.listener = env->NewGlobalRef(listener);
        jclass cls  = env->GetObjectClass(pc.listener);
        pc.on_progress = env->GetMethodID(cls, "onProgress", "(I)V");
        if (!pc.on_progress) {
            // Couldn't resolve method — drop the callback rather than crash.
            env->DeleteGlobalRef(pc.listener);
            pc.listener = nullptr;
            if (env->ExceptionCheck()) env->ExceptionClear();
        }
        env->DeleteLocalRef(cls);
    }

    whisper_full_params wparams = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    wparams.n_threads        = nThreads > 0 ? nThreads : 4;
    wparams.print_realtime   = false;
    wparams.print_progress   = false;
    wparams.print_timestamps = false;
    wparams.print_special    = false;
    wparams.translate        = false;
    wparams.language         = lang;
    wparams.no_context       = true;
    wparams.single_segment   = true;
    wparams.temperature      = 0.0f;
    if (pc.listener) {
        wparams.progress_callback           = progress_callback_thunk;
        wparams.progress_callback_user_data = &pc;
    }

    LOGI("whisper_full: starting on %d samples, %d threads, lang=%s",
         (int)n, wparams.n_threads, lang);
    const int rc = whisper_full(ctx, wparams, samples.data(), n);
    LOGI("whisper_full: returned rc=%d", rc);
    env->ReleaseStringUTFChars(languageCode, lang);
    if (pc.listener) env->DeleteGlobalRef(pc.listener);
    if (rc != 0) {
        LOGW("whisper_full failed");
        return env->NewStringUTF("");
    }

    std::string out;
    const int nSeg = whisper_full_n_segments(ctx);
    LOGI("whisper_full -> %d segments", nSeg);
    for (int i = 0; i < nSeg; ++i) {
        const char* t = whisper_full_get_segment_text(ctx, i);
        if (t) {
            LOGI("  seg[%d]: %s", i, t);
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
