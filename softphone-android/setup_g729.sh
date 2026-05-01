#!/bin/bash
#
# Downloads bcg729 (open-source G.729 codec, BSD license) from GitHub
# Run this once before building the project.
#
# Usage: bash setup_g729.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CPP_DIR="$SCRIPT_DIR/app/src/main/cpp/bcg729"
TEMP_DIR="$SCRIPT_DIR/.bcg729_tmp"

echo "=== Setting up bcg729 G.729 codec ==="

# Clean previous
rm -rf "$TEMP_DIR"
mkdir -p "$CPP_DIR"

# Clone bcg729 from GitLab (Ouvaton)
echo "Downloading bcg729..."
git clone --depth 1 https://gitlab.ouvaton.org/ouvaton/bcg729.git "$TEMP_DIR" 2>/dev/null || {
    echo "GitLab failed, trying GitHub mirror..."
    git clone --depth 1 https://github.com/nicovoice/bcg729.git "$TEMP_DIR" 2>/dev/null || {
        echo ""
        echo "ERROR: Could not download bcg729. Please manually download from:"
        echo "  https://gitlab.ouvaton.org/ouvaton/bcg729"
        echo ""
        echo "Then copy the .c and .h files from src/ and include/ into:"
        echo "  $CPP_DIR/"
        exit 1
    }
}

# Remove stub (will be replaced by real implementation)
rm -f "$CPP_DIR/bcg729_stub.c"

# Copy source files
echo "Copying source files..."
if [ -d "$TEMP_DIR/src" ]; then
    cp "$TEMP_DIR"/src/*.c "$CPP_DIR/" 2>/dev/null || true
    cp "$TEMP_DIR"/src/*.h "$CPP_DIR/" 2>/dev/null || true
fi
if [ -d "$TEMP_DIR/include" ]; then
    cp "$TEMP_DIR"/include/*.h "$CPP_DIR/" 2>/dev/null || true
fi

# Clean up
rm -rf "$TEMP_DIR"

# Verify
SRC_COUNT=$(ls "$CPP_DIR"/*.c 2>/dev/null | wc -l)
HDR_COUNT=$(ls "$CPP_DIR"/*.h 2>/dev/null | wc -l)

echo ""
echo "=== Done ==="
echo "Copied $SRC_COUNT source files and $HDR_COUNT header files to:"
echo "  $CPP_DIR/"
echo ""
echo "You can now build the project in Android Studio."
