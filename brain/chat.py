"""Ordered text/voice conversations over a private Unix socket.

Plain lines are text turns; AUDIO:<path> consumes a private WAV recording.
/listen reserves a voice session and shows microphone state. Voice responses
are one JSON line sent after speech playback. /stop interrupts the active turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
from pathlib import Path

from brain.llm import LLMBackend
from brain.mood import MoodSystem
from brain.overlay_client import OverlayClient
from brain.persistence import Database
from brain.tts import TTSEngine
from brain.whisper_engine import WhisperEngine

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
        whisper_engine: WhisperEngine | None = None,
    ) -> None:
        self._llm = llm
        self._mood = mood
        self._overlay = overlay
        self._tts = tts
        self._db = db
        self._whisper = whisper_engine
        self._turn_lock = asyncio.Lock()
        self._voice_owner = None
        self._active_turn = None
        self._voice_task = None
        # Track recent events for LLM context
        self._recent_events: list[str] = []
        self._active_app: str | None = None
        self._system_stats: dict[str, int] = {}
        # Latest one-line ambient screen description (set by AmbientVision).
        self._ambient_view: str | None = None

    def update_context(self, event_type: str, data: dict | None = None) -> None:
        """Update context from desktop events (called by main event handler)."""
        # ambient_view is a synthetic context update (not a desktop event), so
        # it doesn't belong in the recent_events feed — store it and return.
        if event_type == "ambient_view":
            if data:
                self._ambient_view = data.get("desc")
            return

        if event_type in ("cursor_move", "window_move", "window_resize"):
            return

        self._recent_events.append(event_type)
        if len(self._recent_events) > 10:
            self._recent_events = self._recent_events[-10:]

        if data:
            if event_type == "system_stats":
                self._system_stats.update(data)
            elif "app_class" in data:
                self._active_app = data["app_class"]

    async def _handle_slash_command(
        self,
        text: str,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """Handle /model and /vision provider-swap commands.

        Returns True if the line was a recognized slash-command (so the caller
        should NOT forward it to the LLM).
        """
        parts = text.split()
        cmd = parts[0].lower()

        if cmd not in ("/model", "/vision"):
            return False

        if len(parts) < 2:
            reply = f"Usage: {cmd} <provider> [model]"
            await self._overlay.say(reply, "thinking")
            writer.write((reply + "\n").encode())
            await writer.drain()
            return True

        provider = parts[1]
        model = parts[2] if len(parts) > 2 else None

        if cmd == "/model":
            self._llm.set_text_provider(provider, model)
            reply = f"Text model: {provider}" + (f" ({model})" if model else "")
        else:  # /vision
            self._llm.set_vision_provider(provider, model)
            reply = f"Vision model: {provider}" + (f" ({model})" if model else "")

        logger.info("Slash command: %s", text)
        await self._overlay.say(reply, "happy")
        writer.write((reply + "\n").encode())
        await writer.drain()
        return True

    async def run(self) -> None:
        """Start the chat socket server."""
        sock_path = Path(CHAT_SOCKET)

        # Ensure parent directory exists
        sock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)

        # Remove stale socket
        if sock_path.exists():
            sock_path.unlink()

        clients: set[asyncio.Task] = set()

        async def handle_client(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            clients.add(asyncio.current_task())
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break

                    user_text = line.decode("utf-8", errors="replace").strip()
                    if not user_text:
                        continue

                    if user_text == "/stop":
                        for task in {self._active_turn, self._voice_task} - {None, asyncio.current_task()}:
                            task.cancel()
                        await self._tts.interrupt()
                        await self._overlay.set_state("idle", 0)
                        writer.write(b"Stopped.\n")
                        await writer.drain()
                        continue
                    if user_text == "/listen":
                        if self._voice_owner not in (None, writer):
                            writer.write(b"ERROR: Another voice session is active.\n")
                        else:
                            self._voice_owner = writer
                            self._voice_task = asyncio.current_task()
                            await self._tts.interrupt()
                            self._tts.listening = True
                            await self._overlay.say("Listening…", "thinking")
                            writer.write(b"Listening.\n")
                        await writer.drain()
                        continue

                    audio = user_text.startswith("AUDIO:")
                    if audio:
                        audio_path = Path(user_text[6:].strip())
                        # Only consume recordings created in our private runtime directory.
                        allowed = Path(CHAT_SOCKET).parent / "recordings"
                        if not self._whisper or audio_path.is_symlink() or audio_path.resolve().parent != allowed.resolve() or not audio_path.is_file():
                            writer.write(b"ERROR: Voice unavailable or invalid recording.\n")
                            await writer.drain()
                            continue
                        try:
                            await self._overlay.set_state("thinking", 0)
                            user_text = await self._whisper.transcribe(audio_path)
                        finally:
                            audio_path.unlink(missing_ok=True)
                        self._tts.listening = False
                        if not user_text:
                            writer.write(b"ERROR: No speech detected.\n")
                            await writer.drain()
                            await self._overlay.set_state("idle", 0)
                            continue

                    # Slash-commands: runtime provider swaps. Handled locally,
                    # never sent to the LLM.
                    if user_text.startswith("/"):
                        handled = await self._handle_slash_command(user_text, writer)
                        if handled:
                            continue

                    async with self._turn_lock:
                        self._active_turn = asyncio.current_task()
                        self._tts.conversing = True
                        try:
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
                                system_stats=self._system_stats,
                                ambient_view=self._ambient_view,
                            )

                            logger.info("Buddy says: %s", response)

                            # Display and speak the response
                            await self._overlay.say(response, "idle")
                            await self._tts.speak(response, wait=True, conversation=True)
                            await self._overlay.set_state("idle", 0)

                            # Log the conversation
                            await self._db.log_conversation("user", user_text)
                            await self._db.log_conversation("assistant", response)

                            # Mood boost from conversation
                            self._mood.process_event("conversation", {})

                            # Send response back to the client (so chat.sh can show it)
                            writer.write((json.dumps({"text": response}) + "\n").encode() if audio else (response.replace("\n", " ") + "\n").encode())
                            await writer.drain()
                        finally:
                            self._tts.conversing = False
                            self._active_turn = None

            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Chat handler error")
                writer.write(b"ERROR: Conversation failed; check brain.log.\n")
                await writer.drain()
                await self._overlay.set_state("idle", 0)
            finally:
                if self._voice_owner is writer:
                    self._voice_owner = None
                    self._voice_task = None
                    self._tts.listening = False
                    await self._overlay.set_state("idle", 0)
                clients.discard(asyncio.current_task())
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass

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
            await server.wait_closed()
            for task in clients:
                task.cancel()
            await asyncio.gather(*clients, return_exceptions=True)
            if sock_path.exists():
                sock_path.unlink()
