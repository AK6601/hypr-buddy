#!/usr/bin/env bash
# Hypr Buddy — Chat Input
# ============================
# Opens a text prompt and sends the user's message to the buddy's brain.
# The buddy will respond via the overlay speech bubble and TTS.
#
# Hyprland keybinding (add to ~/.config/hypr/hyprland.conf):
#   bind = $mainMod, B, exec, ~/hypr-buddy/scripts/chat.sh
#
# Supports: fuzzel (default on Hyprland), wofi, rofi, bemenu, or zenity.

set -euo pipefail

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr-buddy"
CHAT_SOCKET="$RUNTIME_DIR/chat.sock"

# Check if brain is running
if [ ! -S "$CHAT_SOCKET" ]; then
    notify-send "Hypr Buddy" "Buddy is not running! Start with scripts/run.sh" 2>/dev/null || true
    exit 1
fi

# Auto-detect prompt tool (in preference order for Hyprland)
get_input() {
    local prompt="Talk to $(get_buddy_name):"

    if command -v fuzzel &>/dev/null; then
        echo "" | fuzzel --dmenu --prompt "$prompt " --width 40
    elif command -v wofi &>/dev/null; then
        echo "" | wofi --dmenu --prompt "$prompt"
    elif command -v rofi &>/dev/null; then
        rofi -dmenu -p "$prompt" -theme-str 'window {width: 400px;}'
    elif command -v bemenu &>/dev/null; then
        echo "" | bemenu -p "$prompt"
    elif command -v zenity &>/dev/null; then
        zenity --entry --title="Hypr Buddy" --text="$prompt"
    else
        # Last resort: terminal input
        echo "No GUI prompt found. Install fuzzel, wofi, or rofi." >&2
        echo "Falling back to terminal input." >&2
        read -rp "$prompt " REPLY
        echo "$REPLY"
    fi
}

get_buddy_name() {
    # Try to read name from config
    local config="${HYPR_BUDDY_CONFIG:-$(dirname "$(dirname "$(readlink -f "$0")")")/config}/buddy.toml"
    if [ -f "$config" ]; then
        grep '^name' "$config" 2>/dev/null | head -1 | sed 's/.*= *"\(.*\)".*/\1/' || echo "Buddy"
    else
        echo "Buddy"
    fi
}

# Get user input
MESSAGE=$(get_input 2>/dev/null) || exit 0

# Skip empty messages
if [ -z "${MESSAGE// /}" ]; then
    exit 0
fi

# Send to brain and get response
if command -v socat &>/dev/null; then
    RESPONSE=$(echo "$MESSAGE" | socat -t5 - UNIX-CONNECT:"$CHAT_SOCKET" 2>/dev/null) || true
elif command -v python3 &>/dev/null; then
    RESPONSE=$(python3 -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('$CHAT_SOCKET')
s.sendall((sys.argv[1] + '\n').encode())
s.settimeout(30)
data = b''
while True:
    chunk = s.recv(4096)
    if not chunk or b'\n' in chunk:
        data += chunk
        break
    data += chunk
s.close()
print(data.decode().strip())
" "$MESSAGE" 2>/dev/null) || true
else
    # Fire-and-forget: just send the message, don't wait for response
    echo "$MESSAGE" | socat - UNIX-CONNECT:"$CHAT_SOCKET" 2>/dev/null || true
    exit 0
fi

# Optionally show response as a notification too
if [ -n "${RESPONSE:-}" ]; then
    NAME=$(get_buddy_name)
    notify-send "$NAME" "$RESPONSE" 2>/dev/null || true
fi
