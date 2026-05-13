// JNI bridge to llama.cpp for the on-device intent classifier.
//
// The classifier loads a small instruct model (Qwen2.5-0.5B-Instruct GGUF),
// formats each transcript into a short chat-style prompt, runs greedy
// inference for a handful of tokens, and reports whether the model said
// YES or NO. The model is fixed; learning happens by curating the
// few-shot example pool on the Kotlin side and inlining it into the
// prompt.
//
// API note: uses the model-handle style (llama_token_*(model, ...))
// that's stable across llama.cpp versions through our pinned b4404,
// rather than the newer llama_vocab opaque-type API.

#include <jni.h>
#include <android/log.h>
#include <cstdint>
#include <string>
#include <vector>
#include "llama.h"

#define LOG_TAG "LlamaJni"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

namespace {

struct LlamaHandle {
    llama_model*   model = nullptr;
    llama_context* ctx   = nullptr;
};

bool g_backend_inited = false;

void ensure_backend_init() {
    if (!g_backend_inited) {
        llama_backend_init();
        g_backend_inited = true;
    }
}

std::vector<llama_token> tokenize(const llama_model* model, const std::string& text, bool add_bos) {
    int n = -llama_tokenize(model, text.c_str(), (int)text.size(),
                            nullptr, 0, add_bos, /*parse_special*/ true);
    if (n <= 0) return {};
    std::vector<llama_token> out(n);
    int actual = llama_tokenize(model, text.c_str(), (int)text.size(),
                                out.data(), n, add_bos, /*parse_special*/ true);
    if (actual < 0) return {};
    out.resize(actual);
    return out;
}

}  // namespace

extern "C" {

JNIEXPORT jlong JNICALL
Java_com_sixoffive_ao_jarvis_classifier_LlamaNative_nativeInit(
        JNIEnv* env, jclass, jstring modelPath) {
    ensure_backend_init();
    const char* path = env->GetStringUTFChars(modelPath, nullptr);
    LOGI("loading classifier model: %s", path);

    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = 0;

    llama_model* model = llama_load_model_from_file(path, mp);
    env->ReleaseStringUTFChars(modelPath, path);
    if (!model) {
        LOGE("llama_load_model_from_file returned null");
        return 0;
    }

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx     = 1024;
    cp.n_batch   = 256;
    cp.n_threads = 2;
    cp.n_threads_batch = 2;

    llama_context* ctx = llama_new_context_with_model(model, cp);
    if (!ctx) {
        LOGE("llama_new_context_with_model returned null");
        llama_free_model(model);
        return 0;
    }
    auto* h = new LlamaHandle{model, ctx};
    LOGI("classifier ready; handle=%p", (void*)h);
    return reinterpret_cast<jlong>(h);
}

JNIEXPORT void JNICALL
Java_com_sixoffive_ao_jarvis_classifier_LlamaNative_nativeFree(
        JNIEnv*, jclass, jlong handle) {
    auto* h = reinterpret_cast<LlamaHandle*>(handle);
    if (!h) return;
    if (h->ctx)   llama_free(h->ctx);
    if (h->model) llama_free_model(h->model);
    delete h;
}

/** Greedy generate up to `maxTokens` from `prompt` and return the text. */
JNIEXPORT jstring JNICALL
Java_com_sixoffive_ao_jarvis_classifier_LlamaNative_nativeGenerate(
        JNIEnv* env, jclass,
        jlong handle,
        jstring jprompt,
        jint maxTokens) {

    auto* h = reinterpret_cast<LlamaHandle*>(handle);
    if (!h || !h->ctx || !h->model) {
        return env->NewStringUTF("");
    }

    const char* p = env->GetStringUTFChars(jprompt, nullptr);
    std::string prompt(p);
    env->ReleaseStringUTFChars(jprompt, p);

    auto tokens = tokenize(h->model, prompt, /*add_bos*/ true);
    if (tokens.empty()) {
        LOGW("tokenize returned empty for prompt of %zu chars", prompt.size());
        return env->NewStringUTF("");
    }

    // Reset KV cache so prompts don't bleed across calls.
    llama_kv_cache_clear(h->ctx);

    llama_batch batch = llama_batch_get_one(tokens.data(), (int32_t)tokens.size());
    if (llama_decode(h->ctx, batch) != 0) {
        LOGW("llama_decode failed on prompt");
        return env->NewStringUTF("");
    }

    // Greedy sampler chain.
    auto sparams = llama_sampler_chain_default_params();
    llama_sampler* sampler = llama_sampler_chain_init(sparams);
    llama_sampler_chain_add(sampler, llama_sampler_init_greedy());

    const llama_token eos = llama_token_eos(h->model);
    std::string out;
    llama_token cur = 0;
    for (int i = 0; i < maxTokens; ++i) {
        cur = llama_sampler_sample(sampler, h->ctx, -1);
        if (cur == eos || llama_token_is_eog(h->model, cur)) break;

        char piece[256];
        int n = llama_token_to_piece(h->model, cur, piece, sizeof(piece), 0, /*special*/ false);
        if (n > 0) out.append(piece, n);

        llama_batch nb = llama_batch_get_one(&cur, 1);
        if (llama_decode(h->ctx, nb) != 0) {
            LOGW("llama_decode failed mid-generation");
            break;
        }
    }
    llama_sampler_free(sampler);

    return env->NewStringUTF(out.c_str());
}

}  // extern "C"
