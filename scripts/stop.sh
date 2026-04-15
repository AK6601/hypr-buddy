#!/usr/bin/env bash
# Hypr Buddy — Stop Script
# =============================
# Reads PIDs from the pid file and gracefully shuts down all components.

set -euo pipefail

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr-buddy"
PID_FILE="$RUNTIME_DIR/pids"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

if [ ! -f "$PID_FILE" ]; then
    warn "No PID file found at $PID_FILE"
    warn "Hypr Buddy may not be running."

    # Try to kill by name as a fallback
    info "Attempting to kill by process name..."
    pkill -f "python3 -m brain" 2>/dev/null && info "Killed brain" || true
    pkill -f "python3 -m daemon" 2>/dev/null && info "Killed daemon" || true
    pkill -f "hypr-buddy-overlay" 2>/dev/null && info "Killed overlay" || true
    exit 0
fi

info "Reading PIDs from $PID_FILE..."

# Parse PIDs safely — only accept lines matching KEY=DIGITS (no shell eval)
declare -A PIDS
while IFS='=' read -r key value; do
    # Strip whitespace and validate: key must be alphanumeric, value must be digits only
    key="${key// /}"
    value="${value// /}"
    if [[ "$key" =~ ^[A-Z_]+$ ]] && [[ "$value" =~ ^[0-9]+$ ]]; then
        PIDS["$key"]="$value"
    fi
done < "$PID_FILE"

# Send SIGTERM for graceful shutdown
for component in OVERLAY DAEMON BRAIN; do
    pid="${PIDS[$component]:-}"
    if [ -n "$pid" ]; then
        if kill -0 "$pid" 2>/dev/null; then
            info "Stopping $component (PID $pid)..."
            kill "$pid"
        else
            warn "$component (PID $pid) is not running"
        fi
    fi
done

# Wait a moment for graceful shutdown
sleep 2

# Force kill any remaining
for component in OVERLAY DAEMON BRAIN; do
    pid="${PIDS[$component]:-}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        warn "$component (PID $pid) didn't stop gracefully, force killing..."
        kill -9 "$pid" 2>/dev/null || true
    fi
done

# Clean up
rm -f "$PID_FILE"
rm -f "$RUNTIME_DIR/brain.sock"
rm -f "$RUNTIME_DIR/overlay.sock"

info "Hypr Buddy stopped."
