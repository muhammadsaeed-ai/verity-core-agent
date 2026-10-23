"""LangGraph workflow with SQLite checkpoints and a dependency-light test fallback."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

from src.config import Settings, ask_model
from src.api.integrity import verify_artifact_records
from src.schemas import AgentPlan, AgentReport, ArtifactRecord, PlanStep, QAFeedback, ResearchItem
from src.tools.browser_tool import BrowserUseTool
from src.tools.code_sandbox import write_artifact
from src.graph.state import VerityState

logger = logging.getLogger(__name__)


def _requires_external_evidence(objective: str, plan: AgentPlan) -> bool:
    """Identify objectives that explicitly require timely or sourced information."""
    terms = (
        "research", "current", "latest", "today", "yesterday", "this week", "this month",
        "source", "citation", "cite", "market", "competitor", "pricing", "statistics",
        "compare", "comparison", "benchmark", "fact check", "news", "legal",
        "regulation", "policy", "release notes", "documentation",
    )
    text = (objective + " " + " ".join(plan.acceptance_criteria)).casefold()
    return any(term in text for term in terms)

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph
    LANGGRAPH_AVAILABLE = True
except ImportError:
    SqliteSaver = None
    StateGraph = None
    START = "__start__"
    END = "__end__"
    LANGGRAPH_AVAILABLE = False


def _parse_json_object(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Model output did not contain a JSON object.")
    result = json.loads(text[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError("Model JSON must be an object.")
    return result


def _fallback_plan(objective: str) -> AgentPlan:
    return AgentPlan(
        objective=objective,
        steps=[
            PlanStep(step_id="S1", title="Translate the objective", action="Extract explicit requirements, constraints, and expected outputs.", verification="Every major requirement appears in the acceptance checklist."),
            PlanStep(step_id="S2", title="Gather verifiable evidence", action="Use configured research when enabled and keep source URLs separate from summaries.", verification="Every external evidence item has an observed HTTP(S) source URL."),
            PlanStep(step_id="S3", title="Execute the bounded workflow", action="Generate artifacts from validated plan and evidence records.", verification="Output files exist and JSON conforms to the declared schemas."),
            PlanStep(step_id="S4", title="Critique and finalize", action="Evaluate evidence coverage, artifact integrity, and remaining uncertainty.", verification="QA confidence is recorded and retries stop at a maximum of two."),
        ],
        assumptions=["The objective may require credentials or external services not supplied at runtime."],
        acceptance_criteria=[
            "All hand-offs validate against Pydantic v2 schemas.",
            "Generated files stay under the configured output directory.",
            "The QA critic records confidence, issues, and a concrete retry instruction.",
        ],
    )


def _build_plan(objective: str, settings: Settings) -> AgentPlan:
    if settings.dry_run_test_mode:
        return _fallback_plan(objective)
    prompt = (
        "Return only a JSON object matching this shape: "
        '{"objective":"...","steps":[{"step_id":"S1","title":"...","action":"...","verification":"..."}],'
        '"assumptions":["..."],"acceptance_criteria":["..."]}. '
        "Use 3 to 7 verifiable steps. Do not claim work has been done. Objective: " + objective
    )
    try:
        return AgentPlan.model_validate(_parse_json_object(ask_model(prompt, settings)))
    except Exception as exc:
        logger.warning("Planner output failed schema parsing; using deterministic decomposition: %s", exc)
        return _fallback_plan(objective)


def _planner_node(settings: Settings) -> Callable[[VerityState], dict[str, Any]]:
    def node(state: VerityState) -> dict[str, Any]:
        return {"plan": _build_plan(state["user_objective"], settings).model_dump(mode="json"), "status": "planned"}
    return node


def _research_node(settings: Settings) -> Callable[[VerityState], dict[str, Any]]:
    def node(state: VerityState) -> dict[str, Any]:
        current = [ResearchItem.model_validate(item) for item in state.get("research_data", [])]
        feedback = state.get("qa_feedback", {}).get("retry_feedback", "")
        synthesis = ""
        if settings.dry_run_test_mode:
            fresh = [ResearchItem(
                title="Deterministic synthetic evidence", url="mock://verity-test/evidence",
                snippet="This record exists only in DRY_RUN_TEST_MODE and is not a real-world citation.",
                source="mock", confidence=1.0,
            )]
            synthesis = "Mock research is deterministic and does not contact external services."
        elif not settings.research_enabled:
            fresh = []
            synthesis = "Live research is disabled. No web claims were generated."
        else:
            query = state["user_objective"] + ((" " + feedback[:240]) if feedback else "")
            result = BrowserUseTool(settings=settings).search(query)
            fresh = [
                ResearchItem(
                    title=item["title"], url=item["url"], snippet=item.get("snippet", ""),
                    source=str(result.get("method", "web-search")),
                    confidence=0.7 if item.get("snippet") else 0.55,
                    retrieved_at=str(result.get("retrieved_at", datetime.now(timezone.utc).isoformat())),
                )
                for item in result.get("results", [])
            ]
            if settings.enable_crewai:
                try:
                    from src.crew.specialist_crew import run_specialist_crew
                    synthesis = run_specialist_crew(
                        state["user_objective"], state.get("plan", {}),
                        [item.model_dump(mode="json") for item in fresh], feedback, settings,
                    )
                except Exception as exc:
                    logger.warning("CrewAI unavailable; retaining raw source records: %s", exc)
                    synthesis = "CrewAI specialist stage unavailable: " + type(exc).__name__
            else:
                synthesis = "CrewAI specialist stage disabled by configuration."
        unique: dict[str, ResearchItem] = {}
        for item in current + fresh:
            unique.setdefault(item.url or (item.source + ":" + item.title), item)
        return {
            "research_data": [item.model_dump(mode="json") for item in unique.values()],
            "research_synthesis": synthesis[:12000], "status": "researching",
        }
    return node


def _code_node(settings: Settings) -> Callable[[VerityState], dict[str, Any]]:
    def node(state: VerityState) -> dict[str, Any]:
        task_id = state["task_id"]
        plan = AgentPlan.model_validate(state["plan"])
        evidence = [ResearchItem.model_validate(item) for item in state.get("research_data", [])]
        plan_record = write_artifact(task_id, "plan.json", plan.model_dump_json(indent=2), settings.output_dir, "application/json")
        evidence_record = write_artifact(
            task_id, "evidence.json",
            json.dumps({"task_id": task_id, "sources": [item.model_dump(mode="json") for item in evidence]}, ensure_ascii=False, indent=2),
            settings.output_dir, "application/json",
        )
        lines = ["# Verity-1 execution notes", "", "## Objective", state["user_objective"], "", "## Plan"]
        for step in plan.steps:
            lines.extend([f"{step.step_id}. **{step.title}**", step.action, f"Verification: {step.verification}", ""])
        lines.extend(["## Evidence status", ""])
        if evidence:
            for index, item in enumerate(evidence, start=1):
                lines.extend([
                    f"{index}. **{item.title}**", item.snippet or "No source snippet was returned.",
                    f"Source: {item.url}" if item.url else "Source URL unavailable", "",
                ])
        else:
            lines.append("No external sources were collected. Claims requiring web evidence remain unverified.")
        lines.extend([
            "## Specialist synthesis",
            state.get("research_synthesis", "No specialist synthesis was supplied.") or "No specialist synthesis was supplied.",
            "",
            "## Scope note",
            "Retrieved source records are separate from generated summaries. A URL records retrieval, not proof that a claim is true.",
            "",
        ])
        notes_record = write_artifact(task_id, "execution-notes.md", "\n".join(lines), settings.output_dir)
        artifacts = {item["path"]: item for item in state.get("execution_artifacts", [])}
        for item in (plan_record, evidence_record, notes_record):
            artifacts[item["path"]] = item
        return {"execution_artifacts": list(artifacts.values()), "status": "executed"}
    return node


def _render_report(report: AgentReport) -> str:
    lines = [
        "# Verity-1 task report", "",
        f"**Task ID:** {report.task_id}  ", f"**Status:** {report.status}  ",
        f"**Provider:** {report.provider} / {report.model}  ",
        f"**QA confidence:** {report.qa_feedback.confidence:.2f}  ",
        f"**Retries:** {report.retry_count}", "",
        "## Objective", report.objective, "", "## Execution plan",
    ]
    for step in report.plan.steps:
        lines.extend([f"### {step.step_id} — {step.title}", step.action, f"**Verification:** {step.verification}", ""])
    lines.extend(["## Retrieved evidence", ""])
    if report.research:
        for index, item in enumerate(report.research, start=1):
            lines.extend([
                f"{index}. **{item.title}**", item.snippet or "No source snippet supplied.",
                f"Source: {item.url}" if item.url else "Source URL unavailable.", "",
            ])
    else:
        lines.append("No external sources were retrieved. Treat time-sensitive claims as unverified.")
    lines.extend([
        "## QA review", report.qa_feedback.verification_summary, "",
        f"**Result:** {'PASS' if report.qa_feedback.passed else 'NEEDS REVIEW'}", "",
    ])
    if report.qa_feedback.issues:
        lines.append("### Issues")
        lines.extend([f"- {item}" for item in report.qa_feedback.issues])
        lines.append("")
    lines.extend(["## Artifacts", ""])
    lines.extend([f"- {item.path} — SHA-256 {item.sha256} — {item.size_bytes} bytes" for item in report.artifacts])
    lines.extend([
        "", "## Reliability note",
        "QA confidence is automated assessment, not a correctness guarantee. Inspect original sources for high-impact decisions.", "",
    ])
    return "\n".join(lines)


def _qa_node(settings: Settings, qa_scores: list[float] | None) -> Callable[[VerityState], dict[str, Any]]:
    score_index = {"value": 0}
    scores = list(qa_scores or [])

    def node(state: VerityState) -> dict[str, Any]:
        plan = AgentPlan.model_validate(state["plan"])
        evidence = [ResearchItem.model_validate(item) for item in state.get("research_data", [])]
        artifacts = [ArtifactRecord.model_validate(item) for item in state.get("execution_artifacts", [])]
        if scores:
            index = min(score_index["value"], len(scores) - 1)
            confidence = float(scores[index])
            score_index["value"] += 1
            verification = "Confidence supplied by deterministic workflow test injection."
            issues = [] if confidence >= 0.8 else ["Injected low-confidence score requires a retry."]
            retry_feedback = "Collect relevant evidence and ensure each acceptance criterion appears in the execution notes."
        elif settings.dry_run_test_mode:
            confidence = 0.98 if plan.steps and artifacts else 0.45
            verification = "Deterministic QA checked plan schema, artifact records, and test-mode execution."
            issues = [] if confidence >= 0.8 else ["The plan or generated artifact set is incomplete."]
            retry_feedback = "Complete missing plan or artifact checks before the next QA pass."
        else:
            prompt = (
                "You are Verity-1's QA critic. Return only JSON with keys confidence (0..1), issues (string array), "
                "retry_feedback (string), verification_summary (string). Score workflow completeness and criteria, not "
                "whether unverified claims are true. If current facts require web evidence and none exists, confidence "
                "must be below 0.80. Do not invent facts. Objective: " + state["user_objective"]
                + "\nAcceptance criteria: " + json.dumps(plan.acceptance_criteria)
                + "\nEvidence URLs: " + json.dumps([item.url for item in evidence])
                + "\nArtifacts: " + json.dumps([item.model_dump(mode="json") for item in artifacts])
                + "\nCurrent retry count: " + str(state.get("retry_count", 0))
            )
            try:
                candidate = _parse_json_object(ask_model(prompt, settings))
                candidate["passed"] = float(candidate["confidence"]) >= 0.8
                parsed = QAFeedback.model_validate(candidate)
                confidence, verification = parsed.confidence, parsed.verification_summary
                issues, retry_feedback = parsed.issues, parsed.retry_feedback
            except Exception as exc:
                confidence = 0.4
                verification = "Critic response was missing or failed strict schema validation."
                issues = ["Critic response could not be validated: " + type(exc).__name__]
                retry_feedback = "Return valid JSON with confidence, issues, retry_feedback, and verification_summary."
        # Apply deterministic preconditions independently of the model's self-reported score.
        hard_gate_issues: list[str] = []
        if not settings.dry_run_test_mode and _requires_external_evidence(state["user_objective"], plan):
            has_citable_evidence = any(
                item.url is not None
                and item.url.startswith(("http://", "https://"))
                and bool(item.snippet.strip())
                for item in evidence
            )
            if not has_citable_evidence:
                hard_gate_issues.append(
                    "This objective calls for external research or citations, but no usable sourced evidence was collected."
                )

        integrity = verify_artifact_records(
            settings.output_dir,
            state["task_id"],
            [item.model_dump(mode="json") for item in artifacts],
        )
        if not integrity["integrity_verified"]:
            hard_gate_issues.append(
                f"Artifact integrity check failed for {integrity['failed_count']} of {integrity['checked_count']} recorded artifacts."
            )

        if hard_gate_issues:
            confidence = min(float(confidence), 0.79)
            issues = list(issues) + hard_gate_issues
            retry_feedback = (
                "Resolve the deterministic QA blockers: collect usable public-source evidence where required, "
                "and regenerate or repair every artifact that fails its SHA-256/size verification. "
                + (retry_feedback or "")
            ).strip()
            verification = (
                (verification.rstrip() + " ") if verification else ""
            ) + "Deterministic evidence and artifact-integrity gates were applied independently of the model score."

        confidence = max(0.0, min(1.0, confidence))
        passed = confidence >= 0.8
        retry_count = int(state.get("retry_count", 0))
        final_attempt = passed or retry_count >= 2
        feedback = QAFeedback(
            confidence=confidence, passed=passed, issues=issues,
            retry_feedback=retry_feedback, verification_summary=verification,
        )
        if not final_attempt:
            return {
                "qa_feedback": feedback.model_dump(mode="json"),
                "retry_count": retry_count + 1, "status": "retrying",
            }
        status = "completed" if passed else "needs_review"
        report = AgentReport(
            task_id=state["task_id"], objective=state["user_objective"], status=status,
            plan=plan, research=evidence, artifacts=artifacts, qa_feedback=feedback,
            retry_count=retry_count, provider=settings.provider,
            model=settings.gemini_model if settings.provider == "gemini" else settings.ollama_model,
            started_at=state.get("created_at", datetime.now(timezone.utc).isoformat()),
            summary=("QA accepted the generated workflow artifacts." if passed
                     else "QA retries were exhausted; inspect issues and artifacts before relying on this output."),
        )
        md_record = write_artifact(
            state["task_id"], "report.md", _render_report(report), settings.output_dir, "text/markdown"
        )
        by_path = {item.path: item for item in artifacts}
        by_path[md_record["path"]] = ArtifactRecord.model_validate(md_record)
        report = report.model_copy(update={"artifacts": list(by_path.values())})
        json_record = write_artifact(
            state["task_id"], "report.json", report.model_dump_json(indent=2),
            settings.output_dir, "application/json",
        )
        artifact_dicts = [item.model_dump(mode="json") for item in by_path.values()]
        artifact_dicts.append(json_record)
        return {
            "qa_feedback": feedback.model_dump(mode="json"), "retry_count": retry_count,
            "status": status, "execution_artifacts": artifact_dicts,
            "final_report": report.model_dump(mode="json"),
        }
    return node


class WorkflowRuntime:
    """Thread-safe facade over LangGraph and a deterministic dependency-light fallback."""

    def __init__(self, settings: Settings, connection: sqlite3.Connection, graph: Any = None,
                 nodes: dict[str, Callable[[VerityState], dict[str, Any]]] | None = None):
        self.settings = settings
        self.connection = connection
        self.graph = graph
        self.nodes = nodes or {}
        self.lock = threading.RLock()
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS verity_runtime_snapshots (task_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.connection.commit()

    @staticmethod
    def _thread_id(config: dict[str, Any] | None, state: dict[str, Any] | None = None) -> str:
        return str(((config or {}).get("configurable") or {}).get("thread_id") or (state or {}).get("task_id") or "default")

    def _save_fallback(self, task_id: str, state: VerityState) -> None:
        self.connection.execute(
            "INSERT INTO verity_runtime_snapshots(task_id, state_json, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            (task_id, json.dumps(state, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()

    def invoke(self, initial_state: VerityState, config: dict[str, Any] | None = None) -> VerityState:
        with self.lock:
            if self.graph is not None:
                cfg = config or {"configurable": {"thread_id": initial_state["task_id"]}}
                return self.graph.invoke(initial_state, config=cfg)
            state: VerityState = dict(initial_state)
            task_id = self._thread_id(config, state)
            for name in ("planner", "research", "execution"):
                state.update(self.nodes[name](state))
                self._save_fallback(task_id, state)
            while True:
                state.update(self.nodes["qa"](state))
                self._save_fallback(task_id, state)
                if state.get("status") in {"completed", "needs_review"}:
                    return state
                state.update(self.nodes["research"](state))
                self._save_fallback(task_id, state)
                state.update(self.nodes["execution"](state))
                self._save_fallback(task_id, state)

    def get_state(self, config: dict[str, Any]) -> Any:
        with self.lock:
            if self.graph is not None:
                return self.graph.get_state(config)
            task_id = self._thread_id(config)
            row = self.connection.execute(
                "SELECT state_json FROM verity_runtime_snapshots WHERE task_id=?", (task_id,)
            ).fetchone()
            return SimpleNamespace(values={} if row is None else json.loads(row[0]))


def build_workflow(settings: Settings | None = None, *, qa_scores: list[float] | None = None,
                   force_fallback: bool = False) -> WorkflowRuntime:
    chosen = settings or Settings.from_env()
    chosen.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(chosen.database_path), check_same_thread=False, timeout=30)
    # Enable concurrent readers while API stores write task status and audit events.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    nodes: dict[str, Callable[[VerityState], dict[str, Any]]] = {
        "planner": _planner_node(chosen), "research": _research_node(chosen),
        "execution": _code_node(chosen), "qa": _qa_node(chosen, qa_scores),
    }
    if LANGGRAPH_AVAILABLE and not force_fallback:
        builder = StateGraph(VerityState)
        builder.add_node("planner", nodes["planner"])
        builder.add_node("research_execution", nodes["research"])
        builder.add_node("code_execution", nodes["execution"])
        builder.add_node("qa_critic", nodes["qa"])
        builder.add_edge(START, "planner")
        builder.add_edge("planner", "research_execution")
        builder.add_edge("research_execution", "code_execution")
        builder.add_edge("code_execution", "qa_critic")

        def route_after_qa(state: VerityState) -> str:
            return END if state.get("status") in {"completed", "needs_review"} else "research_execution"

        builder.add_conditional_edges(
            "qa_critic", route_after_qa, {"research_execution": "research_execution", END: END}
        )
        # Initialize checkpoint tables during startup, not on the first live task.
        # The first lazy schema write previously raced with TaskStore writes under load.
        checkpointer = SqliteSaver(connection)
        checkpointer.setup()
        graph = builder.compile(checkpointer=checkpointer)
        return WorkflowRuntime(chosen, connection, graph=graph, nodes=nodes)
    logger.warning("LangGraph unavailable or fallback forced; using dependency-light state runner.")
    return WorkflowRuntime(chosen, connection, graph=None, nodes=nodes)


def initial_state(task_id: str, objective: str) -> VerityState:
    return {
        "task_id": task_id, "user_objective": objective,
        "research_data": [], "execution_artifacts": [], "qa_feedback": {},
        "retry_count": 0, "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
