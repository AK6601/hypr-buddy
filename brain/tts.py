"""Local speech with completion tracking and cancellable subprocesses."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger("hypr-buddy.brain.tts")


class TTSEngine:
    def __init__(self, tts_config: dict) -> None:
        self._enabled = tts_config.get("enabled", True)
        self._model_path = tts_config.get("piper_model", "~/.local/share/piper-voices/en_US-amy-medium.onnx")
        self._queue: asyncio.Queue[tuple[str, asyncio.Future]] = asyncio.Queue()
        self._worker_task = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._utterance: asyncio.Task | None = None
        self.listening = False
        self.conversing = False
        self.on_playback_start = None
        self.on_playback_end = None

    @property
    def busy(self) -> bool:
        return self._utterance is not None or not self._queue.empty()

    async def speak(self, text: str, *, wait: bool = False, conversation: bool = False) -> None:
        if not self._enabled or ((self.listening or self.conversing) and not conversation):
            return
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
        done = asyncio.get_running_loop().create_future()
        await self._queue.put((text, done))
        if wait:
            await done

    async def interrupt(self) -> None:
        while not self._queue.empty():
            _, done = self._queue.get_nowait()
            if not done.done():
                done.set_result(None)
            self._queue.task_done()
        if self._utterance is not None:
            self._utterance.cancel()
            await asyncio.gather(self._utterance, return_exceptions=True)

    async def close(self) -> None:
        await self.interrupt()
        if self._worker_task:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)

    async def _worker(self) -> None:
        while True:
            text, done = await self._queue.get()
            self._utterance = asyncio.create_task(self._speak_now(text))
            try:
                await self._utterance
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                logger.exception("Speech failed")
            finally:
                self._utterance = None
                if not done.done():
                    done.set_result(None)
                self._queue.task_done()

    async def _speak_now(self, text: str) -> None:
        model = Path(self._model_path).expanduser()
        piper = shutil.which("piper") or shutil.which("piper-tts")
        player = shutil.which("aplay")
        if not model.is_file() or not piper or not player:
            logger.warning("Speech unavailable: check Piper, aplay, and model %s", model)
            return
        with Path(str(model) + ".json").open() as f:
            rate = int(json.load(f)["audio"]["sample_rate"])
        # Speak prose, not formatting or fenced source code.
        text = re.sub(r"```.*?```", "I've put the code in the text reply.", text, flags=re.S)
        text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
        text = text.replace("*", "").replace("`", "").replace("~", "")
        try:
            synth = await asyncio.create_subprocess_exec(
                piper, "--model", str(model), "--output_raw",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            self._processes.add(synth)
            audio, _ = await asyncio.wait_for(synth.communicate(text.encode()), 60)
            if synth.returncode:
                raise RuntimeError(f"Piper exited {synth.returncode}")
            playback = await asyncio.create_subprocess_exec(
                player, "-r", str(rate), "-f", "S16_LE", "-c", "1", "-q",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            self._processes.add(playback)
            if self.on_playback_start:
                await self.on_playback_start()
            await asyncio.wait_for(playback.communicate(audio), 120)
        finally:
            for proc in self._processes:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await proc.wait()
            self._processes.clear()
            if self.on_playback_end:
                await self.on_playback_end()
