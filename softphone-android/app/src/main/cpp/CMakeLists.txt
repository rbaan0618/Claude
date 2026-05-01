cmake_minimum_required(VERSION 3.22.1)
project("g729codec" C)

# Collect all bcg729 source files
file(GLOB BCG729_SOURCES "bcg729/*.c")

# Build shared library: JNI wrapper + bcg729 codec
add_library(g729codec SHARED
    g729_jni.c
    ${BCG729_SOURCES}
)

# bcg729 sources use #include "bcg729/encoder.h" so we need the parent dir
# and also the bcg729 dir itself for internal headers like "typedef.h"
target_include_directories(g729codec PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/bcg729
)

# Link Android log library
find_library(log-lib log)
target_link_libraries(g729codec ${log-lib})
