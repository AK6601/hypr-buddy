#!/usr/bin/env python3
"""Hotkey-toggle voice sessions: no terminal, textbox, or permanent microphone."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.chat import CHAT_SOCKET
from brain.audio_capture import capture_blocks

log = logging.getLogger("hypr-buddy.voice")
RATE, CHUNK = 16000, 320  # 20ms chunks


class EndpointDetector:
    """Adaptive energy gate, speech onset debounce, and trailing silence."""
    def __init__(self, threshold=350, silence_seconds=0.7):
        self.floor = threshold
        self.noise = threshold / 3
        self.silence_chunks = round(silence_seconds * RATE / CHUNK)
        self.started = False
        self.hot = self.quiet = self.total = 0

    def feed(self, rms: float) -> bool:
        self.total += 1
        voiced = rms > max(self.floor, self.noise * 3)
        if not self.started:
            self.hot = self.hot + 1 if voiced else 0
            if not voiced:
                self.noise = self.noise * .98 + rms * .02
            self.started = self.hot >= 3
        if self.started:
            self.quiet = 0 if voiced else self.quiet + 1
        return self.started and self.quiet >= self.silence_chunks


def record(stop: threading.Event, cfg: dict) -> str | None:
    import numpy as np

    detector = EndpointDetector(cfg.get("energy_threshold", 350), cfg.get("silence_seconds", .7))
    pre_roll = deque(maxlen=15)
    frames = []
    capture = capture_blocks(stop, cfg.get("pipewire_target"))
    try:
        idle_limit = float(cfg.get("session_idle_seconds", 20))
        for block in capture:
            if stop.is_set():
                return None
            samples = np.frombuffer(block, dtype=np.int16).astype(np.float32)
            finished = detector.feed(float(np.sqrt(np.mean(samples * samples))))
            if detector.started:
                if not frames:
                    frames.extend(pre_roll)
                frames.append(block)
            else:
                pre_roll.append(block)
                if detector.total * CHUNK / RATE >= idle_limit:
                    return None
            if finished or len(frames) * CHUNK / RATE >= 30:
                break
        if not detector.started or stop.is_set():
            return None
        directory = Path(CHAT_SOCKET).parent / "recordings"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".wav", dir=directory)
        os.close(fd)
        with wave.open(path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(b"".join(frames))
        return path
    finally:
        capture.close()


async def main(once=False):
    import fcntl
    import tomllib

    directory = Path(CHAT_SOCKET).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    control = directory / "voice.sock"
    # Toggle an existing session off without guessing PIDs.
    try:
        _, writer = await asyncio.open_unix_connection(str(control))
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    else:
        writer.write(b"stop\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return
    with (directory / "voice.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        control.unlink(missing_ok=True)
        stop = threading.Event()
        stop_async = asyncio.Event()
        def request_stop():
            stop.set()
            stop_async.set()
        async def stop_client(reader, writer):
            request_stop()
            writer.close()
            await writer.wait_closed()
        old_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(stop_client, path=str(control))
        finally:
            os.umask(old_umask)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop)
        with (Path(__file__).resolve().parent.parent / "config/buddy.toml").open("rb") as f:
            cfg = tomllib.load(f).get("speech", {})
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(CHAT_SOCKET)
            async def exchange(line):
                writer.write((line + "\n").encode())
                await writer.drain()
                result = await asyncio.wait_for(reader.readline(), 240)
                if not result:
                    raise ConnectionError("Brain disconnected")
                return result.decode().strip()
            async def conversation():
                while not stop.is_set():
                    reply = await exchange("/listen")
                    if reply.startswith("ERROR:"):
                        raise RuntimeError(reply)
                    path = await asyncio.to_thread(record, stop, cfg)
                    if not path:
                        return
                    try:
                        reply = await exchange("AUDIO:" + path)
                        log.info("Shiro: %s", reply)
                    finally:
                        Path(path).unlink(missing_ok=True)
                    if once:
                        return
                    # Let the speaker's acoustic tail fade before opening the mic.
                    await asyncio.sleep(.3)
            task = asyncio.create_task(conversation())
            stopped = asyncio.create_task(stop_async.wait())
            await asyncio.wait([task, stopped], return_when=asyncio.FIRST_COMPLETED)
            request_stop()
            if not task.done():
                # Recording checks stop every audio chunk. Keep its cleanup alive.
                try:
                    await asyncio.wait_for(asyncio.shield(task), 1)
                except asyncio.TimeoutError:
                    task.cancel()
            stopped.cancel()
            results = await asyncio.gather(task, stopped, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result
        finally:
            request_stop()
            if writer:
                writer.write(b"/stop\n")
                try:
                    await writer.drain()
                except ConnectionError:
                    pass
                writer.close()
                await writer.wait_closed()
            # A separate connection interrupts playback even if the turn is busy.
            try:
                _, closer = await asyncio.open_unix_connection(CHAT_SOCKET)
                closer.write(b"/stop\n")
                await closer.drain()
                closer.close()
                await closer.wait_closed()
            except OSError:
                pass
            server.close()
            await server.wait_closed()
            control.unlink(missing_ok=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Record one turn instead of a session")
    try:
        asyncio.run(main(parser.parse_args().once))
    except Exception as exc:
        log.error("Voice session failed: %s", exc)
        sys.exit(1)
