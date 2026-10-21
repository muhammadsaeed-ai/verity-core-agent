"""Typed state shared by Verity-1 graph nodes."""

from __future__ import annotations

from typing import Any, TypedDict


class VerityState(TypedDict, total=False):
    task_id: str
    user_objective: str
    plan: dict[str, Any]
    research_data: list[dict[str, Any]]
    research_synthesis: str
    execution_artifacts: list[dict[str, Any]]
    qa_feedback: dict[str, Any]
    retry_count: int
    status: str
    final_report: dict[str, Any]
    created_at: str
    error: str
