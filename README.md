<p align="center">
  <img src="https://img.shields.io/badge/VERITY--1-Hybrid_Autonomous_Agent-0b132b?style=for-the-badge&labelColor=05070d" alt="Verity-1" />
  <img src="https://img.shields.io/badge/VerityAgent-Open_Source-00d4ff?style=for-the-badge&labelColor=05070d" alt="VerityAgent" />
</p>

<h1 align="center">Verity-1 — Core Agent</h1>
<p align="center"><strong>A checkpointed, schema-validated agent workflow that plans, researches, generates artifacts, and challenges its own output.</strong></p>

<p align="center">
  <a href="https://github.com/muhammadsaeed-ai/verity-core-agent/actions/workflows/ci.yml"><img src="https://github.com/muhammadsaeed-ai/verity-core-agent/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/Pydantic-v2-E92063?logo=pydantic&logoColor=white" alt="Pydantic v2" />
  <img src="https://img.shields.io/badge/License-MIT-24a148" alt="MIT" />
  <img src="https://img.shields.io/badge/LLM-Gemini%20%7C%20Ollama-7c3aed" alt="Gemini and Ollama" />
</p>

Verity-1 is the VerityAgent hybrid autonomous-agent foundation: LangGraph controls state and retries, CrewAI coordinates specialist roles, Browser Use / Playwright gather web evidence, a restricted Python runner handles bounded data work, and FastAPI exposes run/status endpoints for n8n.

This is not an unrestricted self-modifying agent. The default workflow creates traceable deliverables from structured state; arbitrary model-generated Python is never automatically executed.

## Architecture at a glance

<div align="center">
  <table>
    <tr><td align="center"><strong>01 · PLAN</strong><br/>Objective → acceptance criteria</td><td align="center">➜</td><td align="center"><strong>02 · RESEARCH</strong><br/>Browser Use / Playwright / HTTP fallback</td></tr>
    <tr><td align="center"><strong>05 · QA CRITIC</strong><br/>Confidence, issues, retry feedback</td><td align="center">⬅</td><td align="center"><strong>03 · VALIDATE</strong><br/>Pydantic v2 contracts</td></tr>
    <tr><td align="center"><strong>REPORT + JSON</strong><br/>SQLite thread checkpoint</td><td align="center">⬅</td><td align="center"><strong>04 · SYNTHESIZE</strong><br/>Files + SHA-256 manifests</td></tr>
  </table>
  <p><strong>QA ≥ 0.80 → finish · QA &lt; 0.80 and retries &lt; 2 → research/execution loop</strong></p>
</div>

<details>
<summary><strong>Inspect the execution contract</strong></summary>

| Stage | Input | Output / control |
|---|---|---|
| Planner | Objective and task ID | AgentPlan with verifiable steps |
| Research | Plan and QA retry feedback | ResearchItem records with observed URLs where available |
| Code & files | Validated plan and evidence | Markdown, JSON, and SHA-256 artifact records |
| QA critic | Objective, criteria, artifact hashes | QAFeedback with confidence, issues, and retry directive |
| Router | QA confidence and retry count | End or retry research/execution; maximum two retries |

</details>

## Implemented capabilities

- **Durable control flow:** LangGraph StateGraph with SQLite SqliteSaver; graph thread IDs map to task IDs for checkpoint-state retrieval.
- **Typed agent hand-offs:** Pydantic v2 contracts reject unknown fields in plans, evidence records, QA feedback, and final reports.
- **Three specialist roles:** Lead Architect & Planner, Deep Web Intelligence Operator, and QA Verifier & Critic are defined as sequential CrewAI tasks.
- **Browser resilience:** Browser Use can be selected first, then Playwright, then a DuckDuckGo HTML parser. Missing Chromium does not silently become fake evidence.
- **Bounded Python runner:** AST checks, subprocess timeout, constrained imports/builtins, capped range, JSON input/output, and atomic artifact writing. This is defence-in-depth, not an OS-grade sandbox for hostile code.
- **HTTP orchestration:** POST /api/v1/agent/run, GET /api/v1/agent/status/{task_id}, and configured N8N_WEBHOOK_URL completion/failure callbacks.
- **Repeatable verification:** mock mode covers workflow routing and artifacts without a model key or browser.

## Quick start

### 1. Install

Python 3.12 is the CI-tested baseline.

<pre><code>git clone https://github.com/muhammadsaeed-ai/verity-core-agent.git
cd verity-core-agent
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
</code></pre>

Copy .env.example to .env and place your Google AI Studio key in GOOGLE_API_KEY for Gemini. If you plan to use Browser Use mode, install a compatible browser:

<pre><code>python -m playwright install chromium
</code></pre>

### 2. Run a deterministic end-to-end smoke test

<pre><code>python main.py --test-e2e</code></pre>

This path makes no model API calls and does not require Chromium. It writes plan.json, evidence.json, execution-notes.md, report.md, and report.json under outputs/{task-id}/.

### 3. Run a real task

<pre><code>python main.py --task "Research current Python agent orchestration options and produce a cited comparison"</code></pre>

A configured LLM is required. External research requires an internet connection. If no real sources are returned, the report says so instead of inventing citations.

### 4. Start the API

<pre><code>python main.py --serve</code></pre>

Open http://127.0.0.1:8000/docs to inspect the API contract.

<pre><code>curl -X POST http://127.0.0.1:8000/api/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"user_objective":"Create a verifiable report for a test workflow objective."}'</code></pre>

Use the returned task_id with:

<pre><code>curl http://127.0.0.1:8000/api/v1/agent/status/&lt;task_id&gt;</code></pre>

For n8n callbacks, configure N8N_WEBHOOK_URL in .env. The service sends a JSON completion/failure event to this operator-controlled endpoint; callers cannot supply arbitrary callback URLs.

## Provider modes

### Gemini (default)

<pre><code>VERITY_PROVIDER=gemini
GOOGLE_API_KEY=your_google_ai_studio_key
VERITY_GEMINI_MODEL=gemini-2.5-flash</code></pre>

Gemini API free-tier availability, quotas, and data handling depend on Google's current terms. Unlimited free inference is not guaranteed.

### Ollama (local model)

Install Ollama separately and pull the model:

<pre><code>ollama pull llama3.2</code></pre>

Configure:

<pre><code>VERITY_PROVIDER=ollama
VERITY_OLLAMA_MODEL=llama3.2
VERITY_OLLAMA_BASE_URL=http://localhost:11434</code></pre>

For fully local model inference, disable web research and the n8n callback:

<pre><code>VERITY_PROVIDER=ollama
VERITY_RESEARCH_ENABLED=false
N8N_WEBHOOK_URL=</code></pre>

This disables online browsing; planning, file synthesis, checkpoints, and QA can use the local model. Installing dependencies and downloading model weights require network access beforehand.

## API contract

| Method | Endpoint | Purpose |
|---|---|---|
| POST | /api/v1/agent/run | Queue a task and return its ID |
| GET | /api/v1/agent/status/{task_id} | Retrieve current/final state and QA feedback |
| GET | /health | Liveness check |

The run body accepts user_objective or its objective alias, plus an optional task_id. IDs allow letters, digits, underscores, and hyphens. Duplicate IDs return HTTP 409; unknown tasks return 404.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| VERITY_PROVIDER | gemini | gemini or ollama |
| VERITY_GEMINI_MODEL | gemini-2.5-flash | Gemini model |
| VERITY_OLLAMA_MODEL | llama3.2 | Local Ollama model |
| DRY_RUN_TEST_MODE | false | Disable external inference and use deterministic test evidence |
| VERITY_RESEARCH_ENABLED | true | Browser/search research |
| VERITY_BROWSER_ENGINE | playwright | playwright or browser-use first |
| VERITY_ENABLE_CREWAI | true | Live specialist team |
| VERITY_DATABASE_PATH | data/verity.sqlite3 | SQLite checkpoints and task records |
| VERITY_OUTPUT_DIR | outputs | Artifact root |
| N8N_WEBHOOK_URL | empty | Trusted completion/failure callback |
| VERITY_API_HOST | 0.0.0.0 | API bind host |
| VERITY_API_PORT | 8000 | API port |

## Verification

Run the complete test suite:

<pre><code>pytest -v</code></pre>

Run the deterministic end-to-end smoke test:

<pre><code>DRY_RUN_TEST_MODE=true VERITY_RESEARCH_ENABLED=false VERITY_ENABLE_CREWAI=false python main.py --test-e2e</code></pre>

Tests cover strict schema validation, safe-runner restrictions, path traversal, retry loops and retry exhaustion, artifact creation, and FastAPI task/status behavior. GitHub Actions executes pytest and the smoke test on pushes and pull requests.

## Repository structure

<pre><code>src/
├── config.py                 # Gemini / Ollama factory and settings
├── schemas.py                # Strict Pydantic v2 hand-off contracts
├── tools/
│   ├── browser_tool.py       # Browser Use + Playwright + HTTP fallback
│   └── code_sandbox.py       # Restricted Python runner + artifact writer
├── crew/
│   └── specialist_crew.py    # Planner, researcher, QA specialist roles
├── graph/
│   ├── state.py              # Typed workflow state
│   └── workflow.py           # Graph nodes, routing, SQLite checkpoints
└── api/
    └── server.py             # FastAPI + n8n gateway
main.py                       # CLI and server runner
tests/                        # Unit, retry, API, integration verification
</code></pre>

## Reliability and security boundaries

- QA confidence is an automated signal, not proof of correctness; inspect source pages for high-impact decisions.
- A source URL records that a result was retrieved; it is not proof that every related claim is true.
- Browser navigation requires compatible browser dependencies. If Chromium is unavailable, an HTTP fallback is attempted.
- The restricted Python runner is not designed for hostile multi-tenant execution. Add OS/container isolation and resource limits before exposing arbitrary execution to untrusted users.
- An n8n webhook is outbound network traffic. Configure only an endpoint you control.
- Follow each model provider's current usage terms and data policies.

---

**Built for developers by VerityAgent.** Open-source foundation maintained by Mohammed Saeed, founder and owner of VerityAgent.
