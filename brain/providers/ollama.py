"""Ollama native tool calls with bounded local inference."""

from __future__ import annotations

import base64
import logging

import httpx

from brain.providers.base import ChatTurn, ExecuteTool, LLMProvider

logger = logging.getLogger("hypr-buddy.brain.providers.ollama")



class OllamaProvider(LLMProvider):
    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 keep_alive: str | None = None, max_tokens: int = 256,
                 num_ctx: int = 4096, timeout: float = 90, think: bool = False) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._keep_alive = keep_alive
        self._max_tokens = max_tokens
        self._num_ctx = num_ctx
        self._timeout = timeout
        self._think = think

    async def warmup(self) -> None:
        """Load weights before the first spoken turn without generating text."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}/api/generate", json={
                "model": self._model, "prompt": "", "stream": False,
                "keep_alive": self._keep_alive or "5m",
                "options": {"num_ctx": self._num_ctx},
            })
            response.raise_for_status()

    @property
    def supports_vision(self) -> bool:
        # Whether the configured model can actually see depends on the model
        # itself; we let the caller pick a vision-capable model. We report True
        # so the coordinator will attempt image attachment for the vision path.
        return True

    @staticmethod
    def _b64(images: list[bytes]) -> list[str]:
        return [base64.b64encode(img).decode("ascii") for img in images]

    async def chat(
        self,
        system_prompt: str,
        history: list[ChatTurn],
        tools_schema: list[dict],
        execute_tool: ExecuteTool,
        images: list[bytes] | None = None,
        max_tool_iters: int = 3,
    ) -> str:
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for i, turn in enumerate(history):
            msg: dict = {"role": turn.role, "content": turn.text}
            # Attach images one-shot to the LAST turn if provided here.
            if images and i == len(history) - 1:
                msg["images"] = self._b64(images)
            messages.append(msg)

        # If history is empty but images were supplied, still attach them.
        if images and not history:
            messages.append({"role": "user", "content": "", "images": self._b64(images)})

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for iteration in range(max_tool_iters + 1):
                req_json: dict = {
                    "model": self._model,
                    "messages": messages,
                    "stream": False,
                    "think": self._think,
                    "options": {"num_predict": self._max_tokens, "num_ctx": self._num_ctx},
                }
                if tools_schema and iteration < max_tool_iters:
                    req_json["tools"] = tools_schema
                if self._keep_alive is not None:
                    req_json["keep_alive"] = self._keep_alive

                resp = await client.post(f"{self._base_url}/api/chat", json=req_json)
                resp.raise_for_status()
                data = resp.json()
                msg = data.get("message", {})
                messages.append(msg)

                # Native tool calls
                if msg.get("tool_calls") and iteration < max_tool_iters:
                    for tc in msg["tool_calls"]:
                        fn_name = tc["function"]["name"]
                        fn_args = tc["function"].get("arguments", {})
                        allowed = {t["function"]["name"] for t in tools_schema}
                        if fn_name not in allowed or not isinstance(fn_args, dict):
                            raise ValueError("Invalid tool call from model")
                        tool_result = await execute_tool(fn_name, fn_args)
                        messages.append({
                            "role": "tool",
                            "name": fn_name,
                            "content": str(tool_result),
                        })
                    continue

                content = msg.get("content", "")
                return content.strip() or "I couldn’t form a reply. Could you try that again?"

            return "I reached the tool limit. Tell me if you want me to continue."
