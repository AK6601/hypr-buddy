"""
D-Bus Notification Monitor
===========================

Monitors desktop notifications via the org.freedesktop.Notifications interface.
We become a monitor on the session bus and sniff Notify method calls, extracting
app_name, summary, body, and urgency.

Includes per-app debouncing to avoid flooding the brain with rapid-fire notifications.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ipc.protocol import DesktopEvent, EventType
from daemon.sender import EventSender

logger = logging.getLogger("hypr-buddy.daemon.notifications")


class NotificationMonitor:
    """Watches D-Bus for desktop notifications."""

    def __init__(self, sender: EventSender, config: dict) -> None:
        self._sender = sender
        self._debounce_seconds: float = config.get("debounce_seconds", 5.0)
        self._ignore_apps: set[str] = set(config.get("ignore_apps", []))
        self._last_seen: dict[str, float] = {}

    def _should_debounce(self, app_name: str) -> bool:
        """Return True if we should suppress this notification (too recent)."""
        now = time.monotonic()
        last = self._last_seen.get(app_name, 0.0)
        if now - last < self._debounce_seconds:
            return True
        self._last_seen[app_name] = now
        return False

    async def run(self) -> None:
        """Connect to the session bus and monitor notifications."""
        try:
            from dbus_next.aio import MessageBus
            from dbus_next import MessageType, BusType, Message
        except ImportError:
            logger.error("dbus-next not installed — notification monitoring disabled")
            # Stay alive (don't crash the task group) but do nothing
            while True:
                await asyncio.sleep(3600)
            return  # Unreachable, but makes the control flow explicit

        while True:
            try:
                bus = await MessageBus(bus_type=BusType.SESSION).connect()
                logger.info("Connected to session D-Bus for notification monitoring")

                # Subscribe to Notify method calls on the Notifications interface.
                # We use the BecomeMonitor interface if available, otherwise match rules.
                reply = await bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus.Monitoring",
                        member="BecomeMonitor",
                        signature="asu",
                        body=[
                            [
                                "type='method_call',"
                                "interface='org.freedesktop.Notifications',"
                                "member='Notify'"
                            ],
                            0,
                        ],
                    )
                )

                if reply.message_type == MessageType.ERROR:
                    logger.warning(
                        "BecomeMonitor failed (%s), falling back to AddMatch",
                        reply.error_name,
                    )
                    # Fallback: use AddMatch rule (less reliable but works)
                    await bus.call(
                        Message(
                            destination="org.freedesktop.DBus",
                            path="/org/freedesktop/DBus",
                            interface="org.freedesktop.DBus",
                            member="AddMatch",
                            signature="s",
                            body=[
                                "type='method_call',"
                                "interface='org.freedesktop.Notifications',"
                                "member='Notify'"
                             ],
                        )
                    )

                queue = asyncio.Queue()
                def message_handler(msg):
                    queue.put_nowait(msg)

                bus.add_message_handler(message_handler)

                # Process incoming messages
                while True:
                    msg = await queue.get()
                    if (
                        msg.member == "Notify"
                        and msg.interface == "org.freedesktop.Notifications"
                    ):
                        await self._handle_notify(msg)

            except Exception as e:
                logger.warning("D-Bus error: %s — retrying in 5s", e)
                await asyncio.sleep(5)

    async def _handle_notify(self, msg) -> None:  # type: ignore[no-untyped-def]
        """Extract notification fields and forward as an event."""
        try:
            body = msg.body
            if not body or len(body) < 5:
                return

            app_name: str = body[0] or "unknown"
            summary: str = body[3] or ""
            body_text: str = body[4] or ""

            # Extract urgency from hints dict (index 6) if available
            urgency = 1  # normal
            if len(body) > 6 and isinstance(body[6], dict):
                hints = body[6]
                if "urgency" in hints:
                    urg_variant = hints["urgency"]
                    urgency = int(urg_variant.value if hasattr(urg_variant, "value") else urg_variant)

            if app_name in self._ignore_apps:
                return

            if self._should_debounce(app_name):
                logger.debug("Debounced notification from %s", app_name)
                return

            await self._sender.send(DesktopEvent(
                type=EventType.NOTIFICATION,
                data={
                    "app_name": app_name,
                    "summary": summary,
                    "body": body_text[:200],  # Truncate long bodies
                    "urgency": urgency,
                },
            ))

            logger.info("Notification: [%s] %s", app_name, summary)

        except (IndexError, TypeError, ValueError) as e:
            logger.debug("Failed to parse notification: %s", e)
