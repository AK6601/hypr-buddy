# Hypr Buddy

A desktop companion/mascot application for **CachyOS** with **Hyprland** (Wayland). A persistent animated character floats on top of all windows, reacts to desktop events, notifications, and active windows, speaks via TTS, and can hold conversations via LLM integration.

![Screenshot Placeholder](assets/screenshot-placeholder.png)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Desktop (Hyprland)                         │
│                                                                     │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────────────┐ │
│  │   Daemon     │────▶│    Brain     │────▶│      Overlay         │ │
│  │  (Python)    │     │  (Python)    │     │      (Rust)          │ │
│  │              │     │              │     │                      │ │
│  │ • Hyprland   │     │ • Personality│     │ • Layer-shell surface│ │
│  │   IPC events │     │ • Mood system│     │ • Sprite animation   │ │
│  │ • D-Bus      │     │ • Rule engine│     │ • Speech bubbles     │ │
│  │   notifs     │     │ • LLM chat   │     │ • Transparent overlay│ │
│  │ • System     │     │ • TTS engine │     │ • Input handling     │ │
│  │   monitors   │     │ • Proactive  │     │                      │ │
│  │              │     │   behavior   │     │                      │ │
│  └──────┬───────┘     └──────┬───────┘     └──────────┬───────────┘ │
│         │                    │                        │             │
│         │  Unix Socket       │  Unix Socket           │             │
│         │  (brain.sock)      │  (overlay.sock)        │             │
│         └────────────────────┘────────────────────────┘             │
│                                                                     │
│  ┌──────────────┐     ┌─────────────┐     ┌──────────────────────┐  │
│  │ SQLite DB    │     │ Config TOML │     │   Sprite Sheets      │  │
│  │ (~/.local/   │     │ (config/)   │     │   (assets/sprites/)  │  │
│  │  share/)     │     │             │     │                      │  │
│  └──────────────┘     └─────────────┘     └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Daemon** monitors Hyprland IPC, D-Bus notifications, and system state
2. Events are normalized to JSON and sent to the **Brain** via Unix socket
3. **Brain** processes events through the mood system and rule engine
4. Brain sends animation/speech commands to the **Overlay** via Unix socket
5. **Overlay** renders the sprite and speech bubbles on a Wayland layer-shell surface

## Prerequisites

- **CachyOS** (or any Arch-based distro)
- **Hyprland** (Wayland compositor with wlr-layer-shell support)
- **Python 3.11+**
- **Rust** (stable toolchain)
- **Piper TTS** (optional, for voice)
- **Ollama** (optional, for LLM conversations)

## Installation

### Quick Install

```bash
git clone https://github.com/yourusername/hypr-buddy.git
cd hypr-buddy
./scripts/install.sh
```

The install script will:
1. Install system packages via `pacman`
2. Set up the Rust toolchain
3. Install Python dependencies
4. Build the Rust overlay
5. Generate placeholder sprites
6. Download the Piper voice model

### Manual Install

```bash
# System packages
sudo pacman -S wayland wayland-protocols python python-pip rustup gcc pkg-config

# Rust
rustup default stable

# Python packages
pip install --user dbus-next structlog httpx tomli pillow cairosvg aiosqlite

# Build overlay
cd overlay && cargo build --release && cd ..

# Generate sprites
python3 scripts/generate_placeholders.py

# Download voice model (optional)
./scripts/setup_voice.sh
```

## Usage

### Start

```bash
./scripts/run.sh
```

This launches all three components as background processes. The buddy will appear in the bottom-right corner of your screen.

### Stop

```bash
./scripts/stop.sh
```

### Talk to Your Buddy

Chat with the buddy using the built-in prompt. The brain uses your configured LLM (Ollama or Anthropic) to generate responses.

**Option 1: Hyprland keybinding (recommended)**

Add to `~/.config/hypr/hyprland.conf`:

```
bind = $mainMod, B, exec, ~/hypr-buddy/scripts/chat.sh
```

Press `Super+B` to open a fuzzel/wofi prompt, type your message, and the buddy responds via speech bubble + TTS.

**Option 2: Command line**

```bash
# Via socat
echo "How are you today?" | socat -t5 - UNIX-CONNECT:$XDG_RUNTIME_DIR/hypr-buddy/chat.sock

# Via the chat script
./scripts/chat.sh
```

Supports: **fuzzel** (default on Hyprland), wofi, rofi, bemenu, zenity.

### Send Overlay Commands Manually

You can also control the overlay directly for testing:

```bash
SOCK="$XDG_RUNTIME_DIR/hypr-buddy/overlay.sock"

# Make her say something
echo '{"cmd": "say", "text": "Hello world!", "state": "talking"}' | socat - UNIX-CONNECT:$SOCK

# Change emotional state
echo '{"cmd": "set_state", "state": "happy", "duration": 5.0}' | socat - UNIX-CONNECT:$SOCK

# Hide/show
echo '{"cmd": "visibility", "visible": false}' | socat - UNIX-CONNECT:$SOCK
```

## Configuration

All configuration is in the `config/` directory as TOML files.

### Character Personality (`config/buddy.toml`)

```toml
[character]
name = "Miku"
personality = "Friendly, slightly nerdy, encouraging..."
greeting_morning = "Good morning! Ready to take on the day?"
```

Customize the name, personality description, and time-based greetings. The personality description is used as context for LLM conversations.

### LLM Integration (`config/buddy.toml`)

```toml
[llm]
backend = "ollama"          # "ollama" or "anthropic"
model = "mistral"           # Model name
ollama_url = "http://localhost:11434"
```

**Ollama (Local, Default):** Install [Ollama](https://ollama.ai), pull a model (`ollama pull mistral`), and it works out of the box.

**Anthropic API:** Set `backend = "anthropic"`, choose a model like `claude-sonnet-4-5-20250514`, and put your API key in `~/.config/hypr-buddy/anthropic_key`.

### Event Monitoring (`config/events.toml`)

Control which desktop events the buddy reacts to:

```toml
[hyprland]
watch_activewindow = true   # React to app focus changes
watch_fullscreen = true     # Go quiet during fullscreen

[notifications]
enabled = true
ignore_apps = ["Spotify", "Discord"]  # Don't react to these

[ignore]
window_classes = ["polkit"]  # Ignore these window types
```

### Overlay Appearance (`config/overlay.toml`)

```toml
[window]
anchor = "bottom-right"     # Screen corner: bottom-right, bottom-left, top-right, top-left
margin_x = 50               # Distance from screen edge
margin_y = 50

[animation]
idle_fps = 8                # Low FPS when idle (saves CPU)
talking_fps = 12            # Higher FPS when active
transition_ms = 200         # Crossfade duration between states
```

## Customization

### Custom Sprites

Replace the sprite sheets in `assets/sprites/`. Each state needs a horizontal strip PNG:

- **Format:** 4 frames side-by-side (1024x256 for 256x256 sprites)
- **Required states:** idle, talking, happy, sad, surprised, thinking, sleeping, waving, angry
- **Transparency:** Use alpha channel for transparent background

To regenerate placeholders: `python3 scripts/generate_placeholders.py`

### Custom Reactions

Edit `brain/reactions.py` to add new reaction rules. Each rule specifies:

```python
(
    "event_type",                              # Event to match
    lambda data, mood: condition(data, mood),  # Match function
    [                                          # Possible responses
        {"state": "happy", "say": "Response text!"},
        {"state": "happy", "say": "Alternative response!"},
    ],
)
```

### Custom Proactive Lines

Edit `brain/proactive.py` and add to the `PROACTIVE_LINES` dictionary.

## Project Structure

```
hypr-buddy/
├── overlay/              # Rust — Wayland layer-shell overlay renderer
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs       # Entry point, main loop
│       ├── config.rs     # TOML config loading
│       ├── ipc.rs        # Unix socket command listener
│       ├── sprite.rs     # Sprite sheet loading & animation
│       ├── speech.rs     # Speech bubble rendering
│       └── renderer.rs   # Pixel buffer & compositing
├── daemon/               # Python — Desktop event listener
│   ├── main.py           # Entry point, async orchestration
│   ├── hyprland.py       # Hyprland IPC event stream
│   ├── notifications.py  # D-Bus notification monitoring
│   ├── system.py         # Battery, time-of-day monitors
│   └── sender.py         # Unix socket event sender
├── brain/                # Python — AI personality engine
│   ├── main.py           # Entry point, event processing
│   ├── mood.py           # Mood system (-1.0 to 1.0)
│   ├── personality.py    # Character definition & text flavoring
│   ├── reactions.py      # Rule-based reaction engine
│   ├── llm.py            # Ollama/Anthropic LLM backend
│   ├── chat.py           # User chat input via LLM
│   ├── tts.py            # Piper TTS speech queue
│   ├── proactive.py      # Timer-based proactive behavior
│   ├── overlay_client.py # Sends commands to overlay
│   └── persistence.py    # SQLite storage
├── ipc/                  # Shared protocol definitions
│   └── protocol.py       # Message types & socket paths
├── assets/
│   └── sprites/          # Generated sprite sheet PNGs
├── config/
│   ├── buddy.toml        # Personality, LLM, TTS settings
│   ├── events.toml       # Event monitoring settings
│   └── overlay.toml      # Overlay appearance settings
├── scripts/
│   ├── install.sh        # Full installation
│   ├── run.sh            # Launch all components
│   ├── stop.sh           # Stop all components
│   ├── chat.sh           # Chat prompt (fuzzel/wofi) for talking to buddy
│   ├── setup_voice.sh    # Download Piper voice model
│   └── generate_placeholders.py  # Sprite sheet generator
├── pyproject.toml        # Python project configuration
└── README.md
```

## Troubleshooting

### "Hyprland socket not found"

- Make sure Hyprland is running
- Check that `$HYPRLAND_INSTANCE_SIGNATURE` is set: `echo $HYPRLAND_INSTANCE_SIGNATURE`
- The socket should be at `$XDG_RUNTIME_DIR/hypr/<signature>/.socket2.sock`

### "Cannot connect to brain/overlay"

- Components start in order: brain first, then daemon, then overlay
- Check if sockets exist: `ls -la $XDG_RUNTIME_DIR/hypr-buddy/`
- Check logs: each component logs to stderr
- Remove stale sockets: `rm $XDG_RUNTIME_DIR/hypr-buddy/*.sock`

### Overlay doesn't appear

- Ensure your compositor supports `wlr-layer-shell-unstable-v1`
- Check Hyprland version: `hyprctl version`
- Try running the overlay manually: `RUST_LOG=debug ./overlay/target/release/hypr-buddy-overlay`

### Piper TTS not working

- Install Piper: `sudo pacman -S piper-tts` or from AUR
- Download a voice model: `./scripts/setup_voice.sh`
- Test: `echo "Hello" | piper --model ~/.local/share/piper-voices/en_US-amy-medium.onnx --output_raw | aplay -r 22050 -f S16_LE -c 1`

### High CPU usage

- The overlay should use < 2% CPU when idle (8 FPS, sleeps between frames)
- If CPU is high, check `overlay.toml` — lower `idle_fps` to 4
- The daemon and brain are event-driven and should use near-zero CPU when idle

### Notifications not detected

- The daemon needs access to the session D-Bus
- Ensure `dbus-next` is installed: `pip install dbus-next`
- Some notification daemons may need `BecomeMonitor` permission — the daemon falls back to `AddMatch` if this fails

## Future Plans

- **Live2D Support** — Replace sprite sheets with Live2D models for fluid animation
- **Screen OCR** — Read on-screen text to understand context better
- **Whisper Voice Input** — Talk to your buddy with your voice
- **Waybar Integration** — Show mood/status as a Waybar module
- **Plugin System** — User-defined reaction modules
- **Multi-monitor** — Follow the cursor across monitors
- **Theme Support** — Multiple character skins/themes

## License

MIT
