"""Strict Pydantic contracts for requests and inter-agent hand-offs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class TaskRunRequest(StrictModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()), pattern=r"^[A-Za-z0-9_-]{1,80}$")
    user_objective: str = Field(
        validation_alias=AliasChoices("user_objective", "objective"),
        min_length=8,
        max_length=12000,
    )

    @field_validator("user_objective")
    @classmethod
    def objective_must_contain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The objective cannot be empty.")
        return value


class PlanStep(StrictModel):
    step_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=3, max_length=160)
    action: str = Field(min_length=5, max_length=1200)
    verification: str = Field(min_length=5, max_length=600)


class AgentPlan(StrictModel):
    objective: str = Field(min_length=8, max_length=12000)
    steps: list[PlanStep] = Field(min_length=2, max_length=12)
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)


class ResearchItem(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    url: str | None = None
    snippet: str = Field(default="", max_length=10000)
    source: str = Field(default="web-search", max_length=80)
    confidence: float = Field(default=0.5, ge=0, le=1)
    retrieved_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("url")
    @classmethod
    def url_must_be_http_or_local_mock(cls, value: str | None) -> str | None:
        if value is not None and not (
            value.startswith("https://") or value.startswith("http://") or value.startswith("mock://")
        ):
            raise ValueError("Research URLs must use http(s), or mock:// in deterministic tests.")
        return value


class QAFeedback(StrictModel):
    confidence: float = Field(ge=0, le=1)
    passed: bool
    issues: list[str] = Field(default_factory=list, max_length=30)
    retry_feedback: str = Field(default="", max_length=3000)
    verification_summary: str = Field(min_length=3, max_length=2000)


class ArtifactRecord(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(default="text/markdown", max_length=120)


class AgentReport(StrictModel):
    task_id: str
    objective: str
    status: Literal["completed", "needs_review", "failed"]
    plan: AgentPlan
    research: list[ResearchItem]
    artifacts: list[ArtifactRecord]
    qa_feedback: QAFeedback
    retry_count: int = Field(ge=0, le=2)
    provider: str
    model: str
    started_at: str
    finished_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    summary: str = Field(min_length=3, max_length=3000)

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
