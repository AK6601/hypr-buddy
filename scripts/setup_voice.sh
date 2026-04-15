#!/usr/bin/env bash
# Virtual Buddy — Voice Model Setup
# ====================================
# Downloads a Piper TTS voice model for speech synthesis.
#
# Piper (https://github.com/rhasspy/piper) is a fast, local TTS engine
# that uses ONNX models for neural speech synthesis.
#
# This script downloads the "amy" English voice (medium quality, good balance
# of quality vs speed).

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

VOICE_DIR="${HOME}/.local/share/piper-voices"
MODEL_NAME="en_US-amy-medium"
MODEL_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx"
CONFIG_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx.json"

mkdir -p "$VOICE_DIR"

MODEL_PATH="$VOICE_DIR/${MODEL_NAME}.onnx"
CONFIG_PATH="$VOICE_DIR/${MODEL_NAME}.onnx.json"

if [ -f "$MODEL_PATH" ] && [ -f "$CONFIG_PATH" ]; then
    info "Voice model already exists at $MODEL_PATH"
    exit 0
fi

info "Downloading Piper voice model: $MODEL_NAME"
info "This may take a moment (~60MB)..."

# Download model
if command -v curl &>/dev/null; then
    curl -L --progress-bar -o "$MODEL_PATH" "$MODEL_URL"
    curl -L -s -o "$CONFIG_PATH" "$CONFIG_URL"
elif command -v wget &>/dev/null; then
    wget -q --show-progress -O "$MODEL_PATH" "$MODEL_URL"
    wget -q -O "$CONFIG_PATH" "$CONFIG_URL"
else
    warn "Neither curl nor wget found. Please install one and re-run."
    warn "Model URL: $MODEL_URL"
    warn "Config URL: $CONFIG_URL"
    warn "Download both to: $VOICE_DIR/"
    exit 1
fi

info "Voice model installed at $MODEL_PATH"
info ""
info "To test: echo 'Hello, I am your virtual buddy!' | piper --model $MODEL_PATH --output_raw | aplay -r 22050 -f S16_LE -c 1"
