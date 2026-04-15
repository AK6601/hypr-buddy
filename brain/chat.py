"""
Chat Input Handler
===================

Listens on a Unix domain socket for user text input. When the user sends
a message (via the chat.sh script or any client), it's passed to the LLM
backend for a conversational response, which is then spoken and displayed
via the overlay.

The chat socket accepts simple newline-terminated text. No JSON wrapping
needed — just plain text, one message per line.

Typical usage with Hyprland:
  1. User presses a hotkey (e.g., Super+B)
  2. fuzzel/wofi pops up as a text prompt
  3. User types a message and hits Enter
  4. scripts/chat.sh sends it to this socket
  5. LLM generates a response
  6. Response appears in the overlay speech bubble + TTS
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from brain.llm import LLMBackend
from brain.mood import MoodSystem
from brain.overlay_client import OverlayClient
from brain.persistence import Database
from brain.tts import TTSEngine

logger = logging.getLogger("hypr-buddy.brain.chat")


def _chat_socket_path() -> str:
    """Return the path for the chat input socket."""
    candidates = [
        os.environ.get("XDG_RUNTIME_DIR"),
        f"/run/user/{os.getuid()}",
    ]
    for base in candidates:
        if base and os.path.isdir(base):
            return os.path.join(base, "hypr-buddy", "chat.sock")
    import tempfile
    return os.path.join(tempfile.gettempdir(), f"hypr-buddy-{os.getuid()}", "chat.sock")


CHAT_SOCKET = _chat_socket_path()


class ChatHandler:
    """Listens for user chat messages and responds via LLM."""

    def __init__(
        self,
        llm: LLMBackend,
        mood: MoodSystem,
        overlay: OverlayClient,
        tts: TTSEngine,
        db: Database,
    ) -> None:
        self._llm = llm
        self._mood = mood
        self._overlay = overlay
        self._tts = tts
        self._db = db
        # Track recent events for LLM context
        self._recent_events: list[str] = []
        self._active_app: str | None = None

    def update_context(self, event_type: str, app_class: str | None = None) -> None:
        """Update context from desktop events (called by main event handler)."""
        self._recent_events.append(event_type)
        if len(self._recent_events) > 10:
            self._recent_events = self._recent_events[-10:]
        if app_class:
            self._active_app = app_class

    async def run(self) -> None:
        """Start the chat socket server."""
        sock_path = Path(CHAT_SOCKET)

        # Ensure parent directory exists
        sock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)

        # Remove stale socket
        if sock_path.exists():
            sock_path.unlink()

        async def handle_client(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break

                    user_text = line.decode("utf-8", errors="replace").strip()
                    if not user_text:
                        continue

                    logger.info("User says: %s", user_text)

                    # Show thinking state while LLM generates
                    await self._overlay.set_state("thinking", 0)

                    # Get LLM response
                    from datetime import datetime
                    hour = datetime.now().hour
                    if 5 <= hour < 12:
                        period = "morning"
                    elif 12 <= hour < 17:
                        period = "afternoon"
                    elif 17 <= hour < 21:
                        period = "evening"
                    else:
                        period = "night"

                    response = await self._llm.chat(
                        user_message=user_text,
                        mood_label=self._mood.label,
                        active_app=self._active_app,
                        time_period=period,
                        recent_events=self._recent_events,
                    )

                    logger.info("Buddy says: %s", response)

                    # Display and speak the response
                    await self._overlay.say(response, "talking")
                    await self._tts.speak(response)

                    # Log the conversation
                    await self._db.log_conversation("user", user_text)
                    await self._db.log_conversation("assistant", response)

                    # Mood boost from conversation
                    self._mood.process_event("conversation", {})

                    # Send response back to the client (so chat.sh can show it)
                    writer.write((response + "\n").encode())
                    await writer.drain()

            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Chat handler error: %s", e)
            finally:
                writer.close()
                await writer.wait_closed()

        # Set restrictive umask before socket creation to avoid a permissions
        # race window (socket is world-accessible between create and chmod).
        old_umask = os.umask(0o177)  # Only owner can read/write
        try:
            server = await asyncio.start_unix_server(handle_client, path=str(sock_path))
        finally:
            os.umask(old_umask)
        logger.info("Chat server listening on %s", sock_path)

        try:
            await server.serve_forever()
        finally:
            server.close()
            if sock_path.exists():
                sock_path.unlink()
