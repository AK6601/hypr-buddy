"""
Ambient Vision
==============

Optional, OFF-by-default background loop that periodically glances at the
active monitor and stashes a one-line description into the chat handler's
context (`ambient_view`) so Shiro has passive awareness of what's on screen.

Privacy:
  - Disabled by default; only runs when [vision.ambient].enabled is true.
  - Logs every capture so the user can see when a screenshot happened.
  - NEVER stores raw frames — only the short text description is kept.
  - Skips capture while a fullscreen window is active (if configured).
"""

from __future__ import annotations

import asyncio
import logging

from brain import vision

logger = logging.getLogger("hypr-buddy.brain.ambient_vision")


class AmbientVision:
    """Periodically captures the screen and stores a one-line description."""

    def __init__(
        self,
        vision_config: dict,
        vision_provider,  # LLMProvider used for the one-shot description
        chat_handler,     # ChatHandler — receives update_context("ambient_view", ...)
    ) -> None:
        ambient_cfg = vision_config.get("ambient", {})
        self._enabled: bool = bool(ambient_cfg.get("enabled", False))
        self._interval_sec: int = int(ambient_cfg.get("interval_sec", 300))
        self._only_active_monitor: bool = bool(ambient_cfg.get("only_active_monitor", True))
        self._skip_on_fullscreen: bool = bool(ambient_cfg.get("skip_on_fullscreen", True))
        self._vision_provider = vision_provider
        self._chat = chat_handler
        self._is_fullscreen = False

    def set_fullscreen(self, fs: bool) -> None:
        self._is_fullscreen = fs

    async def run(self) -> None:
        """Main ambient loop. Returns immediately if disabled."""
        if not self._enabled:
            logger.info("Ambient vision disabled (off by default).")
            return

        logger.info(
            "Ambient vision enabled — capturing every %ds (skip_on_fullscreen=%s).",
            self._interval_sec, self._skip_on_fullscreen,
        )
        # Give the system a moment to settle before the first capture.
        await asyncio.sleep(min(self._interval_sec, 60))

        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("Ambient vision tick failed: %s", e)
            await asyncio.sleep(self._interval_sec)

    async def _tick(self) -> None:
        if self._skip_on_fullscreen and self._is_fullscreen:
            logger.debug("Ambient vision: skipping (fullscreen).")
            return

        png = await vision.capture_screen(active_monitor_only=self._only_active_monitor)
        if not png:
            logger.debug("Ambient vision: no capture available this tick.")
            return

        logger.info("Ambient vision: captured screen, requesting description.")
        try:
            desc = await self._vision_provider.chat(
                system_prompt=(
                    "In ONE short line, describe what is currently on the "
                    "user's screen. Be concise and factual."
                ),
                history=[],
                tools_schema=[],
                execute_tool=_noop_tool,
                images=[png],
                max_tool_iters=1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Ambient vision: description failed: %s", e)
            return

        desc = (desc or "").strip()
        if not desc:
            return

        # Store ONLY the text description — never the raw frame.
        self._chat.update_context("ambient_view", {"desc": desc})
        logger.info("Ambient view updated: %s", desc)


async def _noop_tool(name: str, args: dict) -> str:
    """Tool executor for the vision-only path (no tools should be invoked)."""
    return "Tools are not available in vision-only mode."
