"""
Learning Engine
===============

Fully-local, long-term learning. On a timer (and optionally at shutdown), the
engine reads the tail of the conversation log and asks the LOCAL Ollama model to
distill a compact, durable profile of the user — their preferences, the tone
they like, what they're into, and the tools/languages they use. That profile is
merged into the `user_profile` table and surfaced back into Shiro's system
prompt, so she gradually learns the user's "way of talking" and what matters to
them.

Design notes:
- Local only. No cloud providers are ever called from here.
- Robust by construction: every distillation pass is wrapped in try/except and
  logs warnings — a bad model response, a network blip, or malformed JSON must
  never crash or block the brain.
- Modes: "explicit_only" disables distillation entirely (only the
  remember_fact tool writes memory); "auto" runs the periodic distillation.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from brain.persistence import Database

logger = logging.getLogger("hypr-buddy.brain.learning")

# Sections we track in the user_profile table.
_PROFILE_SECTIONS = ("preferences", "tone", "topics", "tools_langs")

# Fixed distillation system prompt.
_DISTILL_SYSTEM_PROMPT = (
    "You maintain a compact JSON profile of the user for a desktop assistant. "
    "Given the EXISTING profile and RECENT conversation, output ONLY a JSON "
    "object with keys preferences, tone, topics, tools_langs — each a string of "
    "at most 2 short sentences capturing durable facts (skip one-off chatter). "
    "Merge with, don't discard, the existing profile."
)

# Find the first {...} block, tolerating leading prose / code fences.
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

# Distillation can be slow on a local model; give it room.
_DISTILL_TIMEOUT = 120.0


class LearningEngine:
    """Periodically distills the conversation log into a durable user profile."""

    def __init__(
        self,
        learning_config: dict,
        llm_config: dict,
        db: Database,
    ) -> None:
        self._cfg = dict(learning_config or {})
        self._llm_cfg = dict(llm_config or {})
        self._db = db

        self._enabled: bool = bool(self._cfg.get("enabled", True))
        self._mode: str = str(self._cfg.get("mode", "auto"))
        self._interval_min: int = int(self._cfg.get("distill_interval_min", 30))
        self._min_new_messages: int = int(self._cfg.get("min_new_messages", 10))

        # Local Ollama details, mirrored from the LLM config.
        self._model: str = self._llm_cfg.get("model", "gemma4:e2b")
        self._ollama_url: str = self._llm_cfg.get(
            "ollama_url", "http://localhost:11434"
        ).rstrip("/")

    def _distillation_active(self) -> bool:
        """Whether periodic/shutdown distillation should run at all."""
        return self._enabled and self._mode != "explicit_only"

    async def run(self) -> None:
        """Main loop: distill every `distill_interval_min` minutes."""
        if not self._distillation_active():
            logger.info(
                "Learning distillation disabled (enabled=%s, mode=%s).",
                self._enabled, self._mode,
            )
            return

        import asyncio

        logger.info(
            "Learning engine started (interval=%dm, min_new=%d, model=%s).",
            self._interval_min, self._min_new_messages, self._model,
        )
        interval_sec = max(1, self._interval_min) * 60
        while True:
            await asyncio.sleep(interval_sec)
            await self.distill_once()

    async def distill_once(self) -> None:
        """Run a single distillation pass. Never raises."""
        try:
            last_id_raw = await self._db.get_learning_state(
                "last_distilled_conversation_id", "0"
            )
            try:
                last_id = int(last_id_raw or 0)
            except (TypeError, ValueError):
                last_id = 0

            rows = await self._db.get_conversations_since(last_id)
            if len(rows) < self._min_new_messages:
                logger.debug(
                    "Distill skipped: %d new messages (< %d).",
                    len(rows), self._min_new_messages,
                )
                return

            existing_profile = await self._db.get_profile()
            user_message = self._build_user_message(existing_profile, rows)

            raw = await self._call_ollama(user_message)
            if not raw:
                logger.warning("Distill: empty response from model.")
                # Still advance the cursor so we don't re-process forever.
                await self._advance_cursor(rows)
                return

            parsed = self._parse_profile_json(raw)
            if parsed:
                for section in _PROFILE_SECTIONS:
                    content = parsed.get(section)
                    if isinstance(content, str) and content.strip():
                        await self._db.upsert_profile_section(
                            section, content.strip()
                        )
                logger.info(
                    "Distilled profile updated (%d new messages).", len(rows)
                )
            else:
                logger.warning("Distill: could not parse profile JSON from model.")

            # Advance regardless of parse success — these rows have been seen.
            await self._advance_cursor(rows)
        except Exception as e:  # noqa: BLE001 — distillation must never crash
            logger.warning("Distillation pass failed: %s", e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _advance_cursor(self, rows: list[tuple[int, str, str]]) -> None:
        """Move the distillation cursor past the rows we just consumed."""
        if not rows:
            return
        max_id = max(r[0] for r in rows)
        await self._db.set_learning_state(
            "last_distilled_conversation_id", str(max_id)
        )

    @staticmethod
    def _build_user_message(
        existing_profile: dict[str, str],
        rows: list[tuple[int, str, str]],
    ) -> str:
        """Compose the user-turn payload: existing profile + recent transcript."""
        profile_obj = {
            section: existing_profile.get(section, "")
            for section in _PROFILE_SECTIONS
        }
        profile_json = json.dumps(profile_obj, ensure_ascii=False)

        transcript_lines = []
        for _id, role, content in rows:
            role_label = "User" if role == "user" else "Assistant"
            transcript_lines.append(f"{role_label}: {content}")
        transcript = "\n".join(transcript_lines)

        return (
            "EXISTING profile (JSON):\n"
            f"{profile_json}\n\n"
            "RECENT conversation:\n"
            f"{transcript}\n\n"
            "Output the merged profile as a single JSON object now."
        )

    async def _call_ollama(self, user_message: str) -> str:
        """One non-streaming /api/chat request to the local Ollama model."""
        req_json = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _DISTILL_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=_DISTILL_TIMEOUT) as client:
            resp = await client.post(f"{self._ollama_url}/api/chat", json=req_json)
            resp.raise_for_status()
            data = resp.json()
        return (data.get("message", {}) or {}).get("content", "") or ""

    @staticmethod
    def _parse_profile_json(raw: str) -> dict | None:
        """Robustly extract a profile dict from a model response.

        Tolerates code fences and surrounding prose by grabbing the first
        balanced-ish {...} block and json-loading it.
        """
        text = raw.strip()
        # Strip a leading ```json / ``` fence if present.
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()

        # Direct parse first.
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass

        # Fallback: first {...} block anywhere in the text.
        match = _JSON_BLOCK_RE.search(raw)
        if match:
            try:
                obj = json.loads(match.group(0))
                if isinstance(obj, dict):
                    return obj
            except Exception:  # noqa: BLE001
                return None
        return None
