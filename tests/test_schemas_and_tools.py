from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.schemas import AgentPlan, PlanStep, ResearchItem
from src.tools.code_sandbox import execute_python, validate_python_source, write_artifact


def test_research_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ResearchItem(title="Evidence", url="https://example.com", invented=True)


def test_plan_schema_requires_multiple_verifiable_steps() -> None:
    with pytest.raises(ValidationError):
        AgentPlan(
            objective="A sufficiently long objective",
            steps=[{"step_id": "S1", "title": "Only step", "action": "Do one thing", "verification": "Check output"}],
            acceptance_criteria=["It should work"],
        )


def test_sandbox_metrics_and_json_artifact(tmp_path) -> None:
    result = execute_python(
        "import statistics, json\nresult = {\"average\": statistics.mean(data), \"count\": len(data)}",
        [2, 4, 6], task_id="unit-test", output_name="metrics.json", output_dir=tmp_path,
    )
    assert result["result"] == {"average": 4, "count": 3}
    path = tmp_path / "unit-test" / "metrics.json"
    assert json.loads(path.read_text(encoding="utf-8"))["result"]["count"] == 3


def test_sandbox_rejects_unsafe_imports_and_dunder_access() -> None:
    with pytest.raises(ValueError):
        validate_python_source("import os\nresult = os.listdir('.')")
    with pytest.raises(ValueError):
        validate_python_source("result = data.__class__")


def test_artifact_writer_prevents_path_traversal(tmp_path) -> None:
    with pytest.raises(ValueError):
        write_artifact("../outside", "report.md", "no", tmp_path)
    with pytest.raises(ValueError):
        write_artifact("safe-id", "../report.md", "no", tmp_path)


def test_plan_step_contract() -> None:
    step = PlanStep(
        step_id="S1", title="Collect evidence", action="Collect evidence with source URLs.",
        verification="Validate that every returned URL is actually present.",
    )
    assert step.step_id == "S1"
