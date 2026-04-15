"""
Mood System
============

Tracks the buddy's emotional state as a float from -1.0 (miserable) to 1.0 (ecstatic).
Events shift the mood up or down; over time it decays toward a baseline.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("virtual-buddy.brain.mood")

# How much each event type shifts mood
MOOD_DELTAS: dict[str, float] = {
    # Positive
    "window_focus:steam": 0.10,
    "window_focus:lutris": 0.10,
    "window_focus:spotify": 0.05,
    "window_focus:rhythmbox": 0.05,
    "window_focus:discord": 0.03,
    "window_open:steam": 0.08,
    # Negative
    "notification:urgency_2": -0.08,  # Critical urgency
    "notification:urgency_1": -0.02,  # Normal urgency
    "battery:low": -0.10,
    # Conversation (user talking to buddy)
    "conversation": 0.05,
    # Neutral / mild
    "workspace": 0.01,
    "window_focus": 0.0,  # Default for unknown apps
}

BASELINE = 0.2
DECAY_RATE = 0.005  # Per minute, how fast mood drifts toward baseline
DECAY_INTERVAL = 30  # seconds


class MoodSystem:
    """Tracks and updates the buddy's mood value."""

    def __init__(self, initial: float = 0.3) -> None:
        self._value = max(-1.0, min(1.0, initial))
        self._last_decay = time.monotonic()
        # Track continuous work time
        self._work_start: float | None = None
        self._work_apps = {"code", "codium", "neovim", "nvim", "vim", "emacs", "jetbrains"}

    @property
    def value(self) -> float:
        return self._value

    @property
    def label(self) -> str:
        """Human-readable mood label."""
        v = self._value
        if v >= 0.7:
            return "ecstatic"
        elif v >= 0.4:
            return "happy"
        elif v >= 0.1:
            return "content"
        elif v >= -0.1:
            return "neutral"
        elif v >= -0.4:
            return "down"
        elif v >= -0.7:
            return "sad"
        else:
            return "miserable"

    def _clamp(self) -> None:
        self._value = max(-1.0, min(1.0, self._value))

    def process_event(self, event_type: str, data: dict) -> None:
        """Adjust mood based on an incoming event."""
        app_class = data.get("app_class", "").lower()

        # Check specific app delta first, then generic event type
        specific_key = f"{event_type}:{app_class}"
        delta = MOOD_DELTAS.get(specific_key)

        if delta is None:
            # Check notification urgency
            if event_type == "notification":
                urgency = data.get("urgency", 1)
                delta = MOOD_DELTAS.get(f"notification:urgency_{urgency}", -0.02)
            elif event_type == "battery" and data.get("low"):
                delta = MOOD_DELTAS.get("battery:low", -0.10)
            else:
                delta = MOOD_DELTAS.get(event_type, 0.0)

        if delta != 0.0:
            self._value += delta
            self._clamp()
            logger.debug("Mood adjusted by %.2f -> %.2f (%s)", delta, self._value, self.label)

        # Track work sessions
        if event_type == "window_focus":
            if app_class in self._work_apps:
                if self._work_start is None:
                    self._work_start = time.monotonic()
            else:
                self._work_start = None

    @property
    def work_minutes(self) -> float:
        """How many minutes the user has been in a work app continuously."""
        if self._work_start is None:
            return 0.0
        return (time.monotonic() - self._work_start) / 60.0

    async def decay_loop(self) -> None:
        """Continuously decay mood toward baseline."""
        while True:
            await asyncio.sleep(DECAY_INTERVAL)
            elapsed_minutes = (time.monotonic() - self._last_decay) / 60.0
            self._last_decay = time.monotonic()

            if abs(self._value - BASELINE) < 0.01:
                continue

            decay_amount = DECAY_RATE * elapsed_minutes
            if self._value > BASELINE:
                self._value = max(BASELINE, self._value - decay_amount)
            else:
                self._value = min(BASELINE, self._value + decay_amount)
