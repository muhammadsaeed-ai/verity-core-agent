<p align="center">
  <img src="https://img.shields.io/badge/VERITY--1-Hybrid_Autonomous_Agent-0b132b?style=for-the-badge&labelColor=05070d" alt="Verity-1" />
  <img src="https://img.shields.io/badge/VerityAgent-Open_Source-00d4ff?style=for-the-badge&labelColor=05070d" alt="VerityAgent" />
</p>

<h1 align="center">Verity-1 — Core Agent</h1>
<p align="center"><strong>A checkpointed, schema-validated agent workflow that plans, researches, creates artifacts, and challenges its own output.</strong></p>

<p align="center">
  <a href="https://github.com/muhammadsaeed-ai/verity-core-agent/actions/workflows/ci.yml"><img src="https://github.com/muhammadsaeed-ai/verity-core-agent/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/Pydantic-v2-E92063?logo=pydantic&logoColor=white" alt="Pydantic v2" />
  <img src="https://img.shields.io/badge/License-MIT-24a148" alt="MIT" />
  <img src="https://img.shields.io/badge/LLM-Gemini%20%7C%20Ollama-7c3aed" alt="Gemini and Ollama" />
</p>

Verity-1 is the VerityAgent hybrid autonomous-agent foundation. LangGraph controls workflow state and retries, CrewAI coordinates specialist roles, Browser Use and Playwright gather web evidence, a restricted Python runner handles bounded data work, and FastAPI exposes run/status endpoints for n8n.

This is not an unrestricted self-modifying agent. The default workflow creates traceable deliverables from structured state; arbitrary model-generated Python is never automatically executed.

## Architecture at a glance

<div align="center">
  <table>
    <tr><td align="center"><strong>01 · PLAN</strong><br/>Objective → acceptance criteria</td><td align="center">➜</td><td align="center"><strong>02 · RESEARCH</strong><br/>Browser Use / Playwright / HTTP fallback</td></tr>
    <tr><td align="center"><strong>05 · QA CRITIC</strong><br/>Confidence, issues, retry feedback</td><td align="center">⬅</td><td align="center"><strong>03 · VALIDATE</strong><br/>Pydantic v2 contracts</td></tr>
    <tr><td align="center"><strong>REPORT + JSON</strong><br/>SQLite checkpoints</td><td align="center">⬅</td><td align="center"><strong>04 · SYNTHESIZE</strong><br/>Artifacts + SHA-256 records</td></tr>
  </table>
  <p><strong>QA ≥ 0.80 → finish · QA &lt; 0.80 and retries &lt; 2 → research/execution loop</strong></p>
</div>

## Core capabilities

- **Durable control flow:** LangGraph StateGraph with SQLite SqliteSaver and task-based thread IDs.
- **Typed hand-offs:** Pydantic v2 validates plans, browser evidence, QA feedback, and final reports; unexpected fields are rejected.
- **Specialist team:** Lead Architect & Planner, Deep Web Intelligence Operator, and QA Verifier & Critic are defined as sequential CrewAI tasks.
- **Browser resilience:** Browser Use runs in a dedicated Python environment; Playwright is tried next, with DuckDuckGo HTML HTTP as a final fallback.
- **Bounded Python runner:** AST policy, subprocess timeout, constrained imports/builtins, capped range, JSON I/O, and atomic output writes. This is defence-in-depth, not an OS-grade sandbox for hostile code.
- **HTTP orchestration:** POST /api/v1/agent/run, GET /api/v1/agent/status/{task_id}, and operator-configured n8n completion/failure callbacks.
- **Repeatable verification:** deterministic mode runs without model credentials or live browsing and checks routing, schemas, and artifacts.

## Quick start

Use Python 3.12 for the CI baseline.

<pre><code>git clone https://github.com/muhammadsaeed-ai/verity-core-agent.git
cd verity-core-agent
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
</code></pre>

Copy .env.example to .env. Set GOOGLE_API_KEY to a Google AI Studio key for Gemini, or choose Ollama below.

### Browser Use isolation

Current Browser Use releases and this CrewAI release pin incompatible MCP major versions. To keep both in the project without creating a dependency-resolution conflict, Browser Use runs in a separate environment. This is optional; Playwright and the HTTP fallback work from the main environment.

<pre><code>python -m venv .venv-browser-use
# Windows PowerShell:
# .venv-browser-use\Scripts\python.exe -m pip install -r requirements-browser-use.txt
# macOS/Linux:
.venv-browser-use/bin/python -m pip install -r requirements-browser-use.txt
</code></pre>

Set VERITY_BROWSER_ENGINE=browser-use in .env to try Browser Use before Playwright. The tool detects the dedicated environment automatically; alternatively set VERITY_BROWSER_USE_PYTHON to its Python executable path. Install a Playwright Chromium binary for the Playwright fallback:

<pre><code>python -m playwright install chromium</code></pre>

### Deterministic end-to-end check

<pre><code>python main.py --test-e2e</code></pre>

No model API call or browser is required. The command writes plan.json, evidence.json, execution-notes.md, report.md, and report.json under outputs/{task-id}/ and exits non-zero when required artifacts are missing or QA is not accepted.

### Run a live task

<pre><code>python main.py --task "Research current Python agent orchestration options and produce a sourced comparison"</code></pre>

A configured model is required. External research requires internet access. If no sources are returned, the report states that limitation rather than inventing citations.

### Start the API

<pre><code>python main.py --serve</code></pre>

Open http://127.0.0.1:8000/docs for the API contract.

<pre><code>curl -X POST http://127.0.0.1:8000/api/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"user_objective":"Create a verifiable report for a test workflow objective."}'</code></pre>

Use the returned task_id with GET /api/v1/agent/status/{task_id}. Duplicate task IDs return HTTP 409; unknown task IDs return 404.

## Model providers

### Gemini (default)

<pre><code>VERITY_PROVIDER=gemini
GOOGLE_API_KEY=your_google_ai_studio_key
VERITY_GEMINI_MODEL=gemini-2.5-flash</code></pre>

Free-tier access, rate limits, and data handling depend on Google's current terms. Unlimited free inference is not guaranteed.

### Ollama (local inference)

Install Ollama separately, then retrieve the chosen local model:

<pre><code>ollama pull llama3.2</code></pre>

Configure .env:

<pre><code>VERITY_PROVIDER=ollama
VERITY_OLLAMA_MODEL=llama3.2
VERITY_OLLAMA_BASE_URL=http://localhost:11434
VERITY_RESEARCH_ENABLED=false
N8N_WEBHOOK_URL=</code></pre>

Disabling research and the callback keeps workflow model inference local. Installing packages and retrieving model weights require network access beforehand.

## API contract

| Method | Endpoint | Purpose |
|---|---|---|
| POST | /api/v1/agent/run | Accept a task and return its ID |
| GET | /api/v1/agent/status/{task_id} | Retrieve stored state and QA feedback |
| GET | /health | Liveness check |

The request accepts user_objective or its objective alias, plus an optional task_id. Task IDs allow letters, digits, underscores, and hyphens.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| VERITY_PROVIDER | gemini | gemini or ollama |
| VERITY_GEMINI_MODEL | gemini-2.5-flash | Gemini chat model |
| VERITY_OLLAMA_MODEL | llama3.2 | Local Ollama model |
| DRY_RUN_TEST_MODE | false | Deterministic evidence and QA without external LLM calls |
| VERITY_RESEARCH_ENABLED | true | Enable web research |
| VERITY_BROWSER_ENGINE | playwright | playwright or browser-use first |
| VERITY_BROWSER_USE_PYTHON | auto-detect | Optional isolated Browser Use Python path |
| VERITY_ENABLE_CREWAI | true | Enable live specialist crew |
| VERITY_DATABASE_PATH | data/verity.sqlite3 | SQLite checkpoints and API task records |
| VERITY_OUTPUT_DIR | outputs | Artifact root |
| N8N_WEBHOOK_URL | empty | Trusted completion/failure callback |
| VERITY_API_HOST | 0.0.0.0 | API bind host |
| VERITY_API_PORT | 8000 | API port |

## Verification

<pre><code>pytest -v
python main.py --test-e2e</code></pre>

GitHub Actions runs the unit/integration suite and deterministic smoke test on pushes and pull requests. The suite covers schema rejection, restricted execution, path traversal, retry loops and exhaustion, artifact generation, and API task/status flows.

## Security and reliability boundaries

- QA confidence is an automated signal, not a guarantee of correctness.
- A stored URL confirms retrieval, not that every statement on a page is true.
- Browser navigation requires installed browser dependencies; the HTTP parser is the final fallback.
- The Python runner is not suitable for hostile multi-tenant execution without OS/container isolation and resource limits.
- An n8n webhook is outbound network traffic. Configure only a destination you control.
- Follow each model provider's current usage terms and data policies.

---

**Built for developers by VerityAgent.** Open-source foundation maintained by Mohammed Saeed, founder and owner of VerityAgent.
