<div align="center">

<img src="docs/assets/verity-hero.svg" alt="Verity-1 — autonomy with checkpoints, not blind execution" width="100%">

<p>
<a href="https://github.com/muhammadsaeed-ai/verity-core-agent/actions"><img src="https://img.shields.io/badge/CI-10%2F10%20tests%20passed-35D6A5?style=for-the-badge&logo=githubactions&logoColor=07131b" alt="10 tests passed in verified CI run"></a>
<img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12">
<img src="https://img.shields.io/badge/Pydantic-v2-E92063?style=for-the-badge&logo=pydantic&logoColor=white" alt="Pydantic v2">
<img src="https://img.shields.io/badge/Models-Gemini%20%7C%20Ollama-8B7CF6?style=for-the-badge" alt="Gemini and Ollama">
<img src="https://img.shields.io/badge/License-MIT-91A9C0?style=for-the-badge" alt="MIT license">
</p>

### A hybrid autonomous workflow engine by VerityAgent

**Plan → Research → Validate → Synthesize → Critique → Route**

[Interactive Control Room](https://muhammadsaeed-ai.github.io/verity-core-agent/) · [Source Code](https://github.com/muhammadsaeed-ai/verity-core-agent) · [CI Runs](https://github.com/muhammadsaeed-ai/verity-core-agent/actions) · [API Docs (local)](#api-contract)

</div>

Verity-1 is built around a precise idea: autonomous workflows should not hide state, silently invent evidence, or treat every run as a success. It combines **LangGraph** state control and SQLite checkpoints, **CrewAI** specialist roles, **Pydantic v2** contracts, browser/search tools, artifact synthesis, and **FastAPI** orchestration.

> **Design principle:** make execution traceable, make uncertainty visible, and keep finalization behind a quality gate. The project does not automatically execute arbitrary model-generated Python as part of its normal workflow.

## The system, visualized

<div align="center">
<img src="docs/assets/architecture-infographic.svg" alt="Verity-1 architecture infographic: LangGraph control plane, CrewAI specialists, Pydantic validation, QA retry routing, FastAPI, and SQLite" width="100%">
</div>

The outer graph owns state transitions and retry decisions. Specialist agents contribute analysis, browser results are stored as explicit records, data crosses schema boundaries, and the QA critic decides whether the workflow can finish or must retry.

<details>
<summary><strong>▶ Play the animated workflow replay</strong></summary>
<div align="center"><img src="docs/assets/workflow-replay.svg" alt="Animated Verity-1 execution trace showing plan, research, validation, synthesis, QA, and the retry loop" width="100%"></div>
This is an illustrative motion diagram of the implemented control flow, not a recording of a live task or a live telemetry feed. The [interactive control room](https://muhammadsaeed-ai.github.io/verity-core-agent/) lets you replay the sequence and inspect the accepted and retry paths.
</details>

## Verification, not vanity metrics

<div align="center"><img src="docs/assets/quality-policy.svg" alt="Verity-1 QA policy chart: 0.80 acceptance threshold, two maximum retries, ten automated tests passed, and five smoke-test artifact types" width="100%"></div>

These figures describe the current implementation and verified deterministic CI baseline. They are **not** claims about answer accuracy, latency, browser coverage, or production uptime.

| Control | Current behavior |
|---|---|
| QA acceptance threshold | <code>confidence &gt;= 0.80</code> |
| Retry budget | Maximum 2 retries; failed exhaustion ends as <code>needs_review</code> |
| Test baseline | 10 automated tests passed in the latest verified CI run |
| End-to-end outputs | <code>plan.json</code>, <code>evidence.json</code>, <code>execution-notes.md</code>, <code>report.md</code>, <code>report.json</code> |
| Persistence | SQLite-backed LangGraph checkpointing plus task records |
| Hand-off validation | Pydantic v2 models with unexpected fields forbidden |

## Capability map

<table>
<tr><td width="50%" valign="top">

### Stateful control plane

- LangGraph StateGraph and conditional routing
- SQLite SqliteSaver checkpointing
- Task IDs mapped to graph thread IDs
- Bounded retries with explicit terminal status

</td><td width="50%" valign="top">

### Specialist execution

- **Lead Architect & Planner**
- **Deep Web Intelligence Operator**
- **QA Verifier & Critic**
- CrewAI sequential specialist tasks

</td></tr>
<tr><td width="50%" valign="top">

### Evidence and synthesis

- Browser Use in a separate optional environment
- Playwright with DuckDuckGo HTML HTTP fallback
- Pydantic validation for structured evidence
- Markdown / JSON artifacts and SHA-256 metadata

</td><td width="50%" valign="top">

### API and automation

- FastAPI task-run and status endpoints
- Interactive local OpenAPI docs
- Operator-configured n8n completion/failure callback
- Docker and Docker Compose support

</td></tr>
</table>

## Quick start

**Baseline:** Python 3.12. The deterministic smoke test runs without an external model key or browser session.

<pre><code>git clone https://github.com/muhammadsaeed-ai/verity-core-agent.git
cd verity-core-agent

python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt</code></pre>

Copy <code>.env.example</code> to <code>.env</code>, then run the verified deterministic path:

<pre><code>python main.py --test-e2e</code></pre>

Expected files are written under <code>outputs/&lt;task-id&gt;/</code>. The smoke test exits non-zero if a required artifact is missing or QA does not finish as accepted.

### Run a live task

Configure <code>GOOGLE_API_KEY</code> in <code>.env</code> for Gemini, or use Ollama as described below:

<pre><code>python main.py --task "Research current Python agent orchestration options and produce a sourced comparison"</code></pre>

A configured model is required. Live research also needs internet access. When search backends return no usable sources, Verity-1 reports that limitation rather than fabricating source URLs.

### Start the API

<pre><code>python main.py --serve</code></pre>

Open <code>http://127.0.0.1:8000/docs</code>.

<pre><code>curl -X POST http://127.0.0.1:8000/api/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"user_objective":"Create a verifiable report for a test workflow objective."}'</code></pre>

Use the returned <code>task_id</code> to query <code>GET /api/v1/agent/status/{task_id}</code>.

## Model providers

### Gemini — default

<pre><code>VERITY_PROVIDER=gemini
GOOGLE_API_KEY=your_google_ai_studio_key
VERITY_GEMINI_MODEL=gemini-2.5-flash</code></pre>

Availability, quotas, rate limits, and data handling depend on Google's current terms. Unlimited free inference is not guaranteed.

### Ollama — local inference

Install Ollama separately and retrieve a model:

<pre><code>ollama pull llama3.2</code></pre>

Then configure:

<pre><code>VERITY_PROVIDER=ollama
VERITY_OLLAMA_MODEL=llama3.2
VERITY_OLLAMA_BASE_URL=http://localhost:11434
VERITY_RESEARCH_ENABLED=false
N8N_WEBHOOK_URL=</code></pre>

This can keep model inference local and disables online research/callbacks. Installing packages and downloading model weights require network access beforehand.

## Browser Use dependency isolation

Recent Browser Use releases and the selected CrewAI release declare incompatible major versions of MCP. To avoid an unsatisfiable installation in the main environment, Browser Use is optional and installed into a separate Python environment:

<pre><code>python -m venv .venv-browser-use</code></pre>

Install <code>requirements-browser-use.txt</code> in that environment, then set <code>VERITY_BROWSER_ENGINE=browser-use</code>. The tool auto-detects <code>.venv-browser-use</code>, or you can specify <code>VERITY_BROWSER_USE_PYTHON</code>. Playwright and the HTTP fallback remain available from the main environment.

For Playwright, install the browser binary once:

<pre><code>python -m playwright install chromium</code></pre>

## API contract

| Method | Endpoint | Purpose |
|---|---|---|
| <code>POST</code> | <code>/api/v1/agent/run</code> | Accept a task and return its ID |
| <code>GET</code> | <code>/api/v1/agent/status/{task_id}</code> | Read task status, QA feedback and artifacts |
| <code>GET</code> | <code>/health</code> | Service liveness check |

The request accepts <code>user_objective</code> or the alias <code>objective</code>, plus an optional <code>task_id</code>. IDs may contain letters, numbers, underscores, and hyphens. Duplicate IDs return <code>409</code>; unknown tasks return <code>404</code>.

Configure <code>N8N_WEBHOOK_URL</code> only for an endpoint you control. Callback URLs are operator configuration, not accepted from public run requests.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| <code>VERITY_PROVIDER</code> | <code>gemini</code> | <code>gemini</code> or <code>ollama</code> |
| <code>VERITY_GEMINI_MODEL</code> | <code>gemini-2.5-flash</code> | Gemini model |
| <code>VERITY_OLLAMA_MODEL</code> | <code>llama3.2</code> | Local Ollama model |
| <code>DRY_RUN_TEST_MODE</code> | <code>false</code> | Deterministic evidence and QA for tests |
| <code>VERITY_RESEARCH_ENABLED</code> | <code>true</code> | Enable web research |
| <code>VERITY_BROWSER_ENGINE</code> | <code>playwright</code> | <code>playwright</code> or <code>browser-use</code> first |
| <code>VERITY_BROWSER_USE_PYTHON</code> | auto-detect | Optional isolated Browser Use interpreter |
| <code>VERITY_ENABLE_CREWAI</code> | <code>true</code> | Enable live specialist crew |
| <code>VERITY_DATABASE_PATH</code> | <code>data/verity.sqlite3</code> | SQLite checkpoint/task database |
| <code>VERITY_OUTPUT_DIR</code> | <code>outputs</code> | Artifact output root |
| <code>N8N_WEBHOOK_URL</code> | empty | Trusted completion/failure callback |
| <code>VERITY_API_HOST</code> | <code>0.0.0.0</code> | API bind host |
| <code>VERITY_API_PORT</code> | <code>8000</code> | API port |

## Verification

Run the test suite and end-to-end smoke test:

<pre><code>pytest -v
python main.py --test-e2e</code></pre>

GitHub Actions runs dependency installation, unit/integration tests, and the deterministic smoke test on pushes and pull requests. Inspect the [CI run history](https://github.com/muhammadsaeed-ai/verity-core-agent/actions) for the latest result.

## Repository structure

<pre><code>docs/
├── index.html                    # Interactive branded control-room showcase
└── assets/
    ├── verity-hero.svg            # Hero artwork + animated system trace
    ├── architecture-infographic.svg
    ├── quality-policy.svg         # Policy values + current CI baseline
    └── workflow-replay.svg        # Animated workflow motion diagram
src/
├── config.py                      # Gemini / Ollama providers and settings
├── schemas.py                     # Pydantic v2 contracts
├── tools/
│   ├── browser_tool.py            # Browser Use bridge + Playwright + HTTP fallback
│   └── code_sandbox.py            # Restricted Python runner + artifact writer
├── crew/specialist_crew.py        # CrewAI specialist roles
├── graph/
│   ├── state.py                   # Typed state contract
│   └── workflow.py                # Graph nodes, QA routing, SQLite checkpoints
└── api/server.py                  # FastAPI + n8n gateway
scripts/browser_use_worker.py      # Isolated Browser Use worker
main.py                            # CLI and server runner
tests/                             # Unit, retry, API and workflow tests</code></pre>

## Security and reliability boundaries

- QA confidence is an automated signal, not proof of factual correctness.
- A source URL shows that a result was retrieved; it does not prove every associated claim.
- Browser navigation requires installed browser dependencies; the HTTP parser is the final fallback.
- The restricted Python runner is defense-in-depth, not a secure multi-tenant OS sandbox. Add operating-system/container isolation and resource limits before exposing execution to untrusted users.
- The n8n callback is outbound network traffic. Configure a destination you control.
- Follow each model provider's current usage terms and data policies.

---

<div align="center">

**Built for developers by VerityAgent.**  
Open-source foundation maintained by Mohammed Saeed, founder and owner of VerityAgent.

</div>
