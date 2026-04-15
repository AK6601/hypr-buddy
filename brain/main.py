"""
Hypr Buddy Brain
====================

The AI/personality engine. Receives events from the daemon, decides reactions,
and sends commands to the overlay renderer.

Architecture:
  - Listens on $XDG_RUNTIME_DIR/hypr-buddy/brain.sock for daemon events
  - Sends commands to overlay via $XDG_RUNTIME_DIR/hypr-buddy/overlay.sock
  - Listens on $XDG_RUNTIME_DIR/hypr-buddy/chat.sock for user chat input
  - Maintains mood state, conversation history, and personality
  - Rule engine for instant reactions + LLM for conversations
  - Proactive behavior on a randomized timer
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipc.protocol import BRAIN_SOCKET, OVERLAY_SOCKET, EventType

from brain.chat import ChatHandler
from brain.mood import MoodSystem
from brain.overlay_client import OverlayClient
from brain.personality import Personality
from brain.persistence import Database
from brain.reactions import ReactionEngine
from brain.llm import LLMBackend
from brain.proactive import ProactiveBehavior
from brain.tts import TTSEngine

logger = logging.getLogger("hypr-buddy.brain")


def load_config() -> dict:
    """Load buddy.toml config."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "buddy.toml"
    if not config_path.exists():
        logger.warning("Config not found at %s, using defaults", config_path)
        return {}
    with open(config_path, "rb") as f:
        return tomllib.load(f)


# Global state for buddy position and current window geometry
class BuddyPos:
    def __init__(self):
        self.x = 0
        self.y = 0
        self.w = 256
        self.h = 256
        self.window_geo = None
        self.last_move = 0.0
        self.current_corner = "top-right"

_pos = BuddyPos()


async def handle_event(
    event_data: dict,
    mood: MoodSystem,
    reactions: ReactionEngine,
    overlay: OverlayClient,
    tts: TTSEngine,
    personality: Personality,
    db: Database,
    chat: ChatHandler | None = None,
) -> None:
    """Process a single event from the daemon."""
    import random
    event_type = event_data.get("type", "")
    data = event_data.get("data", {})

    # Suppress cursor noise in logs
    if event_type != "cursor_move":
        logger.debug("Event: %s -> %s", event_type, data)

    # Update chat handler context so LLM has awareness of desktop state
    if chat is not None:
        chat.update_context(event_type, data.get("app_class"))

    # Update mood based on event
    mood.process_event(event_type, data)

    # Movement Logic: Follow Active Window
    if event_type in (EventType.WINDOW_FOCUS, EventType.WINDOW_MOVE, EventType.WINDOW_RESIZE) and "geometry" in data:
        _pos.window_geo = data["geometry"]
        if _pos.window_geo:
            wx, wy = _pos.window_geo["x"], _pos.window_geo["y"]
            ww, wh = _pos.window_geo["w"], _pos.window_geo["h"]
            
            # If focus changed, pick a new random corner
            if event_type == EventType.WINDOW_FOCUS:
                _pos.current_corner = random.choice(["top-left", "top-right", "bottom-left", "bottom-right"])

            # Calculate coordinates based on chosen corner
            if _pos.current_corner == "top-left":
                _pos.x, _pos.y = wx - _pos.w // 2, wy - _pos.h // 2
            elif _pos.current_corner == "top-right":
                _pos.x, _pos.y = wx + ww - _pos.w // 2, wy - _pos.h // 2
            elif _pos.current_corner == "bottom-left":
                _pos.x, _pos.y = wx - _pos.w // 2, wy + wh - _pos.h // 2
            elif _pos.current_corner == "bottom-right":
                _pos.x, _pos.y = wx + ww - _pos.w // 2, wy + wh - _pos.h // 2

            await overlay.move_to(int(_pos.x), int(_pos.y))

    # Movement Logic: Avoid Mouse Proximity
    elif event_type == "cursor_move":
        import time
        mx, my = data.get("x", 0), data.get("y", 0)
        
        # Center of buddy
        bx = _pos.x + _pos.w // 2
        by = _pos.y + _pos.h // 2
        
        dist = ((mx - bx)**2 + (my - by)**2)**0.5
        now = time.time()
        
        # If cursor is within 150px and we haven't hopped in the last 2 seconds
        if dist < 150 and _pos.window_geo and (now - _pos.last_move > 2.0):
            # Define possible window corners
            wx, wy = _pos.window_geo["x"], _pos.window_geo["y"]
            ww, wh = _pos.window_geo["w"], _pos.window_geo["h"]
            
            corners = [
                (wx - _pos.w // 2, wy - _pos.h // 2),        # Top-Left
                (wx + ww - _pos.w // 2, wy - _pos.h // 2),   # Top-Right
                (wx - _pos.w // 2, wy + wh - _pos.h // 2),   # Bottom-Left
                (wx + ww - _pos.w // 2, wy + wh - _pos.h // 2), # Bottom-Right
            ]
            
            # Filter corners: must be at least 200px from mouse
            safe_corners = [c for c in corners if ((mx - (c[0] + _pos.w // 2))**2 + (my - (c[1] + _pos.h // 2))**2)**0.5 > 200]
            
            if safe_corners:
                _pos.x, _pos.y = random.choice(safe_corners)
                _pos.last_move = now
                await overlay.move_to(int(_pos.x), int(_pos.y))
                # Optional: visual reaction to being startled
                await overlay.set_state("surprised", 1.0)

    # Check for a reaction
    reaction = reactions.get_reaction(event_type, data, mood.value)
    if reaction is None:
        return

    state = reaction.get("state", "happy")
    text = reaction.get("say")
    duration = reaction.get("duration", 5.0)

    if text:
        # Apply personality flavor to the text
        flavored = personality.flavor_text(text, mood.value)
        await overlay.say(flavored, state)
        await tts.speak(flavored)
        await db.log_interaction(event_type, flavored)
    else:
        await overlay.set_state(state, duration)


async def event_server(
    mood: MoodSystem,
    reactions: ReactionEngine,
    overlay: OverlayClient,
    tts: TTSEngine,
    personality: Personality,
    db: Database,
    chat: ChatHandler | None = None,
) -> None:
    """Listen for events from the daemon on a Unix socket."""
    import json

    # Remove stale socket
    sock_path = Path(BRAIN_SOCKET)
    if sock_path.exists():
        sock_path.unlink()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        addr = writer.get_extra_info("peername")
        logger.info("Daemon connected: %s", addr)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    event_data = json.loads(line.decode())
                    await handle_event(
                        event_data, mood, reactions, overlay, tts, personality, db,
                        chat=chat,
                    )
                except json.JSONDecodeError as e:
                    logger.warning("Invalid JSON from daemon: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle_client, path=BRAIN_SOCKET)
    logger.info("Brain listening on %s", BRAIN_SOCKET)

    try:
        await server.serve_forever()
    finally:
        server.close()
        if sock_path.exists():
            sock_path.unlink()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Hypr Buddy Brain starting...")

    config = load_config()
    char_cfg = config.get("character", {})
    
    # Allow environment override for name
    env_name = os.environ.get("HYPR_BUDDY_NAME")
    if env_name:
        char_cfg["name"] = env_name
        logger.info("Buddy name overridden by environment: %s", env_name)

    behavior_cfg = config.get("behavior", {})
    tts_cfg = config.get("tts", {})
    llm_cfg = config.get("llm", {})

    # Ensure data directory exists
    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "hypr-buddy"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Initialize subsystems
    db = Database(data_dir / "memory.db")
    await db.initialize()

    # Restore mood from last session
    last_mood = await db.get_last_mood()

    mood = MoodSystem(initial=last_mood)
    personality = Personality(char_cfg)
    overlay = OverlayClient(OVERLAY_SOCKET)
    tts = TTSEngine(tts_cfg)
    llm = LLMBackend(llm_cfg, personality)
    reactions = ReactionEngine(behavior_cfg)
    proactive = ProactiveBehavior(behavior_cfg, mood, overlay, tts, personality, reactions)

    # Chat handler — allows user to talk to the buddy via LLM
    chat = ChatHandler(llm, mood, overlay, tts, db)

    tasks: list[asyncio.Task] = []

    # Event listener (passes chat for context updates)
    tasks.append(asyncio.create_task(
        event_server(mood, reactions, overlay, tts, personality, db, chat=chat),
        name="event-server",
    ))

    # Chat input listener
    tasks.append(asyncio.create_task(chat.run(), name="chat-server"))

    # Mood decay
    tasks.append(asyncio.create_task(mood.decay_loop(), name="mood-decay"))

    # Mood persistence (save every 60s)
    async def persist_mood() -> None:
        while True:
            await asyncio.sleep(60)
            await db.save_mood(mood.value)

    tasks.append(asyncio.create_task(persist_mood(), name="mood-persist"))

    # Proactive behavior
    tasks.append(asyncio.create_task(proactive.run(), name="proactive"))

    # Startup greeting
    logger.info("Waiting for overlay to be ready...")
    await overlay.wait_ready(timeout=15.0)
    
    greeting = personality.get_greeting()
    await overlay.say(greeting, "waving")
    await tts.speak(greeting)

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _shutdown() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    logger.info("Brain running.")
    await shutdown_event.wait()

    logger.info("Shutting down...")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await db.save_mood(mood.value)
    await db.close()
    await overlay.close()
    logger.info("Brain stopped.")
