"""
LLM Backend
=============

Provides conversation capability via either a local Ollama instance
or the Anthropic API. Used when the user directly talks to the buddy.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path

import httpx

from brain.personality import Personality

logger = logging.getLogger("hypr-buddy.brain.llm")


class LLMBackend:
    """Handles LLM-powered conversations with the buddy character."""

    def __init__(self, llm_config: dict, personality: Personality) -> None:
        self._backend: str = llm_config.get("backend", "ollama")
        self._model: str = llm_config.get("model", "mistral")
        self._ollama_url: str = llm_config.get("ollama_url", "http://localhost:11434")
        self._personality = personality

        # Load Anthropic API key if configured
        self._anthropic_key: str | None = None
        key_path = llm_config.get("anthropic_api_key_file", "")
        if key_path:
            expanded = Path(key_path).expanduser()
            if expanded.exists():
                # Warn if key file is readable by others
                mode = expanded.stat().st_mode & 0o777
                if mode & 0o077:
                    logger.warning(
                        "API key file %s has overly permissive mode %o — "
                        "should be 0600 (owner-only). Fix with: chmod 600 %s",
                        expanded, mode, expanded,
                    )
                self._anthropic_key = expanded.read_text().strip()

        # Conversation history: list of {"role": "user"|"assistant", "content": str}
        max_history = llm_config.get("conversation_history_length", 20)
        self._history: deque[dict[str, str]] = deque(maxlen=max_history)

    async def chat(
        self,
        user_message: str,
        mood_label: str,
        active_app: str | None = None,
        time_period: str | None = None,
        recent_events: list[str] | None = None,
    ) -> str:
        """Send a user message and get a response from the LLM."""
        self._history.append({"role": "user", "content": user_message})

        system_prompt = self._personality.build_system_prompt(
            mood_label=mood_label,
            active_app=active_app,
            time_period=time_period,
            recent_events=recent_events,
        )

        try:
            if self._backend == "ollama":
                response = await self._chat_ollama(system_prompt)
            elif self._backend == "anthropic":
                response = await self._chat_anthropic(system_prompt)
            else:
                response = f"Unknown LLM backend: {self._backend}"
        except Exception as e:
            logger.error("LLM error: %s", e)
            response = "Hmm, my brain is a bit fuzzy right now... try again in a moment?"

        self._history.append({"role": "assistant", "content": response})
        return response

    async def _chat_ollama(self, system_prompt: str) -> str:
        """Chat via local Ollama instance."""
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(list(self._history))

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._ollama_url}/api/chat",
                json={
                    "model": self._model,
                    "messages": messages,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "...").strip()

    async def _chat_anthropic(self, system_prompt: str) -> str:
        """Chat via Anthropic API."""
        if not self._anthropic_key:
            return "Anthropic API key not configured!"

        messages = list(self._history)

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": 256,
                    "system": system_prompt,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content", [])
            if content and isinstance(content, list):
                return content[0].get("text", "...").strip()
            return "..."
