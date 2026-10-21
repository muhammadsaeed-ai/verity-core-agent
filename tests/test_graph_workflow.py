from __future__ import annotations

from dataclasses import replace
import json

from src.config import Settings
from src.graph.workflow import _fallback_plan, _qa_node, build_workflow, initial_state
from src.tools.code_sandbox import write_artifact


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


def _qa_state_with_artifacts(settings, task_id, objective):
    plan = _fallback_plan(objective)
    records = [
        write_artifact(task_id, "plan.json", plan.model_dump_json(indent=2), settings.output_dir, "application/json"),
        write_artifact(task_id, "evidence.json", json.dumps({"sources": []}), settings.output_dir, "application/json"),
        write_artifact(task_id, "execution-notes.md", "# Local notes\nNo external sources required.", settings.output_dir),
    ]
    state = initial_state(task_id, objective)
    state["plan"] = plan.model_dump(mode="json")
    state["execution_artifacts"] = records
    state["research_data"] = []
    return state


def test_live_qa_hard_gate_rejects_sourced_research_without_usable_evidence(tmp_path, monkeypatch):
    settings = replace(
        Settings.from_env(), dry_run_test_mode=False, research_enabled=True, enable_crewai=False,
        database_path=tmp_path / "qa-evidence.sqlite3", output_dir=tmp_path / "outputs",
    )
    objective = "Research the latest AI agent market trends and cite sources for a comparison."
    state = _qa_state_with_artifacts(settings, "qa-evidence-01", objective)
    monkeypatch.setattr(
        "src.graph.workflow.ask_model",
        lambda prompt, chosen: json.dumps({
            "confidence": 0.99, "issues": [], "retry_feedback": "",
            "verification_summary": "Model claims the draft is ready.",
        }),
    )

    result = _qa_node(settings, None)(state)

    assert result["status"] == "retrying"
    assert result["qa_feedback"]["passed"] is False
    assert result["qa_feedback"]["confidence"] == 0.79
    assert any("no usable sourced evidence" in issue for issue in result["qa_feedback"]["issues"])


def test_qa_hard_gate_detects_tampered_artifact_even_when_model_passes(tmp_path, monkeypatch):
    settings = replace(
        Settings.from_env(), dry_run_test_mode=False, research_enabled=False, enable_crewai=False,
        database_path=tmp_path / "qa-integrity.sqlite3", output_dir=tmp_path / "outputs",
    )
    objective = "Create a local internal checklist and a structured report."
    state = _qa_state_with_artifacts(settings, "qa-integrity-01", objective)
    (settings.output_dir / "qa-integrity-01" / "execution-notes.md").write_text(
        "tampered after artifact registration", encoding="utf-8"
    )
    monkeypatch.setattr(
        "src.graph.workflow.ask_model",
        lambda prompt, chosen: json.dumps({
            "confidence": 0.99, "issues": [], "retry_feedback": "",
            "verification_summary": "Model claims the draft is ready.",
        }),
    )

    result = _qa_node(settings, None)(state)

    assert result["status"] == "retrying"
    assert result["qa_feedback"]["passed"] is False
    assert result["qa_feedback"]["confidence"] == 0.79
    assert any("Artifact integrity check failed" in issue for issue in result["qa_feedback"]["issues"])
