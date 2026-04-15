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

logger = logging.getLogger("virtual-buddy.brain.persistence")

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
"""


class Database:
    """Async SQLite database for buddy state persistence."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open the database and create tables if needed."""
        self._db = await aiosqlite.connect(str(self._path))
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("Database initialized at %s", self._path)

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
