"""
Gemini Provider
===============

Talks to Google's Generative Language API (generateContent). The API key is a
query parameter. Tools use function_declarations + AUTO function calling; the
tool-call loop appends functionResponse parts and re-requests.

Images attach as inline_data parts (base64 PNG) on the latest user turn.
"""

from __future__ import annotations

import base64
import logging

import httpx

from brain.providers.base import (
    ChatTurn,
    ExecuteTool,
    LLMProvider,
    to_gemini_decls,
)

logger = logging.getLogger("hypr-buddy.brain.providers.gemini")

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None,
                 max_tokens: int = 512) -> None:
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens

    @property
    def supports_vision(self) -> bool:
        return True

    @staticmethod
    def _image_part(img: bytes) -> dict:
        return {
            "inline_data": {
                "mime_type": "image/png",
                "data": base64.b64encode(img).decode("ascii"),
            }
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
            return "Gemini API key not configured!"

        # Build contents from history. Gemini roles are "user" / "model".
        contents: list[dict] = []
        for i, turn in enumerate(history):
            role = "model" if turn.role == "assistant" else "user"
            parts: list[dict] = [{"text": turn.text}]
            if images and i == len(history) - 1 and role == "user":
                for img in images:
                    parts.append(self._image_part(img))
            contents.append({"role": role, "parts": parts})

        if images and (not history or contents[-1]["role"] != "user"):
            parts = [self._image_part(img) for img in images]
            contents.append({"role": "user", "parts": parts})

        body: dict = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self._max_tokens},
        }
        if tools_schema:
            body["tools"] = [{"function_declarations": to_gemini_decls(tools_schema)}]
            body["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}

        url = f"{_BASE}/{self._model}:generateContent?key={self._api_key}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            for _ in range(max_tool_iters):
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()

                candidates = data.get("candidates", [])
                if not candidates:
                    return "..."
                content = candidates[0].get("content", {})
                parts = content.get("parts", []) or []

                # Collect function calls and text from this response.
                fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]
                texts = [p["text"] for p in parts if "text" in p]

                if fn_calls:
                    # Append the model's content, then a user turn with the
                    # functionResponse(s), and loop.
                    contents.append({"role": "model", "parts": parts})
                    response_parts: list[dict] = []
                    for fc in fn_calls:
                        name = fc.get("name", "")
                        args = fc.get("args", {}) or {}
                        result = await execute_tool(name, args)
                        response_parts.append({
                            "functionResponse": {
                                "name": name,
                                "response": {"result": str(result)},
                            }
                        })
                    contents.append({"role": "user", "parts": response_parts})
                    continue

                return " ".join(t.strip() for t in texts).strip() or "..."

            return "..."
