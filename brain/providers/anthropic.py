"""
Anthropic Provider
===================

Talks to the Anthropic Messages API. Tools use the native tool_use /
tool_result flow; the loop continues while stop_reason == "tool_use".

Images attach as image blocks (base64 PNG) in the latest user message's
content list. `max_tokens` is required by the API.
"""

from __future__ import annotations

import base64
import logging

import httpx

from brain.providers.base import (
    ChatTurn,
    ExecuteTool,
    LLMProvider,
    to_anthropic_tools,
)

logger = logging.getLogger("hypr-buddy.brain.providers.anthropic")

_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None,
                 max_tokens: int = 512) -> None:
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens

    @property
    def supports_vision(self) -> bool:
        return True

    @staticmethod
    def _image_block(img: bytes) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(img).decode("ascii"),
            },
        }

    async def chat(
        self,
        system_prompt: str,
        history: list[ChatTurn],
        tools_schema: list[dict],
        execute_tool: ExecuteTool,
        images: list[bytes] | None = None,
        max_tool_iters: int = 3,
    ) -> str:
        if not self._api_key:
            return "Anthropic API key not configured!"

        # Build messages with content as a block list so we can attach images.
        messages: list[dict] = []
        for i, turn in enumerate(history):
            blocks: list[dict] = [{"type": "text", "text": turn.text}]
            if images and i == len(history) - 1 and turn.role == "user":
                for img in images:
                    blocks.append(self._image_block(img))
            messages.append({"role": turn.role, "content": blocks})

        if images and (not history or messages[-1]["role"] != "user"):
            messages.append({
                "role": "user",
                "content": [self._image_block(img) for img in images],
            })

        body: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        if tools_schema:
            body["tools"] = to_anthropic_tools(tools_schema)

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            for _ in range(max_tool_iters):
                resp = await client.post(_URL, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()

                content = data.get("content", []) or []
                stop_reason = data.get("stop_reason")

                if stop_reason == "tool_use":
                    # Append assistant content, then a user message with the
                    # tool_result block(s), and loop.
                    messages.append({"role": "assistant", "content": content})
                    result_blocks: list[dict] = []
                    for block in content:
                        if block.get("type") == "tool_use":
                            name = block.get("name", "")
                            args = block.get("input", {}) or {}
                            result = await execute_tool(name, args)
                            result_blocks.append({
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "content": str(result),
                            })
                    messages.append({"role": "user", "content": result_blocks})
                    continue

                # Final answer: concatenate text blocks.
                texts = [b.get("text", "") for b in content if b.get("type") == "text"]
                return " ".join(t.strip() for t in texts).strip() or "..."

            return "..."
