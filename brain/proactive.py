"""
Proactive Behavior
===================

Timer-based system that makes the buddy occasionally comment on what
you're doing, suggest breaks, or just check in. Context-aware and
respectful of fullscreen mode.
"""

from __future__ import annotations

import asyncio
import logging
import random

from brain.mood import MoodSystem
from brain.overlay_client import OverlayClient
from brain.personality import Personality
from brain.reactions import ReactionEngine
from brain.tts import TTSEngine

logger = logging.getLogger("virtual-buddy.brain.proactive")

# Proactive comments keyed by context
PROACTIVE_LINES: dict[str, list[str]] = {
    "long_work": [
        "You've been at it for a while! Maybe stretch a bit?",
        "Don't forget to drink some water~",
        "A short break can do wonders for productivity!",
        "Your dedication is impressive, but rest is important too!",
    ],
    "idle_check": [
        "Whatcha thinking about?",
        "Everything okay over there?",
        "Need any help with anything?",
        "Just checking in~",
    ],
    "encouragement": [
        "You're doing great!",
        "Keep it up! I believe in you~",
        "Whatever you're working on, I'm sure it'll turn out awesome!",
    ],
    "late_night": [
        "It's really late... please consider going to sleep!",
        "The code will still be there tomorrow, I promise.",
        "Burning the midnight oil? At least grab a snack.",
    ],
    "morning_energy": [
        "The day is young! So many possibilities~",
        "Fresh start, fresh code!",
        "Let's make today productive!",
    ],
}


class ProactiveBehavior:
    """Periodically makes the buddy comment or suggest things."""

    def __init__(
        self,
        behavior_config: dict,
        mood: MoodSystem,
        overlay: OverlayClient,
        tts: TTSEngine,
        personality: Personality,
        reactions: ReactionEngine,
    ) -> None:
        self._interval_min: int = behavior_config.get("proactive_interval_min", 20)
        self._interval_max: int = behavior_config.get("proactive_interval_max", 40)
        self._during_fullscreen: bool = behavior_config.get("proactive_during_fullscreen", False)
        self._mood = mood
        self._overlay = overlay
        self._tts = tts
        self._personality = personality
        self._is_fullscreen = False

    def set_fullscreen(self, fs: bool) -> None:
        self._is_fullscreen = fs

    async def run(self) -> None:
        """Main proactive loop — fires on a randomized interval."""
        # Wait a bit after startup before first proactive comment
        await asyncio.sleep(60)

        while True:
            interval = random.randint(self._interval_min, self._interval_max) * 60
            await asyncio.sleep(interval)

            if self._is_fullscreen and not self._during_fullscreen:
                logger.debug("Skipping proactive (fullscreen)")
                continue

            await self._do_proactive()

    async def _do_proactive(self) -> None:
        """Pick and deliver a contextual proactive comment."""
        from datetime import datetime

        hour = datetime.now().hour
        work_mins = self._mood.work_minutes

        # Choose category based on context
        if work_mins >= 90:
            category = "long_work"
            state = "thinking"
        elif hour >= 23 or hour < 5:
            category = "late_night"
            state = "sleeping"
        elif 5 <= hour < 10:
            category = "morning_energy"
            state = "happy"
        elif self._mood.value >= 0.4:
            category = "encouragement"
            state = "happy"
        else:
            category = "idle_check"
            state = "idle"

        lines = PROACTIVE_LINES.get(category, PROACTIVE_LINES["idle_check"])
        text = random.choice(lines)
        text = self._personality.flavor_text(text, self._mood.value)

        await self._overlay.say(text, state)
        await self._tts.speak(text)
        logger.info("Proactive [%s]: %s", category, text)
