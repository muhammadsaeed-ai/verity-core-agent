from __future__ import annotations

import json
import sqlite3

import pytest

from src.api.observability import TaskEventStore


def _seed_tasks(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS verity_tasks (task_id TEXT PRIMARY KEY, objective TEXT NOT NULL, "
            "status TEXT NOT NULL, state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO verity_tasks(task_id, objective, status, state_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("task-a", "Objective A", "completed", "{}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:02+00:00"),
                ("task-b", "Objective B", "failed", "{}", "2026-01-01T00:00:01+00:00", "2026-01-01T00:00:03+00:00"),
                ("task-c", "Objective C", "completed", "{}", "2026-01-01T00:00:02+00:00", "2026-01-01T00:00:02+00:00"),
            ],
        )


def test_event_store_preserves_order_and_json_payload(tmp_path):
    database = tmp_path / "events.sqlite3"
    store = TaskEventStore(database)
    first = store.append("task-1", "task.queued", {"source": "api", "count": 2})
    second = store.append("task-1", "task.started", {"safe": True})
    store.append("task-2", "task.queued", {"source": "api"})

    events = store.list_events("task-1")
    assert [event["event_id"] for event in events] == [first, second]
    assert events[0]["payload"] == {"source": "api", "count": 2}
    assert events[1]["event_type"] == "task.started"
    assert all(event["task_id"] == "task-1" for event in events)


def test_event_store_incremental_cursor_and_limit(tmp_path):
    store = TaskEventStore(tmp_path / "events.sqlite3")
    ids = [store.append("task-1", "step", {"index": i}) for i in range(4)]
    assert [e["event_id"] for e in store.list_events("task-1", limit=2)] == ids[:2]
    assert [e["event_id"] for e in store.list_events("task-1", after_id=ids[1])] == ids[2:]


def test_event_store_rejects_invalid_limits_ids_and_non_json_values(tmp_path):
    store = TaskEventStore(tmp_path / "events.sqlite3")
    with pytest.raises(ValueError):
        store.list_events("task-1", limit=0)
    with pytest.raises(ValueError):
        store.list_events("task-1", after_id=-1)
    with pytest.raises(ValueError):
        store.append("", "task.started")
    with pytest.raises(ValueError):
        store.append("task-1", "task.started", {"not_json": float("nan")})


def test_task_inventory_filters_status_and_paginates(tmp_path):
    database = tmp_path / "events.sqlite3"
    _seed_tasks(database)
    store = TaskEventStore(database)
    completed = store.list_tasks(status="completed", limit=1, offset=0)
    next_page = store.list_tasks(status="completed", limit=1, offset=1)
    all_tasks = store.list_tasks(limit=2, offset=0)

    assert len(completed) == 1
    assert completed[0]["status"] == "completed"
    assert len(next_page) == 1
    assert completed[0]["task_id"] != next_page[0]["task_id"]
    assert len(all_tasks) == 2
    assert all_tasks[0]["updated_at"] >= all_tasks[1]["updated_at"]


def test_task_inventory_pagination_is_stable_for_equal_timestamps(tmp_path):
    database = tmp_path / "events-tied-timestamps.sqlite3"
    _seed_tasks(database)
    store = TaskEventStore(database)
    page_one = store.list_tasks(status="completed", limit=1, offset=0)
    page_two = store.list_tasks(status="completed", limit=1, offset=1)
    assert [page_one[0]["task_id"], page_two[0]["task_id"]] == ["task-a", "task-c"]


def test_task_inventory_rejects_bad_pagination(tmp_path):
    database = tmp_path / "events.sqlite3"
    _seed_tasks(database)
    store = TaskEventStore(database)
    with pytest.raises(ValueError):
        store.list_tasks(limit=0)
    with pytest.raises(ValueError):
        store.list_tasks(offset=-1)
