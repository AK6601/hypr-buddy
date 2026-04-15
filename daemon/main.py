"""
Hypr Buddy Daemon
====================

Async service that monitors the CachyOS/Hyprland desktop and forwards
normalized events to the brain component via Unix domain socket.

Monitors:
  - Hyprland IPC events (active window, workspaces, fullscreen, etc.)
  - D-Bus desktop notifications
  - System state (battery, time-of-day)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

# Append project root so we can import ipc.protocol
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipc.protocol import BRAIN_SOCKET

from daemon.hyprland import HyprlandMonitor
from daemon.notifications import NotificationMonitor
from daemon.system import SystemMonitor
from daemon.sender import EventSender

logger = logging.getLogger("hypr-buddy.daemon")


def load_config() -> dict:
    """Load events.toml config."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "events.toml"
    if not config_path.exists():
        logger.warning("Config not found at %s, using defaults", config_path)
        return {}
    with open(config_path, "rb") as f:
        return tomllib.load(f)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Hypr Buddy Daemon starting...")

    config = load_config()
    sender = EventSender(BRAIN_SOCKET)

    # Build list of monitor coroutines
    tasks: list[asyncio.Task] = []

    # Hyprland IPC monitor
    hypr_cfg = config.get("hyprland", {})
    ignore_cfg = config.get("ignore", {})
    hypr = HyprlandMonitor(sender, hypr_cfg, ignore_cfg)
    tasks.append(asyncio.create_task(hypr.run(), name="hyprland"))

    # D-Bus notification monitor
    notif_cfg = config.get("notifications", {})
    if notif_cfg.get("enabled", True):
        notif = NotificationMonitor(sender, notif_cfg)
        tasks.append(asyncio.create_task(notif.run(), name="notifications"))

    # System monitors
    sys_cfg = config.get("system", {})
    sysmon = SystemMonitor(sender, sys_cfg)
    tasks.append(asyncio.create_task(sysmon.run(), name="system"))

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _shutdown_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown_handler)

    logger.info("Daemon running. Monitors: %s", [t.get_name() for t in tasks])

    # Wait for shutdown
    await shutdown_event.wait()

    logger.info("Cancelling monitors...")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await sender.close()
    logger.info("Daemon stopped.")
