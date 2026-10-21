"""CrewAI planner, research operator, and QA specialist."""

from __future__ import annotations

import os
from typing import Any

from src.config import Settings
from src.tools.browser_tool import BrowserUseTool


def run_specialist_crew(objective: str, plan: dict[str, Any], evidence: list[dict[str, Any]],
                        feedback: str, settings: Settings) -> str:
    if settings.dry_run_test_mode:
        return "Deterministic test mode: specialist roles were simulated; no external model or browser was called."
    if not settings.enable_crewai:
        return "CrewAI specialists are disabled by configuration."
    try:
        from crewai import Agent, Crew, Process, Task
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt to run the CrewAI specialist team.") from exc
    if settings.provider == "gemini":
        if not settings.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is required for Gemini/CrewAI. Set VERITY_PROVIDER=ollama for local inference.")
        os.environ.setdefault("GEMINI_API_KEY", settings.google_api_key)
        llm_name = f"gemini/{settings.gemini_model}"
    else:
        llm_name = f"ollama/{settings.ollama_model}"
    tool = BrowserUseTool(settings=settings)
    planner = Agent(
        role="Lead Architect & Planner",
        goal="Translate the objective into testable requirements and low-risk execution steps.",
        backstory="A principal systems architect who distinguishes requirements from assumptions.",
        llm=llm_name, verbose=False, allow_delegation=False,
    )
    researcher = Agent(
        role="Deep Web Intelligence Operator",
        goal="Evaluate supplied evidence and collect more only when research is enabled.",
        backstory="A careful researcher who cites observed URLs and never fabricates sources.",
        llm=llm_name, tools=[tool] if settings.research_enabled else [],
        verbose=False, allow_delegation=False,
    )
    verifier = Agent(
        role="QA Verifier & Critic",
        goal="Identify unsupported claims, missing acceptance criteria, and concrete verification gaps.",
        backstory="An adversarial reviewer who prefers an explicit limitation over an invented fact.",
        llm=llm_name, verbose=False, allow_delegation=False,
    )
    planner_task = Task(
        description="Review objective and validated plan; list material risks and measurable checks.\nObjective: "
        + objective + "\nPlan: " + str(plan),
        expected_output="A concise risk and acceptance-check memo.",
        agent=planner,
    )
    research_task = Task(
        description=(
            "Review evidence relevance and provenance. Use the browser tool only when research is enabled. "
            "Never create a URL. Research enabled: " + str(settings.research_enabled)
            + "\nObjective: " + objective + "\nEvidence: " + str(evidence)
            + "\nQA feedback: " + feedback
        ),
        expected_output="A source-quality memo; include URLs only if observed.",
        agent=researcher,
    )
    verifier_task = Task(
        description=(
            "Audit prior memos against the objective. State uncertainties and the next useful check. "
            "Do not claim code or tests were run unless the supplied evidence says so. Objective: " + objective
        ),
        expected_output="An evidence-grounded critic memo with risks and checks.",
        agent=verifier, context=[planner_task, research_task],
    )
    result = Crew(
        agents=[planner, researcher, verifier], tasks=[planner_task, research_task, verifier_task],
        process=Process.sequential, verbose=False,
    ).kickoff()
    return str(getattr(result, "raw", result))[:12000]
