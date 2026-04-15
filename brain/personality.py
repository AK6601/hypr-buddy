"""
Personality System
===================

Loads the character definition from config and provides methods for
generating contextually-appropriate text.
"""

from __future__ import annotations

import random
from datetime import datetime


class Personality:
    """Manages the buddy's character traits and speech patterns."""

    def __init__(self, char_config: dict) -> None:
        self.name: str = char_config.get("name", "Miku")
        self.description: str = char_config.get(
            "personality",
            "Friendly, slightly nerdy, encouraging.",
        )
        self._greetings = {
            "morning": char_config.get("greeting_morning", "Good morning!"),
            "afternoon": char_config.get("greeting_afternoon", "Hey hey!"),
            "evening": char_config.get("greeting_evening", "Still at it?"),
            "night": char_config.get("greeting_night", "It's getting late..."),
        }

    def get_greeting(self) -> str:
        """Return an appropriate greeting based on the time of day."""
        hour = datetime.now().hour
        if 5 <= hour < 12:
            return self._greetings["morning"]
        elif 12 <= hour < 17:
            return self._greetings["afternoon"]
        elif 17 <= hour < 21:
            return self._greetings["evening"]
        else:
            return self._greetings["night"]

    def flavor_text(self, text: str, mood: float) -> str:
        """Optionally adjust text based on mood level.

        At extreme moods, add emotional flavor. For normal moods,
        return text as-is to keep things natural.
        """
        if mood >= 0.7:
            # Ecstatic — add enthusiasm
            suffixes = [" ✨", "! Yay!", " 💫", "~!"]
            return text.rstrip("!.") + random.choice(suffixes)
        elif mood <= -0.5:
            # Very down — subdued
            prefixes = ["*sigh* ", "Hmm... ", ""]
            return random.choice(prefixes) + text
        return text

    def build_system_prompt(
        self,
        mood_label: str,
        active_app: str | None = None,
        time_period: str | None = None,
        recent_events: list[str] | None = None,
    ) -> str:
        """Build a system prompt for the LLM including personality and context."""
        lines = [
            f"You are {self.name}, a virtual desktop companion.",
            f"Personality: {self.description}",
            f"Current mood: {mood_label}",
        ]

        if time_period:
            lines.append(f"Time of day: {time_period}")
        if active_app:
            lines.append(f"The user is currently using: {active_app}")
        if recent_events:
            lines.append("Recent desktop events: " + ", ".join(recent_events[-5:]))

        lines.extend([
            "",
            "Guidelines:",
            "- Keep responses short (1-3 sentences).",
            "- Be conversational and warm, matching your personality.",
            "- Reference the user's current activity when relevant.",
            "- Your mood should influence your tone but not overwhelm it.",
            "- Never break character or mention being an AI.",
        ])

        return "\n".join(lines)
