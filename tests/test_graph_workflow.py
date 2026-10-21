from __future__ import annotations

from dataclasses import replace
import json

from src.config import Settings
from src.graph.workflow import build_workflow, initial_state


def test_e2e_graph_and_conditional_retry_route(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "checkpoints.sqlite3", output_dir=tmp_path / "outputs",
    )
    runtime = build_workflow(settings, qa_scores=[0.45, 0.93])
    task_id = "graph-test-01"
    result = runtime.invoke(
        initial_state(task_id, "Verify the graph can retry QA and produce durable artifacts."),
        config={"configurable": {"thread_id": task_id}},
    )
    assert result["status"] == "completed"
    assert result["retry_count"] == 1
    assert result["qa_feedback"]["confidence"] == 0.93
    expected = {"plan.json", "evidence.json", "execution-notes.md", "report.md", "report.json"}
    folder = settings.output_dir / task_id
    assert expected.issubset({path.name for path in folder.iterdir()})
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "completed"
    assert report["retry_count"] == 1
    snapshot = runtime.get_state({"configurable": {"thread_id": task_id}})
    assert snapshot.values["status"] == "completed"


def test_graph_ends_needs_review_after_retry_budget(tmp_path) -> None:
    settings = replace(
        Settings.from_env(), dry_run_test_mode=True,
        database_path=tmp_path / "review.sqlite3", output_dir=tmp_path / "outputs",
    )
    runtime = build_workflow(settings, qa_scores=[0.2, 0.3, 0.4])
    task_id = "graph-review-01"
    result = runtime.invoke(
        initial_state(task_id, "Verify retry exhaustion ends with a clear needs-review result."),
        config={"configurable": {"thread_id": task_id}},
    )
    assert result["status"] == "needs_review"
    assert result["retry_count"] == 2
    assert result["qa_feedback"]["passed"] is False
    assert (settings.output_dir / task_id / "report.md").is_file()
