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

logger = logging.getLogger("hypr-buddy.brain.reactions")


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
            {"state": "happy", "say": "Oooh, browser time~ researching something fun?"},
            {"state": "happy", "say": "Off into the internet we go! Want me to look something up?"},
            {"state": "thinking", "say": "The web's open — say the word and I'll dig something up."},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("code", "codium", "code-oss"),
        [
            {"state": "thinking", "say": "Code mode, let's gooo. What're we building?"},
            {"state": "happy", "say": "Ooh the editor! I love watching you cook. 👀"},
            {"state": "thinking", "say": "Alright, let's squash some bugs together~"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("neovim", "nvim", "vim"),
        [
            {"state": "surprised", "say": "Nvim?? Okay okay, fancy. Respect. ✨"},
            {"state": "happy", "say": "Terminal-wizard hours. I'm into it."},
            {"state": "thinking", "say": "Modal editing, huh — show me those hjkl moves."},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() == "steam",
        [
            {"state": "happy", "say": "Game time detected! You earned it~"},
            {"state": "waving", "say": "Have fun! I'll keep the system happy for you."},
            {"state": "happy", "say": "Ooh, what are we playing? 🎮"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("spotify", "rhythmbox", "lollypop"),
        [
            {"state": "happy", "say": "Tunes! Want me to vibe-check the volume?"},
            {"state": "waving", "say": "Music on~ what's the soundtrack today?"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("discord", "webcord"),
        [
            {"state": "happy", "say": "Social mode~ say hi to everyone for me!"},
            {"state": "waving", "say": "Chatting with the humans? Tell 'em I said hi. 👋"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: d.get("app_class", "").lower() in ("nautilus", "thunar", "dolphin", "nemo"),
        [
            {"state": "idle", "say": "Hunting for a file? I can help you find it~"},
            {"state": "thinking", "say": "Poking through folders, are we?"},
        ],
    ),
    (
        "window_focus",
        lambda d, m: "terminal" in d.get("app_class", "").lower()
        or d.get("app_class", "").lower() in ("kitty", "alacritty", "foot", "wezterm"),
        [
            {"state": "thinking", "say": "Ooh, the terminal. Careful with that rm -rf, yeah? 😅"},
            {"state": "happy", "say": "Raw shell power~ I love it here."},
        ],
    ),

    # --- Notifications ---
    (
        "notification",
        lambda d, m: d.get("urgency", 1) >= 2,
        [
            {"state": "surprised", "say": "Heads up! Something kinda urgent just popped up."},
            {"state": "surprised", "say": "Eyes up~ that one looked important!"},
        ],
    ),
    (
        "notification",
        lambda d, m: "error" in d.get("summary", "").lower()
        or "failed" in d.get("summary", "").lower(),
        [
            {"state": "sad", "say": "Aw, something broke. Want me to take a look?"},
            {"state": "sad", "say": "Uh oh, an error... we'll figure it out together."},
        ],
    ),
    (
        "notification",
        lambda d, m: "update" in d.get("summary", "").lower(),
        [
            {"state": "happy", "say": "Updates are ready~ keeping you shiny and patched!"},
            {"state": "thinking", "say": "New updates waiting whenever you want them."},
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
            {"state": "idle", "say": "Whoosh~ new workspace!"},  # Occasionally comment
        ],
    ),

    # --- Battery ---
    (
        "battery",
        lambda d, m: d.get("low", False),
        [
            {"state": "sad", "say": "Pssst, battery's getting low~ feed me electrons (and yourself)."},
            {"state": "surprised", "say": "Low power! Better find a charger soon. 🔋"},
        ],
    ),

    # --- Time ---
    (
        "time",
        lambda d, m: d.get("period") == "night",
        [
            {"state": "sleeping", "say": "It's super late... I'm getting sleepy too. 🌙"},
            {"state": "sleeping", "say": "Yaaawn~ maybe we call it soon?"},
        ],
    ),
    (
        "time",
        lambda d, m: d.get("period") == "morning",
        [
            {"state": "waving", "say": "Gooood morning! New day, fresh bugs to squash~"},
            {"state": "happy", "say": "Morning! I already feel like today's a good one. ☀"},
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

    def reconfigure(self, behavior_config: dict) -> None:
        """Re-apply tunable reaction parameters in place."""
        cfg = behavior_config or {}
        self._cooldown_seconds = cfg.get("reaction_cooldown", self._cooldown_seconds)

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
