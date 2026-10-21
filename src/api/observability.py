"""Structured task-event audit trail for Verity-1.

This module is deliberately dependency-light so it can be tested without a model
provider, browser session, or network access.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TaskEventStore:
    """Append-only, timestamped task events stored in SQLite."""

    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS verity_task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_verity_task_events_task "
                "ON verity_task_events(task_id, event_id)"
            )

    def append(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> int:
        if not task_id or len(task_id) > 80:
            raise ValueError("task_id must contain 1 to 80 characters.")
        if not event_type or len(event_type) > 100:
            raise ValueError("event_type must contain 1 to 100 characters.")
        serialized = json.dumps(payload or {}, ensure_ascii=False, allow_nan=False)
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            cursor = connection.execute(
                "INSERT INTO verity_task_events(task_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, event_type, serialized, datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.lastrowid)

    def list_events(self, task_id: str, *, limit: int = 100, after_id: int = 0) -> list[dict[str, Any]]:
        if not task_id or len(task_id) > 80:
            raise ValueError("task_id must contain 1 to 80 characters.")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        if after_id < 0:
            raise ValueError("after_id must be zero or greater.")
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            rows = connection.execute(
                "SELECT event_id, task_id, event_type, payload_json, created_at "
                "FROM verity_task_events WHERE task_id=? AND event_id>? "
                "ORDER BY event_id ASC LIMIT ?",
                (task_id, after_id, limit),
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "task_id": row[1],
                "event_type": row[2],
                "payload": json.loads(row[3]),
                "created_at": row[4],
            }
            for row in rows
        ]

    def list_tasks(self, *, limit: int = 50, offset: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200.")
        if offset < 0:
            raise ValueError("offset must be zero or greater.")
        with sqlite3.connect(str(self.database_path), timeout=30) as connection:
            if status:
                rows = connection.execute(
                    "SELECT task_id, objective, status, created_at, updated_at FROM verity_tasks "
                    "WHERE status=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT task_id, objective, status, created_at, updated_at FROM verity_tasks "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [
            {"task_id": row[0], "objective": row[1], "status": row[2],
             "created_at": row[3], "updated_at": row[4]}
            for row in rows
        ]
