"""
System Monitors
================

Monitors system state that doesn't come from Hyprland or D-Bus:
  - Battery level (via /sys/class/power_supply/)
  - Time-of-day changes (morning/afternoon/evening/night)
  - CPU usage (via /proc/stat)
  - RAM usage (via /proc/meminfo)
  - GPU usage (via nvidia-smi if available, or sysfs)
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ipc.protocol import DesktopEvent, EventType
from daemon.sender import EventSender

logger = logging.getLogger("hypr-buddy.daemon.system")


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
    """Read raw CPU counters from /proc/stat. Returns (idle, total) or None."""
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


def _read_mem_info() -> dict[str, int]:
    """Read RAM usage from /proc/meminfo. Returns {total, available, used_pct}."""
    mem_path = Path("/proc/meminfo")
    res = {"total": 0, "available": 0, "used_pct": 0}
    if not mem_path.exists():
        return res
    try:
        content = mem_path.read_text()
        lines = content.splitlines()
        for line in lines:
            if line.startswith("MemTotal:"):
                res["total"] = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                res["available"] = int(line.split()[1])
        
        if res["total"] > 0:
            used = res["total"] - res["available"]
            res["used_pct"] = int((used / res["total"]) * 100)
    except (ValueError, IndexError, OSError):
        pass
    return res


def _read_gpu_usage() -> int | None:
    """Try to read GPU usage percentage."""
    # Try nvidia-smi first
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            encoding="utf-8",
            stderr=subprocess.DEVNULL
        )
        return int(out.strip())
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        pass

    # Fallback to sysfs (AMD/Intel)
    gpu_busy = Path("/sys/class/drm/card0/device/gpu_busy_percent")
    if gpu_busy.exists():
        try:
            return int(gpu_busy.read_text().strip())
        except (ValueError, OSError):
            pass
    
    return None


class SystemMonitor:
    """Periodically checks system state and emits events on changes."""

    def __init__(self, sender: EventSender, config: dict) -> None:
        self._sender = sender
        self._watch_battery: bool = config.get("watch_battery", True)
        self._watch_time: bool = config.get("watch_time", True)
        self._watch_stats: bool = config.get("watch_stats", True)
        self._low_battery_threshold: int = config.get("low_battery_threshold", 20)
        self._poll_interval: float = config.get("poll_interval_seconds", 30)

        self._last_time_period: str = ""
        self._last_battery_level: int = -1
        self._low_battery_alerted: bool = False

        self._last_cpu_idle: int = 0
        self._last_cpu_total: int = 0

    async def run(self) -> None:
        """Main poll loop — checks system state on a configurable interval."""
        logger.info("System monitor started (poll interval: %ss)", self._poll_interval)

        # Initial CPU reading
        stat = _read_cpu_stat()
        if stat:
            self._last_cpu_idle, self._last_cpu_total = stat

        while True:
            try:
                await self._check_time()
                await self._check_battery()
                if self._watch_stats:
                    await self._check_stats()
            except Exception as e:
                logger.error("System monitor error: %s", e)

            await asyncio.sleep(self._poll_interval)

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

    async def _check_stats(self) -> None:
        """Collect and send CPU, RAM, and GPU stats."""
        stats = {}
        
        # CPU
        stat = _read_cpu_stat()
        if stat:
            idle, total = stat
            diff_idle = idle - self._last_cpu_idle
            diff_total = total - self._last_cpu_total
            if diff_total > 0:
                cpu_usage = int((1.0 - (diff_idle / diff_total)) * 100)
                stats["cpu"] = cpu_usage
            self._last_cpu_idle, self._last_cpu_total = idle, total

        # RAM
        mem = _read_mem_info()
        if mem["total"] > 0:
            stats["ram"] = mem["used_pct"]

        # GPU
        gpu = _read_gpu_usage()
        if gpu is not None:
            stats["gpu"] = gpu

        if stats:
            await self._sender.send(DesktopEvent(
                type=EventType.SYSTEM_STATS,
                data=stats
            ))
            logger.debug("System stats: %s", stats)
