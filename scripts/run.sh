#!/usr/bin/env bash
# Hypr Buddy — Launch Script
# ===============================
# Starts all three components (daemon, brain, overlay) as background processes.
# Stores PIDs for clean shutdown via stop.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr-buddy"
mkdir -p "$RUNTIME_DIR" && chmod 700 "$RUNTIME_DIR"
PID_FILE="$RUNTIME_DIR/pids"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Check if already running
if [ -f "$PID_FILE" ]; then
    warn "PID file exists. Hypr Buddy may already be running."
    warn "Run './scripts/stop.sh' first, or delete $PID_FILE"
    exit 1
fi

# Ensure runtime directory exists
mkdir -p "$RUNTIME_DIR" && chmod 700 "$RUNTIME_DIR"
PID_FILE="$RUNTIME_DIR/pids"
LOG_DIR="$RUNTIME_DIR/logs"
mkdir -p "$LOG_DIR"

# Check for Wayland
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    error "WAYLAND_DISPLAY is not set. Are you in a Wayland session?"
fi

# Clean up stale sockets
rm -f "$RUNTIME_DIR/brain.sock"
rm -f "$RUNTIME_DIR/overlay.sock"

# Ensure we're in the project directory for relative imports
cd "$PROJECT_DIR"

# Allow buddy name override via env
export HYPR_BUDDY_NAME="${HYPR_BUDDY_NAME:-}"

# -----------------------------------------------------------------------
# Start the Brain
# -----------------------------------------------------------------------
info "Starting brain (logs: $LOG_DIR/brain.log)..."
PYTHONPATH="$PROJECT_DIR" python3 -m brain > "$LOG_DIR/brain.log" 2>&1 &
BRAIN_PID=$!
info "Brain PID: $BRAIN_PID"

# Give the brain a moment to create its socket
sleep 1

# -----------------------------------------------------------------------
# Start the Daemon
# -----------------------------------------------------------------------
info "Starting daemon (logs: $LOG_DIR/daemon.log)..."
PYTHONPATH="$PROJECT_DIR" python3 -m daemon > "$LOG_DIR/daemon.log" 2>&1 &
DAEMON_PID=$!
info "Daemon PID: $DAEMON_PID"

# -----------------------------------------------------------------------
# Start the Overlay
# -----------------------------------------------------------------------
OVERLAY_BIN="$PROJECT_DIR/overlay/target/release/hypr-buddy-overlay"
if [ ! -f "$OVERLAY_BIN" ]; then
    warn "Overlay binary not found. Building..."
    cd "$PROJECT_DIR/overlay" && cargo build --release
    cd "$PROJECT_DIR"
fi

info "Starting overlay (logs: $LOG_DIR/overlay.log)..."
HYPR_BUDDY_ASSETS="$PROJECT_DIR/assets/sprites" "$OVERLAY_BIN" > "$LOG_DIR/overlay.log" 2>&1 &
OVERLAY_PID=$!
info "Overlay PID: $OVERLAY_PID"


# -----------------------------------------------------------------------
# Save PIDs
# -----------------------------------------------------------------------
cat > "$PID_FILE" <<EOF
BRAIN=$BRAIN_PID
DAEMON=$DAEMON_PID
OVERLAY=$OVERLAY_PID
EOF

info "All components started! PIDs saved to $PID_FILE"
info ""
info "  Brain:   $BRAIN_PID"
info "  Daemon:  $DAEMON_PID"
info "  Overlay: $OVERLAY_PID"
info ""
info "To stop: ./scripts/stop.sh"

# -----------------------------------------------------------------------
# Trap for clean shutdown when this script is killed
# -----------------------------------------------------------------------
cleanup() {
    info "Shutting down Hypr Buddy..."
    kill "$BRAIN_PID" "$DAEMON_PID" "$OVERLAY_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    info "Stopped."
}
trap cleanup SIGTERM SIGINT

# Wait for all children
wait
