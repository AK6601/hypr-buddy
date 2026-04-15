"""
System Monitors
================

Monitors system state that doesn't come from Hyprland or D-Bus:
  - Battery level (via /sys/class/power_supply/)
  - Time-of-day changes (morning/afternoon/evening/night)
  - CPU usage (via /proc/stat)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from ipc.protocol import DesktopEvent, EventType
from daemon.sender import EventSender

logger = logging.getLogger("virtual-buddy.daemon.system")


def _read_battery() -> dict | None:
    """Read battery info from sysfs. Returns None if no battery found."""
    power_dir = Path("/sys/class/power_supply")
    if not power_dir.exists():
        return None

    for entry in power_dir.iterdir():
        type_file = entry / "type"
        if type_file.exists() and type_file.read_text().strip() == "Battery":
            capacity_file = entry / "capacity"
            status_file = entry / "status"
            if capacity_file.exists():
                try:
                    capacity = int(capacity_file.read_text().strip())
                    status = status_file.read_text().strip() if status_file.exists() else "Unknown"
                    return {"level": capacity, "status": status, "name": entry.name}
                except (ValueError, OSError):
                    continue
    return None


def _get_time_period() -> str:
    """Classify current time into a period."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"


def _read_cpu_stat() -> tuple[int, int] | None:
    """Read raw CPU counters from /proc/stat. Returns (idle, total) or None.

    To compute usage percentage, take two samples and calculate:
      usage = 1.0 - (idle2 - idle1) / (total2 - total1)
    """
    stat_path = Path("/proc/stat")
    if not stat_path.exists():
        return None
    try:
        with open(stat_path) as f:
            line = f.readline()
        parts = line.split()
        if parts[0] != "cpu":
            return None
        values = [int(x) for x in parts[1:]]
        idle = values[3]
        total = sum(values)
        return (idle, total)
    except (ValueError, IndexError, OSError):
        return None


class SystemMonitor:
    """Periodically checks system state and emits events on changes."""

    def __init__(self, sender: EventSender, config: dict) -> None:
        self._sender = sender
        self._watch_battery: bool = config.get("watch_battery", True)
        self._watch_time: bool = config.get("watch_time", True)
        self._low_battery_threshold: int = config.get("low_battery_threshold", 20)
        self._work_alert_minutes: int = config.get("work_session_alert_minutes", 90)

        self._last_time_period: str = ""
        self._last_battery_level: int = -1
        self._low_battery_alerted: bool = False

    async def run(self) -> None:
        """Main poll loop — checks system state every 30 seconds."""
        logger.info("System monitor started")

        while True:
            try:
                await self._check_time()
                await self._check_battery()
            except Exception as e:
                logger.error("System monitor error: %s", e)

            await asyncio.sleep(30)

    async def _check_time(self) -> None:
        """Emit a time event when the time period changes."""
        if not self._watch_time:
            return

        period = _get_time_period()
        if period != self._last_time_period:
            self._last_time_period = period
            await self._sender.send(DesktopEvent(
                type=EventType.TIME,
                data={
                    "period": period,
                    "hour": datetime.now().hour,
                    "minute": datetime.now().minute,
                },
            ))
            logger.info("Time period changed: %s", period)

    async def _check_battery(self) -> None:
        """Emit battery events on significant changes."""
        if not self._watch_battery:
            return

        info = _read_battery()
        if info is None:
            return

        level = info["level"]

        # Emit on significant change (every 10%) or crossing the low threshold
        sig_change = abs(level - self._last_battery_level) >= 10
        crossed_low = level <= self._low_battery_threshold and not self._low_battery_alerted

        if sig_change or crossed_low:
            self._last_battery_level = level
            is_low = level <= self._low_battery_threshold
            if is_low:
                self._low_battery_alerted = True
            elif level > self._low_battery_threshold + 5:
                self._low_battery_alerted = False

            await self._sender.send(DesktopEvent(
                type=EventType.BATTERY,
                data={
                    "level": level,
                    "status": info["status"],
                    "low": is_low,
                },
            ))
            logger.info("Battery: %d%% (%s)", level, info["status"])
