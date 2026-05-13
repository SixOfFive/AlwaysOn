# jarvis-android

Companion to the Windows client. Same wire protocol, same `Jarvis` trigger
phrase, same server. Same Whisper engine.

## Stack

- Kotlin native, min SDK 31 (Android 12), target SDK 35 (Android 15)
- Mic always-on (real continuous capture, not a recognizer loop) via a
  foreground `Service` with persistent notification
- `AudioRecord` 16 kHz mono PCM → Silero VAD (ONNX Runtime) → whisper.cpp
  via JNI for the actual transcription
- `OkHttp` WebSocket → jarvis-server
- Android `TextToSpeech` for the spoken reply

The Whisper GGML model (`ggml-small.en.bin`, ~244 MB) downloads on first
start from Hugging Face into the app's private files dir. The Silero VAD
ONNX file is small (~2 MB) and ships as an asset.

`SystemStt` (Android `SpeechRecognizer`) is still available as a fallback —
pass `ENGINE_SYSTEM` to `JarvisService.start()` if you ever want it.

## Open in Android Studio

```
File → Open → C:\Users\sixoffive\Documents\AO\android
```

Let Gradle sync (first time pulls AGP + Kotlin + deps; takes a few min).

## Install on phone

Either:

1. Plug in phone with USB debugging enabled, hit **Run** in Android
   Studio. APK builds, installs, launches.
2. Or from a terminal:
   ```cmd
   gradlew installDebug
   ```

## STT is fully on-device

`AudioRecord` + `silero-vad.onnx` + `whisper.cpp`. No cloud, no Google
service dependency. The first time you tap **Start listening**, the app
will download the Whisper model (~244 MB) from Hugging Face. After that
it's offline.

If you want a faster but less accurate model, edit
`stt/ModelStore.kt:DEFAULT_MODEL` — options on huggingface.co/ggerganov/whisper.cpp
include `tiny.en`, `base.en`, `small.en`, `medium.en`.

## Use

1. Open the app.
2. Enter your server URL — e.g. `ws://192.168.15.103:7333/ws` (use the
   LAN IP of the box running jarvis-server, not `127.0.0.1`).
3. Tap **Start listening**. Grant mic + notification permissions.
4. Lock the phone. The persistent notification means the mic stays
   alive.
5. Say "Jarvis, what time is it?" → server replies, TTS speaks.

The transcript log in-app shows every utterance the system recognizer
caught, so you can see what it's hearing.

## What's missing vs the Windows client

- **Wake-word model.** Same `Jarvis` trigger phrase, but it fires *after*
  Whisper transcribes — there's no cheap always-on detector ahead of it.
  Real wake-word on phone needs a separate small ONNX (or Picovoice
  Porcupine).
- **Battery.** Whisper on CPU + always-on mic drains faster than you'd
  expect — figure 10–20% per hour of active listening on recent phones.
  Tap **Stop** when you're done.

## Layout

```
android/
├── settings.gradle.kts
├── build.gradle.kts
├── gradle.properties
└── app/
    ├── build.gradle.kts            # NDK + CMake + onnxruntime-android
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml
        ├── assets/silero_vad.onnx  # ~2 MB, bundled
        ├── cpp/
        │   ├── CMakeLists.txt      # FetchContent for whisper.cpp v1.7.4
        │   └── whisper_jni.cpp     # init / transcribe / free
        ├── res/                    # layouts, theme, icons, strings
        └── java/com/sixoffive/ao/jarvis/
            ├── MainActivity.kt     # UI: server URL, start/stop, transcript log,
            │                         # model download progress
            ├── JarvisService.kt    # foreground service, owns the pipeline
            ├── audio/
            │   ├── AudioCapture.kt # AudioRecord 16kHz mono continuous
            │   └── VadSegmenter.kt # Silero VAD via ORT, emits speech segments
            ├── stt/
            │   ├── SpeechToText.kt # interface
            │   ├── WhisperStt.kt   # default: audio + VAD + whisper.cpp JNI
            │   ├── WhisperNative.kt# external fun declarations
            │   ├── SystemStt.kt    # fallback: Android SpeechRecognizer
            │   └── ModelStore.kt   # downloads ggml-*.bin from huggingface.co
            ├── trigger/Trigger.kt  # regex for "jarvis"
            ├── net/
            │   ├── Messages.kt     # @Serializable mirrors of the protocol
            │   └── JarvisWsClient.kt
            └── tts/Tts.kt
```
