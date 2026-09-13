"""
LLM Backend (Coordinator)
=========================

Public surface preserved: `LLMBackend(...)` + `chat(...)`. Internally this is
now a coordinator over a pluggable provider layer (ollama / gemini / anthropic),
each handling text + tool-calling + optional image input.

It also owns the `look_at_screen` round-trip: when the text provider asks to
use that tool, the coordinator captures the screen and asks the VISION provider
(one-shot, no tools) for a description, returning that text as the tool result.

Memory behavior is intentionally unchanged — `get_memories()` is still passed
straight through to the system prompt.
"""

from __future__ import annotations

import logging
import asyncio
from collections import deque
from pathlib import Path

from brain import vision
from brain.personality import Personality
from brain.providers import ChatTurn, build_provider
from brain.tools import Tools, TOOLS_SCHEMA

logger = logging.getLogger("hypr-buddy.brain.llm")


class LLMBackend:
    """Coordinates LLM-powered conversations across pluggable providers."""

    def __init__(
        self,
        llm_config: dict,
        personality: Personality,
        tools_engine: Tools = None,
        vision_config: dict | None = None,
        db=None,
        memory_limit: int = 5,
    ) -> None:
        self._llm_config = dict(llm_config)
        self._vision_config = dict(vision_config or {})
        self._personality = personality
        self.tools_engine = tools_engine
        # How many usage-ranked memories to inject into the system prompt.
        # Settable at runtime (e.g. on SIGHUP reload).
        self.memory_limit: int = int(memory_limit)
        # Optional async Database. When present, the system prompt is enriched
        # with the distilled user profile and usage-ranked memories instead of
        # the flat get_memories() fallback.
        self._db = db

        self._ollama_url: str = llm_config.get("ollama_url", "http://localhost:11434")
        self._max_tokens: int = int(llm_config.get("max_tokens", 512))

        # Vision gate: whether look_at_screen is offered at all.
        self._vision_enabled: bool = bool(self._vision_config.get("enabled", False))

        # Cache of loaded key-file contents so repeated provider rebuilds don't
        # re-read disk.
        self._key_cache: dict[str, str | None] = {}

        # --- Build the text provider -------------------------------------
        text_provider = llm_config.get("provider", llm_config.get("backend", "ollama"))
        text_model = llm_config.get("model", "gemma4:e2b")
        self._text_provider_name = text_provider
        self._text_model = text_model
        self._text = self._make_provider(text_provider, text_model, vision=False)

        # --- Build the vision provider -----------------------------------
        vis_provider = self._vision_config.get("provider", "ollama")
        vis_model = self._vision_config.get("model", "qwen2.5vl:3b")
        self._vision_provider_name = vis_provider
        self._vision_model = vis_model
        self._vision = self._make_provider(vis_provider, vis_model, vision=True)

        # Conversation history: deque of {"role", "content"} (unchanged shape).
        max_history = llm_config.get("conversation_history_length", 20)
        self._history: deque[dict[str, str]] = deque(maxlen=max_history)

    async def prepare(self) -> None:
        warmup = getattr(self._text, "warmup", None)
        if warmup is not None:
            try:
                await warmup()
            except Exception as exc:
                logger.warning("Model warmup failed: %s", exc)

    # ------------------------------------------------------------------
    # Key loading
    # ------------------------------------------------------------------
    def _load_key_file(self, path: str) -> str | None:
        """Load an API key from a file path, warning on lax permissions."""
        if not path:
            return None
        expanded = Path(path).expanduser()
        if not expanded.exists():
            return None
        mode = expanded.stat().st_mode & 0o777
        if mode & 0o077:
            logger.warning(
                "API key file %s has overly permissive mode %o — "
                "should be 0600 (owner-only). Fix with: chmod 600 %s",
                expanded, mode, expanded,
            )
        return expanded.read_text().strip()

    def _key_loader(self, config_key: str) -> str | None:
        """Resolve a config key (e.g. 'gemini_api_key_file') to key contents."""
        if config_key in self._key_cache:
            return self._key_cache[config_key]
        path = self._llm_config.get(config_key, "")
        key = self._load_key_file(path)
        self._key_cache[config_key] = key
        return key

    # ------------------------------------------------------------------
    # Provider construction / runtime swap
    # ------------------------------------------------------------------
    def _make_provider(self, provider: str, model: str, vision: bool):
        cfg = dict(self._llm_config)
        cfg["model"] = model
        cfg["max_tokens"] = self._max_tokens
        cfg["ollama_url"] = self._ollama_url
        if vision:
            # Vision Ollama models can be kept warm via keep_alive.
            cfg["keep_alive"] = self._vision_config.get("keep_alive")
        return build_provider(provider, cfg, self._key_loader)

    def set_text_provider(self, provider: str, model: str | None = None) -> None:
        """Hot-swap the text provider (and optionally its model)."""
        chosen = model or (self._text_model if provider == self._text_provider_name else "")
        replacement = self._make_provider(provider, chosen, vision=False)
        self._text_provider_name, self._text_model, self._text = provider, chosen, replacement
        logger.info("Text provider swapped to %s (%s)", provider, self._text_model)

    def set_vision_provider(self, provider: str, model: str | None = None) -> None:
        """Hot-swap the vision provider (and optionally its model)."""
        chosen = model or (self._vision_model if provider == self._vision_provider_name else "")
        replacement = self._make_provider(provider, chosen, vision=True)
        self._vision_provider_name, self._vision_model, self._vision = provider, chosen, replacement
        logger.info("Vision provider swapped to %s (%s)", provider, self._vision_model)

    @property
    def vision_provider(self):
        """Expose the vision provider (used by AmbientVision)."""
        return self._vision

    # ------------------------------------------------------------------
    # The look_at_screen round-trip
    # ------------------------------------------------------------------
    async def _vision_describe(self, reason: str) -> str:
        """Capture the screen and ask the vision provider to describe it."""
        png = await vision.capture_screen(active_monitor_only=True)
        if not png:
            return "I tried to look but couldn't capture the screen right now."
        try:
            return await self._vision.chat(
                system_prompt=(
                    f"Describe what is on the user's screen, focused on: {reason}"
                ),
                history=[],
                tools_schema=[],
                execute_tool=self._noop_tool,
                images=[png],
                max_tool_iters=1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Vision describe failed: %s", e)
            return "I had trouble making sense of the screen just now."

    @staticmethod
    async def _noop_tool(name: str, args: dict) -> str:
        return "Tools are not available in vision-only mode."

    def _make_execute_wrapper(self):
        """Build the async execute_tool callable injected into providers."""

        async def execute_tool(name: str, args: dict) -> str:
            if name == "look_at_screen":
                if not self._vision_enabled:
                    return "Vision is disabled."
                reason = (args or {}).get("reason", "what the user is looking at")
                return await self._vision_describe(reason)
            # All other tools are synchronous on the Tools engine.
            if self.tools_engine is None:
                return f"Error: tool {name} unavailable (no tools engine)."
            return self.tools_engine.execute(name, args)

        return execute_tool

    def _active_tools_schema(self) -> list[dict]:
        """Tool schema offered to the text provider.

        Omits look_at_screen when vision is disabled. Returns [] when there is
        no tools engine at all.
        """
        if self.tools_engine is None:
            return []
        if self._vision_enabled:
            return TOOLS_SCHEMA
        return [
            f for f in TOOLS_SCHEMA
            if f.get("function", {}).get("name") != "look_at_screen"
        ]

    # ------------------------------------------------------------------
    # Public chat surface
    # ------------------------------------------------------------------
    async def chat(
        self,
        user_message: str,
        mood_label: str,
        active_app: str | None = None,
        time_period: str | None = None,
        recent_events: list[str] | None = None,
        system_stats: dict[str, int] | None = None,
        ambient_view: str | None = None,
    ) -> str:
        """Send a user message and get a response from the active text provider."""

        # Prefer the DB-backed path: distilled profile + usage-ranked memories.
        # Fall back to the flat, unranked get_memories() when no db is wired in.
        profile: dict[str, str] | None = None
        if self._db is not None:
            try:
                profile = await self._db.get_profile()
                memories = await self._db.get_relevant_memories(limit=self.memory_limit)
            except Exception as e:  # noqa: BLE001
                logger.warning("Profile/memory fetch failed, falling back: %s", e)
                profile = None
                memories = self.tools_engine.get_memories() if self.tools_engine else []
        else:
            memories = self.tools_engine.get_memories() if self.tools_engine else []

        system_prompt = self._personality.build_system_prompt(
            mood_label=mood_label,
            active_app=active_app,
            time_period=time_period,
            recent_events=recent_events,
            system_stats=system_stats,
            memories=memories,
            profile=profile,
            ambient_view=ambient_view,
            vision_enabled=self._vision_enabled,
        )

        self._history.append({"role": "user", "content": user_message})
        # Convert deque history -> provider-agnostic ChatTurns.
        turns = [
            ChatTurn(role=m["role"], text=m["content"])
            for m in self._history
        ]

        try:
            response = await asyncio.wait_for(self._text.chat(
                system_prompt=system_prompt,
                history=turns,
                tools_schema=self._active_tools_schema(),
                execute_tool=self._make_execute_wrapper(),
                max_tool_iters=3,
            ), timeout=float(self._llm_config.get("turn_timeout_seconds", 120)))
        except asyncio.CancelledError:
            self._history.pop()
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("LLM error: %s", e)
            self._history.pop()
            return "I couldn’t reach my language model. Check the provider and model with scripts/doctor.py."

        self._history.append({"role": "assistant", "content": response})
        return response
