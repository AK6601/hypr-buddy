#!/usr/bin/env bash
# Build first, then supervise the three desktop processes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr-buddy"
[[ -n "${WAYLAND_DISPLAY:-}" ]] || { echo 'Start this inside a Wayland session.' >&2; exit 1; }
mkdir -p "$RUNTIME_DIR/logs"
chmod 700 "$RUNTIME_DIR"
PID_FILE="$RUNTIME_DIR/pids"
[[ ! -e "$PID_FILE" ]] || { echo 'Already running, or stale PID file. Run scripts/stop.sh first.' >&2; exit 1; }
cd "$PROJECT_DIR"
if [[ -x "$HOME/miniconda3/envs/hypr-buddy/bin/python" ]]; then
    export PATH="$HOME/miniconda3/envs/hypr-buddy/bin:$PATH"
elif [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then
    export PATH="$PROJECT_DIR/venv/bin:$PATH"
fi
# Cargo is incremental: always refresh the binary after source changes.
cargo build --release --manifest-path overlay/Cargo.toml
export PYTHONPATH="$PROJECT_DIR"
export HYPR_BUDDY_CONFIG="$PROJECT_DIR/config"
export HYPR_BUDDY_ASSETS="$PROJECT_DIR/assets/sprites"
pids=()
cleanup() {
    trap - EXIT
    if ((${#pids[@]})); then
        kill "${pids[@]}" 2>/dev/null || true
        wait "${pids[@]}" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
}
trap cleanup EXIT
trap 'exit 0' INT TERM
"$PROJECT_DIR/overlay/target/release/hypr-buddy-overlay" > "$RUNTIME_DIR/logs/overlay.log" 2>&1 &
OVERLAY_PID=$!; pids+=("$OVERLAY_PID")
python3 -m brain > "$RUNTIME_DIR/logs/brain.log" 2>&1 &
BRAIN_PID=$!; pids+=("$BRAIN_PID")
python3 -m daemon > "$RUNTIME_DIR/logs/daemon.log" 2>&1 &
DAEMON_PID=$!; pids+=("$DAEMON_PID")
cat > "$PID_FILE" <<EOF
BRAIN=$BRAIN_PID
DAEMON=$DAEMON_PID
OVERLAY=$OVERLAY_PID
EOF
printf 'Shiro is starting. Logs: %s/logs\nVoice hotkey command: %s/voice_chat.sh\n' "$RUNTIME_DIR" "$SCRIPT_DIR"
# A failed component must not leave two orphaned processes behind.
wait -n "${pids[@]}"
