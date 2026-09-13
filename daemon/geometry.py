"""
Geometry & Cursor Poller
========================

Hyprland's event socket (socket2) only emits discrete events — it has no
continuous geometry stream, so the buddy can't follow live window drags,
resizes, or tiling animations, and it never learns where the cursor is.

This monitor closes both gaps by polling on a short interval:
  - `hyprctl activewindow -j` → emit WINDOW_MOVE when the focused window's
    x/y/w/h changes since the last tick (drags, tiling, resizes).
  - `hyprctl cursorpos -j` → emit CURSOR_MOVE when the cursor moves beyond a
    threshold (lets the brain dodge the mouse).
  - `hyprctl monitors -j` (cached/refreshed occasionally) → resolve the
    monitor the focused window lives on and attach its bounds to the
    geometry payload so the brain can clamp the buddy on-screen.

This AUGMENTS daemon/hyprland.py — it does not replace the WINDOW_FOCUS
emission (which carries app_class for reactions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from ipc.protocol import DesktopEvent, EventType
from daemon.sender import EventSender

logger = logging.getLogger("hypr-buddy.daemon.geometry")


async def _hyprctl_json(*args: str) -> dict | list | None:
    """Run `hyprctl <args> -j` and parse the JSON output. Returns None on error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", *args, "-j",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            return None
        return json.loads(stdout.decode())
    except Exception as e:
        logger.debug("hyprctl %s failed: %s", " ".join(args), e)
        return None


def logical_monitor(m: dict) -> dict:
    """Hyprland window/cursor coordinates are logical, including rotated outputs."""
    scale = max(float(m.get("scale", 1)), 0.25)
    width, height = int(m["width"]), int(m["height"])
    if int(m.get("transform", 0)) % 2:
        width, height = height, width
    left, top, right, bottom = m.get("reserved", [0, 0, 0, 0])
    return {
        "id": m.get("id"), "x": int(m["x"]) + int(left),
        "y": int(m["y"]) + int(top),
        "w": max(1, round(width / scale) - int(left) - int(right)),
        "h": max(1, round(height / scale) - int(top) - int(bottom)),
    }


class GeometryMonitor:
    """Polls active-window geometry and cursor position on a short interval."""

    def __init__(self, sender: EventSender, config: dict) -> None:
        self._sender = sender
        self._poll_interval: float = float(config.get("poll_interval", 0.2))
        self._cursor_poll: bool = config.get("cursor_poll", True)
        self._cursor_threshold: float = float(config.get("cursor_threshold", 20))
        # How often to refresh the cached monitor layout (seconds).
        self._monitor_refresh: float = float(config.get("monitor_refresh", 5.0))

        # Last-seen state used to detect changes between ticks.
        self._last_geo: dict | None = None
        self._last_cursor: tuple[int, int] | None = None

        # Cached monitor layout: list of {"id","x","y","w","h"}.
        self._monitors: list[dict] = []
        self._focused_monitor_id = None
        self._monitors_fetched_at: float = 0.0

    async def run(self) -> None:
        """Main poll loop — emits geometry/cursor events on change."""
        logger.info(
            "Geometry monitor started (interval=%.2fs, cursor_poll=%s, threshold=%dpx)",
            self._poll_interval, self._cursor_poll, int(self._cursor_threshold),
        )

        while True:
            try:
                await self._refresh_monitors()
                await self._check_geometry()
                if self._cursor_poll:
                    await self._check_cursor()
            except Exception as e:
                # Resilient: skip a tick on any error, keep polling.
                logger.debug("Geometry poll tick error: %s", e)

            await asyncio.sleep(self._poll_interval)

    async def _refresh_monitors(self) -> None:
        """Refresh the cached monitor layout occasionally."""
        now = time.monotonic()
        if self._monitors and (now - self._monitors_fetched_at) < self._monitor_refresh:
            return

        data = await _hyprctl_json("monitors")
        if not isinstance(data, list):
            return

        self._focused_monitor_id = next((m.get("id") for m in data if m.get("focused")), None)
        monitors: list[dict] = []
        for m in data:
            try:
                monitors.append(logical_monitor(m))
            except (KeyError, TypeError, ValueError):
                continue
        if monitors:
            self._monitors = monitors
            self._monitors_fetched_at = now

    def _monitor_for(self, mon_id: object) -> dict | None:
        """Return the cached monitor bounds for the given monitor id."""
        for m in self._monitors:
            if m["id"] == mon_id:
                return {"x": m["x"], "y": m["y"], "w": m["w"], "h": m["h"]}
        return None

    async def _check_geometry(self) -> None:
        """Emit WINDOW_MOVE when the focused window's geometry changed."""
        data = await _hyprctl_json("activewindow")
        if not isinstance(data, dict) or "at" not in data or "size" not in data:
            # No focused window (e.g. empty workspace) — forget last geo so a
            # re-focus on an identical window still emits.
            monitor = self._monitor_for(self._focused_monitor_id)
            if monitor is None and self._monitors:
                monitor = self._monitor_for(self._monitors[0]["id"])
            if monitor:
                geo = {"address": None, **monitor, "monitor": monitor}
                if self._geo_changed(geo):
                    self._last_geo = geo
                    await self._sender.send(DesktopEvent(type=EventType.WINDOW_MOVE, data={"geometry": geo}))
            return

        try:
            geo = {
                "address": data.get("address"),
                "x": int(data["at"][0]),
                "y": int(data["at"][1]),
                "w": int(data["size"][0]),
                "h": int(data["size"][1]),
            }
        except (KeyError, IndexError, TypeError, ValueError):
            return

        # Attach the bounds of the monitor the window lives on (if known).
        monitor = self._monitor_for(data.get("monitor"))
        if monitor is not None:
            geo["monitor"] = monitor

        if self._geo_changed(geo):
            self._last_geo = geo
            await self._sender.send(DesktopEvent(
                type=EventType.WINDOW_MOVE,
                data={"geometry": geo},
            ))
            logger.debug("Window geometry changed: %s", geo)

    def _geo_changed(self, geo: dict) -> bool:
        """True if x/y/w/h (or the focused address) differs from last tick."""
        prev = self._last_geo
        if prev is None:
            return True
        return (
            geo["address"] != prev.get("address")
            or geo["x"] != prev["x"]
            or geo["y"] != prev["y"]
            or geo["w"] != prev["w"]
            or geo["h"] != prev["h"]
            or geo.get("monitor") != prev.get("monitor")
        )

    async def _check_cursor(self) -> None:
        """Emit CURSOR_MOVE when the cursor moved past the threshold."""
        data = await _hyprctl_json("cursorpos")
        if not isinstance(data, dict) or "x" not in data or "y" not in data:
            return

        try:
            x, y = int(data["x"]), int(data["y"])
        except (TypeError, ValueError):
            return

        if self._last_cursor is not None:
            lx, ly = self._last_cursor
            dist = ((x - lx) ** 2 + (y - ly) ** 2) ** 0.5
            if dist < self._cursor_threshold:
                return

        self._last_cursor = (x, y)
        await self._sender.send(DesktopEvent(
            type=EventType.CURSOR_MOVE,
            data={"x": x, "y": y},
        ))
