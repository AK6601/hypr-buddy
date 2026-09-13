"""
Provider Base Layer
===================

Defines the common abstraction every LLM/VLM provider implements, plus the
small data structures and schema converters shared across providers.

Design notes:
  - Providers NEVER import the Tools engine. Tool execution is injected by the
    coordinator (LLMBackend) as an async callable `execute_tool(name, args)`.
  - A provider runs its own tool-call loop (native or fallback) and returns the
    final assistant text.
  - `images` are optional raw PNG bytes attached one-shot to the latest turn.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# Type of the injected tool executor. It is ALWAYS awaited by providers, even
# though the underlying Tools.execute is synchronous — the coordinator wraps it.
ExecuteTool = Callable[[str, dict], Awaitable[str]]


@dataclass
class ChatTurn:
    """A single conversation turn, provider-agnostic.

    `images` carries raw PNG bytes to attach to this turn (used for the
    one-shot vision path). Most turns have no images.
    """

    role: str  # "user" | "assistant"
    text: str
    images: list[bytes] = field(default_factory=list)


class LLMProvider(abc.ABC):
    """Abstract base for a text/vision provider."""

    @property
    @abc.abstractmethod
    def supports_vision(self) -> bool:
        """Whether this provider/model can accept image input."""
        raise NotImplementedError

    @abc.abstractmethod
    async def chat(
        self,
        system_prompt: str,
        history: list[ChatTurn],
        tools_schema: list[dict],
        execute_tool: ExecuteTool,
        images: list[bytes] | None = None,
        max_tool_iters: int = 3,
    ) -> str:
        """Run a chat completion, executing any tool calls in a loop.

        Returns the final assistant text.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Schema converters: OpenAI-style TOOLS_SCHEMA -> provider-specific shapes
# ---------------------------------------------------------------------------

def to_gemini_decls(tools_schema: list[dict]) -> list[dict]:
    """Convert OpenAI-style tool schema to Gemini function declarations."""
    decls: list[dict] = []
    for f in tools_schema:
        fn = f.get("function", {})
        decls.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return decls


def to_anthropic_tools(tools_schema: list[dict]) -> list[dict]:
    """Convert OpenAI-style tool schema to Anthropic tool definitions."""
    tools: list[dict] = []
    for f in tools_schema:
        fn = f.get("function", {})
        tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return tools
