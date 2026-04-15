"""
Hyprland IPC Monitor
====================

Connects to the Hyprland event socket (socket2) and parses desktop events
such as active window changes, workspace switches, and fullscreen toggles.

Hyprland exposes two Unix sockets:
  - .socket.sock  : for sending commands (hyprctl)
  - .socket2.sock : for receiving events (event stream)

Events arrive as lines in the format: EVENT>>DATA
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ipc.protocol import DesktopEvent, EventType
from daemon.sender import EventSender

logger = logging.getLogger("virtual-buddy.daemon.hyprland")


def _get_socket2_path() -> Path | None:
    """Determine the path to Hyprland's event socket."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    instance_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not instance_sig:
        logger.warning("HYPRLAND_INSTANCE_SIGNATURE not set — Hyprland may not be running")
        return None
    # Validate: instance signature should be alphanumeric/underscores only
    # (prevents path traversal via crafted env vars)
    if not all(c.isalnum() or c in ("_", "-") for c in instance_sig):
        logger.error("Invalid HYPRLAND_INSTANCE_SIGNATURE: contains suspicious characters")
        return None
    sock = (Path(runtime_dir) / "hypr" / instance_sig / ".socket2.sock").resolve()
    # Ensure resolved path is still under runtime_dir
    if not str(sock).startswith(runtime_dir):
        logger.error("Socket path resolved outside runtime dir — possible path traversal")
        return None
    return sock


class HyprlandMonitor:
    """Monitors Hyprland IPC events and forwards them to the brain."""

    def __init__(
        self,
        sender: EventSender,
        hypr_config: dict,
        ignore_config: dict,
    ) -> None:
        self._sender = sender
        self._watch = {
            "activewindow": hypr_config.get("watch_activewindow", True),
            "openwindow": hypr_config.get("watch_openwindow", True),
            "closewindow": hypr_config.get("watch_closewindow", True),
            "workspace": hypr_config.get("watch_workspace", True),
            "fullscreen": hypr_config.get("watch_fullscreen", True),
        }
        self._ignore_classes: set[str] = set(ignore_config.get("window_classes", []))

    async def run(self) -> None:
        """Main loop: connect to Hyprland socket and process events."""
        sock_path = _get_socket2_path()
        if sock_path is None or not sock_path.exists():
            logger.error("Hyprland socket not found. Hyprland monitor will not run.")
            # Stay alive (don't crash the task group) but do nothing
            while True:
                await asyncio.sleep(3600)
            return

        logger.info("Connecting to Hyprland at %s", sock_path)

        while True:
            try:
                reader, _ = await asyncio.open_unix_connection(str(sock_path))
                logger.info("Connected to Hyprland event stream")

                while True:
                    line = await reader.readline()
                    if not line:
                        logger.warning("Hyprland socket closed, reconnecting...")
                        break
                    await self._handle_line(line.decode("utf-8", errors="replace").strip())

            except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
                logger.warning("Hyprland connection error: %s — retrying in 5s", e)

            await asyncio.sleep(5)

    async def _handle_line(self, line: str) -> None:
        """Parse a single Hyprland event line and forward if relevant."""
        if ">>" not in line:
            return

        event_name, _, data = line.partition(">>")

        if event_name == "activewindow" and self._watch.get("activewindow"):
            # Format: activewindow>>CLASS,TITLE
            parts = data.split(",", 1)
            app_class = parts[0] if parts else ""
            title = parts[1] if len(parts) > 1 else ""

            if app_class in self._ignore_classes:
                return

            await self._sender.send(DesktopEvent(
                type=EventType.WINDOW_FOCUS,
                data={"app_class": app_class, "title": title},
            ))

        elif event_name == "openwindow" and self._watch.get("openwindow"):
            # Format: openwindow>>ADDR,WORKSPACE,CLASS,TITLE
            parts = data.split(",", 3)
            app_class = parts[2] if len(parts) > 2 else ""
            title = parts[3] if len(parts) > 3 else ""
            workspace = parts[1] if len(parts) > 1 else ""

            if app_class in self._ignore_classes:
                return

            await self._sender.send(DesktopEvent(
                type=EventType.WINDOW_OPEN,
                data={
                    "app_class": app_class,
                    "title": title,
                    "workspace": workspace,
                },
            ))

        elif event_name == "closewindow" and self._watch.get("closewindow"):
            # Format: closewindow>>ADDR
            await self._sender.send(DesktopEvent(
                type=EventType.WINDOW_CLOSE,
                data={"address": data},
            ))

        elif event_name == "workspace" and self._watch.get("workspace"):
            # Format: workspace>>NAME
            await self._sender.send(DesktopEvent(
                type=EventType.WORKSPACE,
                data={"workspace": data},
            ))

        elif event_name == "fullscreen" and self._watch.get("fullscreen"):
            # Format: fullscreen>>0 or fullscreen>>1
            is_fullscreen = data.strip() == "1"
            await self._sender.send(DesktopEvent(
                type=EventType.FULLSCREEN,
                data={"fullscreen": is_fullscreen},
            ))
