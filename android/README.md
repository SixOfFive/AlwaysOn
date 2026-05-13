# jarvis-android

Companion to the Windows client. Same wire protocol, same `Jarvis` trigger
phrase, same server.

## Stack

- Kotlin native, min SDK 31 (Android 12), target SDK 35 (Android 15)
- Mic always-on via a foreground `Service` with persistent notification
- On-device STT via Android's `SpeechRecognizer` (offline if you have the
  language pack installed)
- `OkHttp` WebSocket → jarvis-server
- Android `TextToSpeech` for the spoken reply

The architecture deliberately puts STT behind a `SpeechToText` interface
so we can swap in a `whisper.cpp` JNI implementation later for
guaranteed-offline transcription on phones without an offline pack.

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

## Make STT fully on-device

Android's `SpeechRecognizer` defaults to network if no offline pack is
installed. Once-per-device:

> Settings → System → Languages → Voice → Offline speech recognition →
> tap **+** → choose **English (US)** → Download.

Some devices show "On-device recognition" instead — same idea.
Pixel-family phones usually have it pre-installed.

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

- **Whisper-quality STT.** Android's recognizer is good but not as
  consistent as `faster-whisper`. Whisper via whisper.cpp JNI is the
  planned upgrade.
- **Wake-word model.** No openWakeWord on phone (would need a tiny
  ONNX-friendly trigger; Picovoice is the obvious commercial path).
- **Battery.** Always-on listening + background service drains faster
  than you'd expect — figure 10–20% per hour of active listening on
  recent phones. Tap **Stop** when you're done.

## Layout

```
android/
├── settings.gradle.kts
├── build.gradle.kts
├── gradle.properties
└── app/
    ├── build.gradle.kts
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml
        ├── res/                    # layouts, theme, icons, strings
        └── java/com/sixoffive/ao/jarvis/
            ├── MainActivity.kt     # UI: server URL, start/stop, transcript log
            ├── JarvisService.kt    # foreground service, mic notification
            ├── stt/
            │   ├── SpeechToText.kt # interface
            │   └── SystemStt.kt    # Android SpeechRecognizer impl
            ├── trigger/Trigger.kt  # regex for "jarvis"
            ├── net/
            │   ├── Messages.kt     # @Serializable mirrors of the protocol
            │   └── JarvisWsClient.kt
            └── tts/Tts.kt
```
