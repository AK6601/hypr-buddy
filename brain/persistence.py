"""
Persistence Layer
==================

SQLite-backed storage for mood history, conversation logs,
interaction counts, and daily summaries.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger("hypr-buddy.brain.persistence")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mood_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    response TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_counts (
    app_class TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_facts (
    id INTEGER PRIMARY KEY,
    fact TEXT,
    created_at REAL DEFAULT 0,
    last_used_at REAL DEFAULT 0,
    use_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'explicit'
);

CREATE TABLE IF NOT EXISTS user_profile (
    section TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns expected on memory_facts. tools.py may have created a bare
# memory_facts(id, fact) before the async Database ever runs SCHEMA, so we
# reconcile any missing columns at initialize() time.
_MEMORY_FACTS_COLUMNS = {
    "created_at": "REAL DEFAULT 0",
    "last_used_at": "REAL DEFAULT 0",
    "use_count": "INTEGER DEFAULT 0",
    "source": "TEXT DEFAULT 'explicit'",
}

# Recency window for the memory ranking bonus: 30 days in seconds.
SECONDS_30D = 30 * 24 * 60 * 60


class Database:
    """Async SQLite database for buddy state persistence."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open the database and create tables if needed."""
        self._db = await aiosqlite.connect(str(self._path))
        await self._db.executescript(SCHEMA)
        await self._migrate_memory_facts()
        await self._db.commit()
        logger.info("Database initialized at %s", self._path)

    async def _migrate_memory_facts(self) -> None:
        """Add any columns missing from a pre-existing bare memory_facts table.

        tools.py opens the same DB with a sync sqlite3 connection and may create
        a bare `memory_facts(id, fact)` before this async path ever runs. We read
        the live schema via PRAGMA and only ALTER in the columns that are absent,
        so the migration is idempotent and tolerant of either origin.
        """
        if self._db is None:
            return
        async with self._db.execute("PRAGMA table_info(memory_facts)") as cursor:
            rows = await cursor.fetchall()
        existing = {row[1] for row in rows}  # row[1] is the column name
        for col, decl in _MEMORY_FACTS_COLUMNS.items():
            if col in existing:
                continue
            try:
                await self._db.execute(
                    f"ALTER TABLE memory_facts ADD COLUMN {col} {decl}"
                )
            except Exception as e:  # noqa: BLE001 — tolerate "duplicate column"
                logger.debug("memory_facts ALTER %s skipped: %s", col, e)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def save_mood(self, value: float) -> None:
        """Save current mood to history."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        await self._db.execute(
            "INSERT INTO mood_history (timestamp, value) VALUES (?, ?)",
            (time.time(), value),
        )
        await self._db.commit()

    async def get_last_mood(self) -> float:
        """Retrieve the most recent mood value, or default 0.3."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        async with self._db.execute(
            "SELECT value FROM mood_history ORDER BY timestamp DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0.3

    async def log_interaction(self, event_type: str, response: str) -> None:
        """Log a reaction/response."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        await self._db.execute(
            "INSERT INTO interactions (timestamp, event_type, response) VALUES (?, ?, ?)",
            (time.time(), event_type, response),
        )
        await self._db.commit()

    async def log_conversation(self, role: str, content: str) -> None:
        """Log a conversation message."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        await self._db.execute(
            "INSERT INTO conversations (timestamp, role, content) VALUES (?, ?, ?)",
            (time.time(), role, content),
        )
        await self._db.commit()

    async def increment_app_count(self, app_class: str) -> None:
        """Increment the interaction count for an app."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        await self._db.execute(
            """INSERT INTO app_counts (app_class, count, last_seen) VALUES (?, 1, ?)
               ON CONFLICT(app_class) DO UPDATE SET count = count + 1, last_seen = ?""",
            (app_class, time.time(), time.time()),
        )
        await self._db.commit()

    async def get_app_counts(self, limit: int = 10) -> list[tuple[str, int]]:
        """Get the most-interacted-with apps."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        async with self._db.execute(
            "SELECT app_class, count FROM app_counts ORDER BY count DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return await cursor.fetchall()

    # ------------------------------------------------------------------
    # User profile (durable distilled facts, one row per section)
    # ------------------------------------------------------------------
    async def get_profile(self) -> dict[str, str]:
        """Return the stored profile as {section: content}."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        async with self._db.execute(
            "SELECT section, content FROM user_profile"
        ) as cursor:
            rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def upsert_profile_section(self, section: str, content: str) -> None:
        """Insert or update a single profile section, stamping updated_at."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        await self._db.execute(
            """INSERT INTO user_profile (section, content, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(section) DO UPDATE SET
                   content = excluded.content,
                   updated_at = excluded.updated_at""",
            (section, content, time.time()),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Conversation tailing (for learning distillation)
    # ------------------------------------------------------------------
    async def get_conversations_since(
        self, last_id: int, limit: int = 400
    ) -> list[tuple[int, str, str]]:
        """Return (id, role, content) rows with id > last_id, ordered by id.

        The conversations table predates an explicit autoincrement id in some
        DBs, so we read the implicit rowid as the id to stay schema-agnostic.
        """
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        async with self._db.execute(
            """SELECT rowid, role, content FROM conversations
               WHERE rowid > ? ORDER BY rowid ASC LIMIT ?""",
            (last_id, limit),
        ) as cursor:
            return await cursor.fetchall()

    # ------------------------------------------------------------------
    # Learning state (small key/value bag)
    # ------------------------------------------------------------------
    async def get_learning_state(self, key: str, default: str | None = None) -> str | None:
        """Read a learning_state value, returning `default` if unset."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        async with self._db.execute(
            "SELECT value FROM learning_state WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else default

    async def set_learning_state(self, key: str, value: str) -> None:
        """Set a learning_state value (string)."""
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        await self._db.execute(
            """INSERT INTO learning_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(value)),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Ranked memory retrieval
    # ------------------------------------------------------------------
    async def get_relevant_memories(self, limit: int = 5) -> list[str]:
        """Return up to `limit` fact strings, ranked by usage + recency.

        Ranking score per fact:
            score = use_count * 1.0
                  + max(0, 1 - (now - last_used_at) / SECONDS_30D)   # recency bonus
                  + (0.25 if source == 'explicit' else 0.0)          # explicit nudge

        Selected facts are "touched": use_count += 1 and last_used_at = now, so
        stale facts gradually sink while frequently-relevant ones stay surfaced.
        Robust to an empty table or a partial (pre-migration) schema.
        """
        if self._db is None:
            raise RuntimeError("Database not initialized — call initialize() first")
        now = time.time()
        try:
            # Pull rows defensively — COALESCE guards rows written before the
            # migration backfilled defaults.
            async with self._db.execute(
                """SELECT id, fact,
                          COALESCE(last_used_at, 0),
                          COALESCE(use_count, 0),
                          COALESCE(source, 'explicit')
                   FROM memory_facts
                   WHERE fact IS NOT NULL AND fact != ''"""
            ) as cursor:
                rows = await cursor.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("get_relevant_memories query failed: %s", e)
            return []

        if not rows:
            return []

        scored: list[tuple[float, int, str]] = []
        for row in rows:
            fact_id, fact, last_used_at, use_count, source = row
            recency_bonus = max(0.0, 1.0 - (now - (last_used_at or 0.0)) / SECONDS_30D)
            source_bonus = 0.25 if source == "explicit" else 0.0
            score = (use_count or 0) * 1.0 + recency_bonus + source_bonus
            scored.append((score, fact_id, fact))

        scored.sort(key=lambda t: t[0], reverse=True)
        chosen = scored[:limit]

        # Bump usage stats for the surfaced facts.
        if chosen:
            try:
                await self._db.executemany(
                    "UPDATE memory_facts SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                    [(now, fact_id) for _, fact_id, _ in chosen],
                )
                await self._db.commit()
            except Exception as e:  # noqa: BLE001
                logger.warning("get_relevant_memories bump failed: %s", e)

        return [fact for _, _, fact in chosen]
