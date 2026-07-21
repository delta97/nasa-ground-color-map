"""Versioned SQLite storage for single-container monitoring."""

from __future__ import annotations

from pathlib import Path

import aiosqlite


MIGRATIONS = [
"""
CREATE TABLE regions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
 geometry_json TEXT NOT NULL, wraps_antimeridian INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE monitors (
 id INTEGER PRIMARY KEY AUTOINCREMENT, region_id INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
 product TEXT NOT NULL, metric TEXT NOT NULL, rule_type TEXT NOT NULL,
 threshold REAL, minimum_quality TEXT NOT NULL DEFAULT 'usable', run_hour INTEGER NOT NULL,
 webhook_url TEXT, enabled INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 0,
 previous_value REAL, previous_quality TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
 observation_date TEXT NOT NULL, value REAL, quality TEXT NOT NULL, payload_json TEXT NOT NULL,
 accepted INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(monitor_id, observation_date)
);
CREATE TABLE monitor_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
 observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
 event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE webhook_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES monitor_events(id) ON DELETE CASCADE,
 attempt INTEGER NOT NULL, response_status INTEGER, error TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX monitor_events_created_idx ON monitor_events(created_at DESC, id DESC);
"""
]


class MonitoringStore:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        cursor = await self.db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        current = (await cursor.fetchone())[0]
        for index, sql in enumerate(MIGRATIONS, 1):
            if index <= current: continue
            await self.db.executescript(sql)
            await self.db.execute("INSERT INTO schema_migrations(version) VALUES (?)", (index,))
        await self.db.commit()
        return self

    async def close(self):
        if self.db is not None: await self.db.close(); self.db = None

    async def health(self) -> bool:
        if self.db is None: return False
        try:
            row = await (await self.db.execute("PRAGMA quick_check")).fetchone()
            return row[0] == "ok"
        except aiosqlite.Error: return False

    async def fetchall(self, sql: str, params=()):
        return [dict(row) for row in await (await self.db.execute(sql, params)).fetchall()]

    async def fetchone(self, sql: str, params=()):
        row = await (await self.db.execute(sql, params)).fetchone(); return dict(row) if row else None

    async def execute(self, sql: str, params=()):
        cursor = await self.db.execute(sql, params); await self.db.commit(); return cursor

    async def prune(self, retention_days: int):
        await self.db.execute("DELETE FROM webhook_attempts WHERE created_at < datetime('now', ?)", (f"-{retention_days} days",))
        await self.db.execute("DELETE FROM monitor_events WHERE created_at < datetime('now', ?)", (f"-{retention_days} days",))
        await self.db.execute("DELETE FROM observations WHERE created_at < datetime('now', ?)", (f"-{retention_days} days",))
        await self.db.commit()
