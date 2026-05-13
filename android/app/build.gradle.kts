plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "com.sixoffive.ao.jarvis"
    compileSdk = 35
    ndkVersion = "27.0.12077973"

    defaultConfig {
        applicationId = "com.sixoffive.ao.jarvis"
        minSdk = 31      // Android 12 — first version with split foreground-service types
        targetSdk = 35   // Android 15
        versionCode = 1
        versionName = "0.1.0"

        ndk {
            // 64-bit only. Tablets and phones from the last several years.
            abiFilters += listOf("arm64-v8a", "x86_64")
        }

        externalNativeBuild {
            cmake {
                cppFlags += "-std=c++17 -fexceptions -frtti"
                arguments += listOf(
                    "-DANDROID_STL=c++_shared",
                    "-DWHISPER_BUILD_TESTS=OFF",
                    "-DWHISPER_BUILD_EXAMPLES=OFF",
                    "-DGGML_OPENMP=OFF",
                )
            }
        }
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
        debug {
            isDebuggable = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }

    // ggml-small.en.bin is downloaded at runtime, not bundled.
    // silero_vad.onnx is small and shipped as an asset.
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.2.0")
    implementation("androidx.activity:activity-ktx:1.9.3")

    // Coroutines for async pipeline
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    // JSON for the wire protocol
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    // WebSocket client to jarvis-server
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // ONNX Runtime for Silero VAD (and any future small models)
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.19.2")
}
