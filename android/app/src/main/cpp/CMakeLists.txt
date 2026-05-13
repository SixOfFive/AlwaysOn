cmake_minimum_required(VERSION 3.22.1)
project(jarvis_native CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# --- whisper.cpp via FetchContent ------------------------------------------
# Pinned tag — bump deliberately when you want a newer build. Configure-time
# git clone happens once per Gradle clean; afterwards CMake reuses the cache
# under build/<variant>/.cxx/<abi>/_deps/whisper-src.

include(FetchContent)

set(WHISPER_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_SERVER   OFF CACHE BOOL "" FORCE)
set(GGML_OPENMP            OFF CACHE BOOL "" FORCE)
set(GGML_LLAMAFILE         OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS      OFF CACHE BOOL "" FORCE)

FetchContent_Declare(
    whisper
    GIT_REPOSITORY https://github.com/ggml-org/whisper.cpp.git
    GIT_TAG        v1.7.4
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(whisper)

# --- our JNI library --------------------------------------------------------

add_library(jarvis_native SHARED
    whisper_jni.cpp
)

target_link_libraries(jarvis_native
    PRIVATE
        whisper
        android
        log
)
