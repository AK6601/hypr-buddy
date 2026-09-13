#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
if [[ -x "$HOME/miniconda3/envs/hypr-buddy/bin/python" ]]; then
    PYTHON="$HOME/miniconda3/envs/hypr-buddy/bin/python"
elif [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then
    PYTHON="$PROJECT_DIR/venv/bin/python"
else
    PYTHON=python3
fi
exec "$PYTHON" "$SCRIPT_DIR/voice_chat.py" "$@"
