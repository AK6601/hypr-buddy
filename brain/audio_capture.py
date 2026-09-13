"""PipeWire capture without PortAudio's ALSA/JACK device enumeration."""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time


def capture_command(target: str | None = None) -> list[str]:
    binary = "/usr/bin/pw-record" if Path("/usr/bin/pw-record").is_file() else shutil.which("pw-record")
    if not binary:
        raise RuntimeError("pw-record is missing. Install pipewire-audio and restart the voice session.")
    command = [binary, "--raw", "--format", "s16", "--rate", "16000", "--channels", "1", "--latency", "20ms"]
    if target:
        command += ["--target", str(target)]
    return command + ["-"]


def capture_blocks(stop, target=None, *, command=None, stall_timeout=5.0):
    """Yield complete 20ms mono PCM blocks; cancel even when a device stalls.

    stderr is retained for actionable failures rather than globally discarded.
    System PipeWire runs outside Conda's dynamic-library search path.
    """
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command or capture_command(target), stdout=subprocess.PIPE,
                                stderr=errors, stdin=subprocess.DEVNULL, env=env)
        pending = bytearray()
        last_data = time.monotonic()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while not stop.is_set():
                    if not selector.select(.1):
                        if time.monotonic() - last_data >= stall_timeout:
                            raise RuntimeError("Microphone produced no audio. Check the default input in wpctl status or set speech.pipewire_target.")
                        continue
                    block = os.read(proc.stdout.fileno(), 8192)
                    if not block:
                        errors.seek(0)
                        detail = errors.read(4096).decode(errors="replace").strip()
                        raise RuntimeError("PipeWire recording stopped: " + (detail or "check that PipeWire is running and an input is connected"))
                    last_data = time.monotonic()
                    pending.extend(block)
                    while len(pending) >= 640 and not stop.is_set():
                        yield bytes(pending[:640])
                        del pending[:640]
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
