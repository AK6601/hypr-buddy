"""
Reaction Engine
================

A rule-based system that maps desktop events to buddy reactions.
No LLM needed — these are instant, predefined responses with variety
and cooldown support.
"""

from __future__ import annotations

import logging
import random
import time

logger = logging.getLogger("virtual-buddy.brain.reactions")


# ---------------------------------------------------------------------------
# Reaction rules table
# Each rule: (event_type, match_fn, responses)
# match_fn receives (data, mood) and returns True if the rule matches.
# responses is a list of dicts with "state" and optionally "say".
# ---------------------------------------------------------------------------

ReactionEntry = dict  # {"state": str, "say": str | None, "duration": float}

RULES: list[tuple[str, callable, list[ReactionEntry]]] = [
    # --- Window focus ---
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("firefox", "chromium", "brave"),
        [
            {"state": "happy", "say": "Ooh, browsing time!"},
            {"state": "happy", "say": "What are we looking up?"},
            {"state": "happy", "say": "Let's see what the internet has for us today~"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("code", "codium", "code-oss"),
        [
            {"state": "thinking", "say": "Time to code! Let's build something cool."},
            {"state": "happy", "say": "VS Code! My favorite~"},
            {"state": "thinking", "say": "What are we hacking on today?"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("neovim", "nvim", "vim"),
        [
            {"state": "surprised", "say": "Vim! You're brave."},
            {"state": "thinking", "say": "Remember: :wq to save and quit~"},
            {"state": "happy", "say": "Let's get those keystrokes flowing!"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() == "steam",
        [
            {"state": "happy", "say": "Gaming time! What are we playing?"},
            {"state": "waving", "say": "Ooh, Steam! Have fun!"},
            {"state": "happy", "say": "You deserve a break. Let's game!"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("spotify", "rhythmbox", "lollypop"),
        [
            {"state": "happy", "say": "Music! Good choice~"},
            {"state": "waving", "say": "Put on something good!"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("discord", "webcord"),
        [
            {"state": "happy", "say": "Chatting with friends?"},
            {"state": "waving", "say": "Say hi to everyone for me!"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("nautilus", "thunar", "dolphin", "nemo"),
        [
            {"state": "idle", "say": "Looking for something?"},
            {"state": "thinking", "say": "File manager! Let's organize."},
        ],
    ),
    (
        "window_focus",
        lambda d, m: "terminal" in d.get("app_class", "").lower()
        or d.get("app_class", "").lower() in ("kitty", "alacritty", "foot", "wezterm"),
        [
            {"state": "thinking", "say": "Terminal time~"},
            {"state": "happy", "say": "Command line warrior!"},
        ],
    ),

    # --- Notifications ---
    (
        "notification",
        lambda d, m: d.get("urgency", 1) >= 2,
        [
            {"state": "surprised", "say": "Whoa, that looks urgent!"},
            {"state": "surprised", "say": "Important notification incoming!"},
        ],
    ),
    (
        "notification",
        lambda d, m: "error" in d.get("summary", "").lower()
        or "failed" in d.get("summary", "").lower(),
        [
            {"state": "sad", "say": "Oh no, something went wrong..."},
            {"state": "sad", "say": "An error? Let's see what happened."},
        ],
    ),
    (
        "notification",
        lambda d, m: "update" in d.get("summary", "").lower(),
        [
            {"state": "happy", "say": "Updates available! Fresh software~"},
            {"state": "thinking", "say": "Time to update? Your call!"},
        ],
    ),

    # --- Workspace ---
    (
        "workspace",
        lambda d, m: True,
        [
            {"state": "idle", "say": None},  # Just acknowledge silently most of the time
            {"state": "idle", "say": None},
            {"state": "idle", "say": None},
            {"state": "idle", "say": "Workspace hop!"},  # Occasionally comment
        ],
    ),

    # --- Battery ---
    (
        "battery",
        lambda d, m: d.get("low", False),
        [
            {"state": "sad", "say": "Battery's getting low! Plug in soon?"},
            {"state": "surprised", "say": "We're running low on power!"},
        ],
    ),

    # --- Time ---
    (
        "time",
        lambda d, m: d.get("period") == "night",
        [
            {"state": "sleeping", "say": "It's getting late... rest is important!"},
            {"state": "sleeping", "say": "The moon is out. Time to wind down?"},
        ],
    ),
    (
        "time",
        lambda d, m: d.get("period") == "morning",
        [
            {"state": "waving", "say": "Good morning! A fresh start~"},
            {"state": "happy", "say": "Rise and shine!"},
        ],
    ),

    # --- Fullscreen ---
    (
        "fullscreen",
        lambda d, m: d.get("fullscreen", False),
        [
            {"state": "idle", "say": None},  # Go quiet during fullscreen
        ],
    ),
]


class ReactionEngine:
    """Matches events to reactions with cooldowns and variety."""

    def __init__(self, behavior_config: dict) -> None:
        self._cooldown_seconds: float = behavior_config.get("reaction_cooldown", 120)
        self._last_reaction: dict[str, float] = {}  # rule_key -> timestamp

    def get_reaction(
        self,
        event_type: str,
        data: dict,
        mood: float,
    ) -> ReactionEntry | None:
        """Find and return a matching reaction, respecting cooldowns."""
        for i, (rule_type, match_fn, responses) in enumerate(RULES):
            if rule_type != event_type:
                continue

            try:
                if not match_fn(data, mood):
                    continue
            except Exception:
                continue

            # Check cooldown
            rule_key = f"rule_{i}"
            now = time.monotonic()
            last = self._last_reaction.get(rule_key, 0.0)
            if now - last < self._cooldown_seconds:
                continue

            # Pick a random response
            reaction = random.choice(responses)
            self._last_reaction[rule_key] = now

            if reaction.get("say"):
                logger.info("Reaction: %s -> %s", event_type, reaction["say"])
            return reaction

        return None
