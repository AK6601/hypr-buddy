"""
Provider Factory
================

Builds a concrete LLMProvider from a provider name + config dict. The
`key_loader` callable resolves API-key files to their string contents (or
None), so providers stay decoupled from how keys are stored on disk.
"""

from __future__ import annotations

import logging
from typing import Callable

from brain.providers.base import (
    ChatTurn,
    ExecuteTool,
    LLMProvider,
    to_anthropic_tools,
    to_gemini_decls,
)

logger = logging.getLogger("hypr-buddy.brain.providers")

# A callable that takes a config key (e.g. "anthropic_api_key_file") and
# returns the loaded key string, or None if missing/unset.
KeyLoader = Callable[[str], "str | None"]

__all__ = [
    "build_provider",
    "LLMProvider",
    "ChatTurn",
    "ExecuteTool",
    "to_gemini_decls",
    "to_anthropic_tools",
]


def build_provider(provider: str, cfg: dict, key_loader: KeyLoader) -> LLMProvider:
    """Construct a provider instance.

    Args:
        provider: "ollama" | "gemini" | "anthropic".
        cfg: merged config dict (typically the [llm] table augmented with the
             chosen model / max_tokens / keep_alive).
        key_loader: resolves api-key-file config keys to their contents.
    """
    provider = (provider or "ollama").lower()
    model = cfg.get("model", "")
    max_tokens = int(cfg.get("max_tokens", 512))

    if provider == "ollama":
        from brain.providers.ollama import OllamaProvider
        return OllamaProvider(
            model=model or "gemma4:e2b",
            base_url=cfg.get("ollama_url", "http://localhost:11434"),
            keep_alive=cfg.get("keep_alive", "5m"),
            max_tokens=max_tokens,
            num_ctx=int(cfg.get("num_ctx", 4096)),
            timeout=float(cfg.get("timeout_seconds", 90)),
            think=cfg.get("think", False),
        )

    if provider == "gemini":
        from brain.providers.gemini import GeminiProvider
        key = key_loader("gemini_api_key_file")
        return GeminiProvider(
            model=model or "gemini-2.5-flash",
            api_key=key,
            max_tokens=max_tokens,
        )

    if provider == "anthropic":
        from brain.providers.anthropic import AnthropicProvider
        key = key_loader("anthropic_api_key_file")
        return AnthropicProvider(
            model=model or "claude-haiku-4-5",
            api_key=key,
            max_tokens=max_tokens,
        )

    raise ValueError(f"Unknown provider: {provider}")
