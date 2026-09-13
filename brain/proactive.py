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

logger = logging.getLogger("hypr-buddy.brain.proactive")

# Proactive comments keyed by context
PROACTIVE_LINES: dict[str, list[str]] = {
    "long_work": [
        "Hey, you've been locked in for a while~ stretch break? I'll wait.",
        "Hydration check! 💧 grab some water, I'll guard your code.",
        "You've been grinding hard. Quick breather? Your brain'll thank you.",
        "Still going strong! Maybe blink a few times for me? 😄",
    ],
    "idle_check": [
        "Whatcha thinking about? 👀",
        "I'm here if you need me~ just say the word.",
        "Standing by! Poke me anytime.",
        "All quiet~ want me to do anything?",
    ],
    "encouragement": [
        "Ooh nice, you're on a roll! Keep going~",
        "Look at you being all productive. So cool. ✨",
        "This is coming together really nicely — proud of you!",
    ],
    "late_night": [
        "It's late late~ wanna call it and pick it up tomorrow? Your code'll still be here, promise.",
        "Burning the midnight oil, huh. I'll stay up with you. 🌙",
        "Psst, future-you would really love a good night's sleep~",
    ],
    "morning_energy": [
        "Morning! I already feel like today's a good one~",
        "Fresh start! What's first on the list?",
        "New day, clean slate — let's make something cool. ☀",
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
        self._startup_delay: float = behavior_config.get("proactive_startup_delay", 60)
        self._mood = mood
        self._overlay = overlay
        self._tts = tts
        self._personality = personality
        self._is_fullscreen = False

    def reconfigure(self, behavior_config: dict) -> None:
        """Re-apply tunable proactive parameters in place."""
        cfg = behavior_config or {}
        self._interval_min = cfg.get("proactive_interval_min", self._interval_min)
        self._interval_max = cfg.get("proactive_interval_max", self._interval_max)
        self._during_fullscreen = cfg.get("proactive_during_fullscreen", self._during_fullscreen)
        self._startup_delay = cfg.get("proactive_startup_delay", self._startup_delay)

    def set_fullscreen(self, fs: bool) -> None:
        self._is_fullscreen = fs

    async def run(self) -> None:
        """Main proactive loop — fires on a randomized interval."""
        # Wait a bit after startup before first proactive comment
        await asyncio.sleep(self._startup_delay)

        while True:
            interval = random.randint(self._interval_min, self._interval_max) * 60
            await asyncio.sleep(interval)

            if self._is_fullscreen and not self._during_fullscreen:
                logger.debug("Skipping proactive (fullscreen)")
                continue

            await self._do_proactive()

    async def _do_proactive(self) -> None:
        """Pick and deliver a contextual proactive comment."""
        if self._tts.listening or self._tts.conversing:
            return
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
