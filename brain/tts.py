"""
TTS Engine
===========

Text-to-speech via Piper TTS (local, fast, high-quality).
Manages a speech queue so utterances don't overlap.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger("virtual-buddy.brain.tts")


class TTSEngine:
    """Queued TTS using Piper as a subprocess."""

    def __init__(self, tts_config: dict) -> None:
        self._enabled: bool = tts_config.get("enabled", True)
        self._engine: str = tts_config.get("engine", "piper")
        self._model_path: str = tts_config.get(
            "piper_model",
            "~/.local/share/piper-voices/en_US-amy-medium.onnx",
        )
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._current_process: asyncio.subprocess.Process | None = None

    async def speak(self, text: str) -> None:
        """Queue text for speech. Non-blocking."""
        if not self._enabled:
            return

        # Start worker on first use
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())

        await self._queue.put(text)

    async def interrupt(self) -> None:
        """Stop current speech and clear the queue."""
        # Clear queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Kill current process
        if self._current_process and self._current_process.returncode is None:
            try:
                self._current_process.terminate()
            except ProcessLookupError:
                pass

    async def _worker(self) -> None:
        """Background worker that processes the speech queue."""
        while True:
            text = await self._queue.get()
            try:
                await self._speak_now(text)
            except Exception as e:
                logger.error("TTS error: %s", e)
            finally:
                self._queue.task_done()

    async def _speak_now(self, text: str) -> None:
        """Actually invoke Piper + aplay to speak the text.

        Uses a two-process pipeline without a shell to avoid injection:
        piper writes raw audio to stdout, aplay reads it from stdin.
        """
        model = Path(self._model_path).expanduser()

        if not model.exists():
            logger.warning("Piper model not found at %s", model)
            return

        piper_bin = shutil.which("piper")
        if not piper_bin:
            logger.warning("piper not found in PATH")
            return

        aplay_bin = shutil.which("aplay")
        if not aplay_bin:
            logger.warning("aplay not found in PATH")
            return

        # Launch piper: reads text from stdin, writes raw PCM to stdout.
        piper_proc = await asyncio.create_subprocess_exec(
            piper_bin, "--model", str(model), "--output_raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Launch aplay: reads raw PCM from stdin.
        aplay_proc = await asyncio.create_subprocess_exec(
            aplay_bin, "-r", "22050", "-f", "S16_LE", "-c", "1", "-q",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._current_process = aplay_proc

        # Feed text to piper, collect its raw audio output
        piper_stdout, piper_stderr = await piper_proc.communicate(
            input=text.encode("utf-8")
        )

        if piper_proc.returncode != 0:
            logger.debug("Piper error: %s", (piper_stderr or b"").decode(errors="replace"))
            aplay_proc.kill()
            self._current_process = None
            return

        # Feed raw audio to aplay
        _, aplay_stderr = await aplay_proc.communicate(input=piper_stdout)

        if aplay_proc.returncode != 0 and aplay_stderr:
            logger.debug("aplay error: %s", aplay_stderr.decode(errors="replace"))

        self._current_process = None
