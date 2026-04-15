"""
Overlay Client
===============

Sends commands to the overlay renderer via Unix domain socket.
Handles connection failures gracefully (overlay may not be running yet).
"""

from __future__ import annotations

import asyncio
import json
import logging

from ipc.protocol import OverlayCommand, set_state, say, hide, show, quit_overlay

logger = logging.getLogger("hypr-buddy.brain.overlay")


class OverlayClient:
    """Sends commands to the overlay via Unix socket."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> bool:
        try:
            _, writer = await asyncio.open_unix_connection(self._socket_path)
            self._writer = writer
            logger.info("Connected to overlay at %s", self._socket_path)
            return True
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            logger.debug("Cannot connect to overlay: %s", e)
            return False

    async def _send(self, cmd: OverlayCommand) -> bool:
        async with self._lock:
            if self._writer is None:
                if not await self._connect():
                    return False
            try:
                if self._writer is None:
                    return False
                self._writer.write((cmd.to_json() + "\n").encode())
                await self._writer.drain()
                return True
            except (ConnectionResetError, BrokenPipeError, OSError):
                logger.warning("Lost connection to overlay")
                self._writer = None
                return False

    async def set_state(self, state: str, duration: float = 0.0) -> bool:
        return await self._send(set_state(state, duration))

    async def say(self, text: str, state: str = "talking") -> bool:
        return await self._send(say(text, state))

    async def hide(self) -> bool:
        return await self._send(hide())

    async def show(self) -> bool:
        return await self._send(show())

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
