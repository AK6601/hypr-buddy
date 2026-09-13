"""
Screen Vision
=============

On-demand screen capture for Hyprland via `grim`. Captures the focused monitor
(or the full output as a fallback), downscales so the long edge is <= 1568px,
and returns PNG bytes. No temp files are written — capture goes through stdout.

If `grim` is missing or fails, capture functions return None and log a clear
warning so the rest of the system can degrade gracefully.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

logger = logging.getLogger("hypr-buddy.brain.vision")

MAX_EDGE = 1568


async def _run(*args: str) -> bytes | None:
    """Run a subprocess, returning stdout bytes or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        logger.warning("Command not found: %s (is it installed?)", args[0])
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to run %s: %s", args[0], e)
        return None

    if proc.returncode != 0:
        logger.warning(
            "%s exited %s: %s", args[0], proc.returncode,
            stderr.decode("utf-8", errors="replace").strip(),
        )
        return None
    return stdout


async def _focused_monitor() -> str | None:
    """Return the name of the focused Hyprland monitor, or None."""
    out = await _run("hyprctl", "-j", "monitors")
    if not out:
        return None
    try:
        monitors = json.loads(out.decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to parse hyprctl monitors: %s", e)
        return None
    for mon in monitors:
        if mon.get("focused"):
            return mon.get("name")
    return None


def _downscale_png(raw: bytes) -> bytes:
    """Downscale PNG bytes so the long edge is <= MAX_EDGE; re-encode to PNG.

    On any Pillow error, returns the original bytes unchanged.
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        longest = max(w, h)
        if longest > MAX_EDGE:
            scale = MAX_EDGE / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.warning("Image downscale failed (%s); using raw capture", e)
        return raw


async def capture_full() -> bytes | None:
    """Capture all outputs (fallback when no focused monitor is found)."""
    raw = await _run("grim", "-")
    if raw is None:
        logger.warning("Screen capture unavailable (grim failed or missing).")
        return None
    logger.info("Captured full screen (%d bytes raw).", len(raw))
    return _downscale_png(raw)


async def capture_screen(active_monitor_only: bool = True) -> bytes | None:
    """Capture the focused monitor as downscaled PNG bytes.

    Falls back to a full capture if no focused monitor can be resolved.
    Returns None if capture is unavailable.
    """
    name = await _focused_monitor() if active_monitor_only else None
    if name:
        raw = await _run("grim", "-o", name, "-")
        if raw is None:
            logger.warning("Screen capture unavailable for monitor %s.", name)
            return None
        logger.info("Captured monitor %s (%d bytes raw).", name, len(raw))
        return _downscale_png(raw)

    # No focused monitor (or full capture requested): fall back.
    return await capture_full()
