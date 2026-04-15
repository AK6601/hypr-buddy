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

from ipc.protocol import BRAIN_SOCKET, OVERLAY_SOCKET

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
    event_type = event_data.get("type", "")
    data = event_data.get("data", {})

    # Update chat handler context so LLM has awareness of desktop state
    if chat is not None:
        chat.update_context(event_type, data.get("app_class"))

    # Update mood based on event
    mood.process_event(event_type, data)

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
