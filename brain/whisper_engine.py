"""
Whisper Engine
================

Provides persistent Whisper transcription capability.
The model loads lazily on CPU and stays resident for subsequent voice turns.
"""

from __future__ import annotations

import asyncio
import logging
import concurrent.futures
from pathlib import Path


logger = logging.getLogger("hypr-buddy.brain.whisper")


class WhisperEngine:
    """Persistent Whisper model for fast transcription."""

    def __init__(self, model_name: str = "small", language: str | None = None, cpu_threads: int = 4) -> None:
        self._model_name = model_name
        self._language = language
        self._cpu_threads = max(1, int(cpu_threads))
        self._model = None
        self._device = "cpu"  # Keep GPU memory available for the conversational LLM.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Load the model off the event loop."""
        if self._model is not None:
            return

        logger.info("Loading Whisper model '%s' on %s...", self._model_name, self._device)
        # Run in thread to not block event loop during long load
        loop = asyncio.get_running_loop()
        def load_model():
            import whisper
            import torch
            torch.set_num_threads(self._cpu_threads)
            return whisper.load_model(self._model_name, device=self._device)

        self._model = await loop.run_in_executor(self._executor, load_model)
        logger.info("Whisper model loaded and ready.")

    async def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe an audio file. Thread-safe and non-blocking for the event loop."""
        async with self._lock:
            if self._model is None:
                await self.load()
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor,
                lambda: self._model.transcribe(
                    str(audio_path), fp16=False, language=self._language,
                    temperature=0.0, condition_on_previous_text=False,
                )
            )
            return result.get("text", "").strip()

    def shutdown(self) -> None:
        """Clean up resources."""
        self._executor.shutdown()
