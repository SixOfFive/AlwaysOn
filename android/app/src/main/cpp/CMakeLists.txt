cmake_minimum_required(VERSION 3.22.1)
project(jarvis_native CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Force Release config even when AGP's debug variant injects Debug. The
# default debug flags include -O0 which makes whisper.cpp unusable. We
# overwrite both the cache entry AND the per-config flag variables so a
# stale CMakeCache.txt can't undo this.
set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
set(CMAKE_C_FLAGS_DEBUG   "-O3 -DNDEBUG" CACHE STRING "" FORCE)
set(CMAKE_CXX_FLAGS_DEBUG "-O3 -DNDEBUG" CACHE STRING "" FORCE)

message(STATUS "jarvis_native build type: ${CMAKE_BUILD_TYPE}")
message(STATUS "jarvis_native C flags:   ${CMAKE_C_FLAGS} ${CMAKE_C_FLAGS_RELEASE}")
message(STATUS "jarvis_native CXX flags: ${CMAKE_CXX_FLAGS} ${CMAKE_CXX_FLAGS_RELEASE}")

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

# Force aggressive optimization globally BEFORE pulling whisper.cpp in.
# add_compile_options affects every target defined after this point,
# including all of ggml + whisper that FetchContent_MakeAvailable adds.
# Doing it here instead of per-target overrides means there's nothing
# the AGP/CMake combo can silently strip.
add_compile_options(
    -O3
    -ffast-math
    -fno-finite-math-only  # whisper uses inf/nan intentionally
    -DNDEBUG
)

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

# (optimization is applied globally above via add_compile_options.)
