#!/usr/bin/env bash
# Hypr Buddy — Installation Script
# =====================================
# Installs all dependencies on CachyOS / Arch Linux.
# Run this once after cloning the repository.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# -----------------------------------------------------------------------
# 1. System packages (pacman)
# -----------------------------------------------------------------------
info "Installing system packages via pacman..."

PACKAGES=(
    wayland
    wayland-protocols
    python
    python-pip
    rustup
    gcc
    pkg-config
    base-devel
    grim
    slurp
    socat
)

# Piper TTS may be in the AUR on CachyOS
if pacman -Si piper-tts &>/dev/null 2>&1; then
    PACKAGES+=(piper-tts)
else
    warn "piper-tts not found in repos. You may need to install it from AUR:"
    warn "  yay -S piper-tts-bin   OR   paru -S piper-tts-bin"
fi

sudo pacman -S --needed --noconfirm "${PACKAGES[@]}"

# -----------------------------------------------------------------------
# 2. Rust toolchain
# -----------------------------------------------------------------------
info "Setting up Rust toolchain..."
if ! command -v cargo &>/dev/null; then
    rustup default stable
else
    rustup update stable
fi

# -----------------------------------------------------------------------
# 3. Python dependencies
# -----------------------------------------------------------------------
info "Installing Python packages..."
pip install --user --break-system-packages \
    dbus-next \
    structlog \
    httpx \
    'tomli>=2.0.1' \
    pillow \
    cairosvg \
    aiosqlite \
    || pip install --user \
        dbus-next \
        structlog \
        httpx \
        'tomli>=2.0.1' \
        pillow \
        cairosvg \
        aiosqlite

# -----------------------------------------------------------------------
# 4. Create data directories
# -----------------------------------------------------------------------
info "Creating data directories..."
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hypr-buddy"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hypr-buddy"

mkdir -p "$DATA_DIR"
mkdir -p "$CONFIG_DIR"

# -----------------------------------------------------------------------
# 5. Build the Rust overlay
# -----------------------------------------------------------------------
info "Building the overlay (Rust)..."
cd "$PROJECT_DIR/overlay"
cargo build --release
info "Overlay binary: $PROJECT_DIR/overlay/target/release/hypr-buddy-overlay"

# -----------------------------------------------------------------------
# 6. Generate placeholder sprites
# -----------------------------------------------------------------------
info "Using bundled Shiro character artwork."
cd "$PROJECT_DIR"

# -----------------------------------------------------------------------
# 7. Download Piper voice model
# -----------------------------------------------------------------------
info "Setting up voice model..."
bash "$SCRIPT_DIR/setup_voice.sh"

# -----------------------------------------------------------------------
# Done!
# -----------------------------------------------------------------------
echo ""
info "Installation complete!"
info ""
info "Optional: to use the Google Gemini cloud backend, save your API key:"
info "  echo 'YOUR_API_KEY' > $CONFIG_DIR/gemini_key"
info "  chmod 600 $CONFIG_DIR/gemini_key"
info ""
info "To start Hypr Buddy:"
info "  cd $PROJECT_DIR"
info "  ./scripts/run.sh"
info ""
info "To stop:"
info "  ./scripts/stop.sh"
