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
from brain.learning import LearningEngine
from brain.proactive import ProactiveBehavior
from brain.ambient_vision import AmbientVision
from brain.tts import TTSEngine
from brain.whisper_engine import WhisperEngine
from brain.tools import Tools

logger = logging.getLogger("hypr-buddy.brain")


def load_config() -> dict:
    """Load buddy.toml config."""
    config_path = Path(os.environ.get("HYPR_BUDDY_CONFIG", Path(__file__).resolve().parent.parent / "config")) / "buddy.toml"
    if not config_path.exists():
        logger.warning("Config not found at %s, using defaults", config_path)
        return {}
    with open(config_path, "rb") as f:
        return tomllib.load(f)


CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


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
        # Address of the window whose corner is currently chosen. Used so we
        # keep the SAME corner across geometry updates and only re-pick when a
        # genuinely new window gains focus.
        self.current_addr: str | None = None

        # Behavior config (populated from buddy.toml at startup).
        self.follow_window = True
        self.follow_mode = "nearest_free"
        self.follow_corner = "top-right"
        self.cursor_avoid_distance = 150.0
        self.cursor_safe_distance = 200.0
        self.cursor_move_cooldown = 2.0

    def configure(self, behavior_cfg: dict) -> None:
        """Load movement-tuning keys from the [behavior] config table."""
        self.follow_window = bool(behavior_cfg.get("follow_window", True))
        self.follow_mode = str(behavior_cfg.get("follow_mode", "nearest_free"))
        self.follow_corner = str(behavior_cfg.get("follow_corner", "bottom-right"))
        if self.current_addr is None:
            self.current_corner = self.follow_corner if self.follow_corner in CORNERS else "bottom-right"
        self.cursor_avoid_distance = float(behavior_cfg.get("cursor_avoid_distance", 150))
        self.cursor_safe_distance = float(behavior_cfg.get("cursor_safe_distance", 200))
        self.cursor_move_cooldown = float(behavior_cfg.get("cursor_move_cooldown", 2.0))


_pos = BuddyPos()


def _corner_pos(corner: str, wx: int, wy: int, ww: int, wh: int) -> tuple[int, int]:
    """Top-left (x, y) of the buddy sprite for a given window corner."""
    half_w = _pos.w
    half_h = _pos.h
    if corner == "top-left":
        return wx - half_w - 12, wy - half_h - 12
    if corner == "bottom-left":
        return wx - half_w - 12, wy + wh + 12
    if corner == "bottom-right":
        return wx + ww + 12, wy + wh + 12
    # default: top-right
    return wx + ww + 12, wy - half_h - 12


def _clamp_to_monitor(x: int, y: int, geo: dict) -> tuple[int, int]:
    """Clamp the buddy's top-left so the whole 256x256 sprite stays on-screen.

    Prefers the monitor bounds carried in the geometry payload; falls back to
    the window bounds if no monitor info is present.
    """
    bounds = geo.get("monitor")
    if bounds is None:
        # Fall back to clamping within the window rectangle.
        bounds = {"x": geo["x"], "y": geo["y"], "w": geo["w"], "h": geo["h"]}

    bx, by = bounds["x"], bounds["y"]
    bw, bh = bounds["w"], bounds["h"]

    # If the bounds are smaller than the sprite, just pin to top-left corner.
    max_x = bx + max(bw - _pos.w, 0)
    max_y = by + max(bh - _pos.h, 0)
    cx = min(max(x, bx), max_x)
    cy = min(max(y, by), max_y)
    return cx, cy


async def handle_event(
    event_data: dict,
    mood: MoodSystem,
    reactions: ReactionEngine,
    overlay: OverlayClient,
    tts: TTSEngine,
    personality: Personality,
    db: Database,
    chat: ChatHandler | None = None,
    ambient: AmbientVision | None = None,
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
        chat.update_context(event_type, data)

    # Track fullscreen state for ambient vision (skip captures while fullscreen)
    if event_type == EventType.FULLSCREEN and ambient is not None:
        ambient.set_fullscreen(bool(data.get("fullscreen", False)))

    # Update mood based on event
    mood.process_event(event_type, data)

    # Movement Logic: Follow Active Window
    if (
        _pos.follow_window
        and event_type in (EventType.WINDOW_FOCUS, EventType.WINDOW_MOVE, EventType.WINDOW_RESIZE)
        and (data.get("geometry") or {}).get("monitor")
    ):
        geo = data["geometry"]
        _pos.window_geo = geo
        if geo:
            wx, wy = geo["x"], geo["y"]
            ww, wh = geo["w"], geo["h"]
            addr = geo.get("address")

            # Decide which corner to sit in.
            if _pos.follow_mode == "fixed":
                _pos.current_corner = _pos.follow_corner
            elif _pos.follow_mode == "random":
                # Re-roll only when a new window gains focus, so the buddy
                # doesn't jitter across every geometry update during a drag.
                if event_type == EventType.WINDOW_FOCUS or addr != _pos.current_addr:
                    _pos.current_corner = random.choice(CORNERS)
            else:  # "nearest_free" (default): stable corner, only re-pick on new window
                is_new_window = addr != _pos.current_addr
                if is_new_window:
                    # Minimize obscured window area, then travel; keep ties stable.
                    def score(corner):
                        px, py = _clamp_to_monitor(*_corner_pos(corner, wx, wy, ww, wh), geo)
                        overlap = max(0, min(px + _pos.w, wx + ww) - max(px, wx)) * max(0, min(py + _pos.h, wy + wh) - max(py, wy))
                        travel = (px - _pos.x) ** 2 + (py - _pos.y) ** 2
                        return overlap, corner.startswith("top"), travel, corner != _pos.current_corner
                    _pos.current_corner = min(CORNERS, key=score)

            _pos.current_addr = addr

            x, y = _corner_pos(_pos.current_corner, wx, wy, ww, wh)
            x, y = _clamp_to_monitor(x, y, geo)
            _pos.x, _pos.y = x, y
            await overlay.move_to(int(_pos.x), int(_pos.y))

    # Movement Logic: Avoid Mouse Proximity
    elif _pos.follow_window and event_type == "cursor_move":
        import time
        mx, my = data.get("x", 0), data.get("y", 0)

        # Center of buddy
        bx = _pos.x + _pos.w // 2
        by = _pos.y + _pos.h // 2

        dist = ((mx - bx) ** 2 + (my - by) ** 2) ** 0.5
        now = time.monotonic()

        # If cursor is too close and we haven't hopped recently, find a safe corner.
        if (
            dist < _pos.cursor_avoid_distance
            and _pos.window_geo
            and (now - _pos.last_move > _pos.cursor_move_cooldown)
        ):
            geo = _pos.window_geo
            wx, wy = geo["x"], geo["y"]
            ww, wh = geo["w"], geo["h"]
            half_w, half_h = _pos.w // 2, _pos.h // 2

            # Rank corners by distance from the cursor; pick the farthest one
            # that clears the safe distance (and isn't the current corner).
            candidates: list[tuple[float, str, tuple[int, int]]] = []
            for corner in CORNERS:
                cx, cy = _corner_pos(corner, wx, wy, ww, wh)
                cx, cy = _clamp_to_monitor(cx, cy, geo)
                center_dist = ((mx - (cx + half_w)) ** 2 + (my - (cy + half_h)) ** 2) ** 0.5
                candidates.append((center_dist, corner, (cx, cy)))

            candidates.sort(reverse=True)  # farthest from cursor first
            safe = [c for c in candidates if c[0] > _pos.cursor_safe_distance and c[1] != _pos.current_corner]
            chosen = safe[0] if safe else (candidates[0] if candidates else None)

            if chosen is not None:
                _pos.current_corner = chosen[1]
                _pos.x, _pos.y = chosen[2]
                _pos.last_move = now
                await overlay.move_to(int(_pos.x), int(_pos.y))
                # Visual reaction to being startled.
                await overlay.set_state("surprised", 1.0)

    if tts.listening or tts.conversing:
        return

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
    ambient: AmbientVision | None = None,
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
                        chat=chat, ambient=ambient,
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
    vision_cfg = config.get("vision", {})
    speech_cfg = config.get("speech", {})
    learning_cfg = config.get("learning", {})

    # Configure window-following / movement behavior from [behavior].
    _pos.configure(behavior_cfg)
    overlay_config = Path(os.environ.get("HYPR_BUDDY_CONFIG", Path(__file__).resolve().parent.parent / "config")) / "overlay.toml"
    with overlay_config.open("rb") as f:
        window_cfg = tomllib.load(f).get("window", {})
    _pos.w = int(window_cfg.get("width", 256))
    _pos.h = int(window_cfg.get("height", 256))

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

    mood = MoodSystem(initial=last_mood, config=config.get("mood", {}))
    personality = Personality(char_cfg)
    tools = Tools(data_dir / "memory.db")
    overlay = OverlayClient(OVERLAY_SOCKET)
    tts = TTSEngine(tts_cfg)
    tts.on_playback_start = lambda: overlay.set_state("talking", 180)
    tts.on_playback_end = lambda: overlay.set_state("idle", 0)
    llm = LLMBackend(
        llm_cfg,
        personality,
        tools_engine=tools,
        vision_config=vision_cfg,
        db=db,
        memory_limit=learning_cfg.get("max_facts_injected", 5),
    )
    whisper = WhisperEngine(
        model_name=speech_cfg.get("whisper_model", "small"),
        language=speech_cfg.get("language") or None,
        cpu_threads=speech_cfg.get("cpu_threads", 4),
    )
    reactions = ReactionEngine(behavior_cfg)
    proactive = ProactiveBehavior(behavior_cfg, mood, overlay, tts, personality, reactions)

    # Long-term learning — distills the conversation log into a durable profile.
    learning = LearningEngine(learning_cfg, llm_cfg, db)

    # Chat handler — allows user to talk to the buddy via LLM
    chat = ChatHandler(llm, mood, overlay, tts, db, whisper_engine=whisper)

    # Ambient vision — passive, off-by-default screen awareness.
    ambient = AmbientVision(vision_cfg, llm.vision_provider, chat)

    tasks: list[asyncio.Task] = []

    # Initialize whisper in the background
    # Speech models load on first voice session, leaving VRAM to the text model.
    if llm_cfg.get("warm_start", True):
        tasks.append(asyncio.create_task(llm.prepare(), name="model-warmup"))

    # Event listener (passes chat for context updates)
    tasks.append(asyncio.create_task(
        event_server(mood, reactions, overlay, tts, personality, db, chat=chat, ambient=ambient),
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

    # Ambient vision (self-disables when off)
    tasks.append(asyncio.create_task(ambient.run(), name="ambient-vision"))

    # Long-term learning loop (self-disables when off / explicit_only)
    tasks.append(asyncio.create_task(learning.run(), name="learning"))

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

    def _reload() -> None:
        """SIGHUP: re-read config and live-apply the SAFE subset.

        Only persona / behavior / mood / movement tuning is hot-reloaded by
        mutating the existing instances in place. Structural changes are NOT
        hot-swapped here and require a full restart (see the INFO log below).
        Wrapped so a malformed config never crashes the brain.
        """
        logger.info("SIGHUP received — reloading config...")
        try:
            new_config = load_config()
            new_char_cfg = new_config.get("character", {})

            # Re-apply the environment name override that main() honors.
            env_name_now = os.environ.get("HYPR_BUDDY_NAME")
            if env_name_now:
                new_char_cfg["name"] = env_name_now

            new_behavior_cfg = new_config.get("behavior", {})

            personality.reconfigure(new_char_cfg)
            mood.reconfigure(new_config.get("mood", {}))
            reactions.reconfigure(new_behavior_cfg)
            proactive.reconfigure(new_behavior_cfg)
            _pos.configure(new_behavior_cfg)

            # Optional: memory injection count (safe to change live).
            new_learning_cfg = new_config.get("learning", {})
            llm.memory_limit = int(new_learning_cfg.get("max_facts_injected", llm.memory_limit))

            logger.info("Config reloaded (persona/behavior/mood/movement).")
            logger.info(
                "NOTE: structural changes (LLM provider/model, whisper model, "
                "sockets, learning enable/interval, overlay/sprites) require a "
                "full restart and were NOT hot-swapped."
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Config reload failed (keeping current config): %s", e)

    loop.add_signal_handler(signal.SIGHUP, _reload)

    logger.info("Brain running.")
    await shutdown_event.wait()

    logger.info("Shutting down...")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    whisper.shutdown()
    await tts.close()

    # Best-effort final distillation so the last session's chatter is learned
    # before we close the DB. Distillation is internally guarded and never raises.
    learning_enabled = learning_cfg.get("enabled", True)
    learning_mode = learning_cfg.get("mode", "auto")
    if (
        learning_cfg.get("distill_on_shutdown", True)
        and learning_enabled
        and learning_mode != "explicit_only"
    ):
        logger.info("Running final distillation pass before shutdown...")
        await learning.distill_once()

    await db.save_mood(mood.value)
    await db.close()
    await overlay.close()
    logger.info("Brain stopped.")
