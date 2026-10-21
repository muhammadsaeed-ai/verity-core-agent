"""Verity-1 CLI entry point and API server runner."""

from __future__ import annotations

import argparse
import json
import os
import sys
from uuid import uuid4

from src.config import Settings
from src.graph.workflow import build_workflow, initial_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Verity-1 hybrid autonomous agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--task", type=str, help="Objective to run through the workflow")
    group.add_argument("--serve", action="store_true", help="Start the FastAPI server")
    group.add_argument("--test-e2e", action="store_true", help="Run deterministic end-to-end verification")
    args = parser.parse_args()

    if args.serve:
        import uvicorn
        uvicorn.run(
            "src.api.server:app", host=os.getenv("VERITY_API_HOST", "0.0.0.0"),
            port=int(os.getenv("VERITY_API_PORT", "8000")), reload=False,
        )
        return 0

    if args.test_e2e:
        os.environ["DRY_RUN_TEST_MODE"] = "true"
        os.environ["VERITY_RESEARCH_ENABLED"] = "false"
        os.environ["VERITY_ENABLE_CREWAI"] = "false"
        objective = "Verify Verity-1 deterministic workflow execution, QA routing, and artifact generation."
    else:
        objective = args.task.strip()
        if len(objective) < 8:
            parser.error("--task must contain at least eight non-whitespace characters.")

    settings = Settings.from_env()
    task_id = "verity-e2e-" + uuid4().hex[:12] if args.test_e2e else uuid4().hex
    runtime = build_workflow(settings)
    result = runtime.invoke(
        initial_state(task_id, objective), config={"configurable": {"thread_id": task_id}},
    )
    print(json.dumps({
        "task_id": task_id, "status": result.get("status"), "retry_count": result.get("retry_count", 0),
        "qa_feedback": result.get("qa_feedback", {}), "artifacts": result.get("execution_artifacts", []),
    }, indent=2, ensure_ascii=False))
    if args.test_e2e:
        required = {"plan.json", "evidence.json", "execution-notes.md", "report.md", "report.json"}
        folder = settings.output_dir / task_id
        missing = sorted(name for name in required if not (folder / name).is_file())
        if missing:
            print("E2E verification failed; missing files: " + ", ".join(missing), file=sys.stderr)
            return 2
        print("E2E verification passed; expected artifacts exist.")
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
