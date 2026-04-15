"""
Event Sender
=============

Manages a persistent connection to the brain's Unix domain socket and sends
serialized DesktopEvent messages. Handles reconnection on failure.
"""

from __future__ import annotations

import asyncio
import logging

from ipc.protocol import DesktopEvent

logger = logging.getLogger("hypr-buddy.daemon.sender")


class EventSender:
    """Sends events to the brain via Unix domain socket with auto-reconnect."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._connected = False

    async def _connect(self) -> bool:
        """Attempt to connect to the brain socket."""
        try:
            _, writer = await asyncio.open_unix_connection(self._socket_path)
            self._writer = writer
            self._connected = True
            logger.info("Connected to brain at %s", self._socket_path)
            return True
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            logger.debug("Cannot connect to brain: %s", e)
            self._connected = False
            return False

    async def send(self, event: DesktopEvent) -> bool:
        """Send an event to the brain. Returns True if sent successfully."""
        async with self._lock:
            # Attempt to connect if not already connected
            if not self._connected:
                if not await self._connect():
                    return False

            try:
                if self._writer is None:
                    return False
                line = event.to_json() + "\n"
                self._writer.write(line.encode())
                await self._writer.drain()
                return True
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                logger.warning("Lost connection to brain: %s", e)
                self._connected = False
                self._writer = None
                return False

    async def close(self) -> None:
        """Close the connection."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._connected = False
