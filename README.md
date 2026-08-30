# CipherAI (Hackathon Prototype)

CipherAI is an async LLM orchestration backend that routes prompts to the best available model while handling key limits and failover automatically.

- Local-first classification via Ollama using `phi4-mini:latest`
- Benchmark-ranked routing by domain
- Redis-backed quota state with `healthy`, `near_cap`, `cooling_down`
- Circuit-breaker retries and cooldown handling
- Live telemetry over REST + WebSocket
- Developer observability via structured logs and a terminal dashboard

## Why This Matters: Beyond What Existing Tools Do

Even widely-used AI developer tools like Cursor handle provider rate limits reactively, not automatically. When Cursor hits a rate limit mid-session, it stops the request and surfaces an error to the user directly: "We've hit a rate limit with the provider. Please switch to the 'auto' model, another model, or try again in a few moments." The user must manually intervene - pick a different model, retry, or wait.

CIPHER AI goes a step further: when a provider's account hits its actual rate limit mid-generation (not just before a request starts), CIPHER AI automatically:
1. Detects the mid-stream failure
2. Selects a healthy alternative provider via the same benchmark-ranked decision engine used for initial routing
3. Compresses the already-generated context locally (via an offline Ollama model, at zero additional API cost) for non-code domains, while preserving code generations verbatim to protect syntactic correctness
4. Seamlessly continues the response on the new provider - with no user intervention, and no visible interruption in the final output

This distinction matters industry-wide: a recent survey of major LLM API providers (Google, OpenRouter, xAI, Alibaba/Qwen) found that automatic model downgrade or fallback at quota exhaustion is largely undocumented - most vendors either error out directly or, at best, provide sample code for developers to build their own manual retry logic. CIPHER AI's mid-generation continuation handler is a genuine, working implementation of the kind of resilience most infrastructure providers only leave as an exercise for the developer.

## Current Architecture

- `classifier/slm_classifier.py`
  - Offline-first classifier
  - Explicit path logs:
    - local Ollama success
    - heuristic fallback + reason (`connection error`, `timeout`, `JSON parse failure`)
- `config/benchmark_matrix.py`
  - Static domain-to-ranked-candidates matrix
  - Current Groq model entries use `qwen/qwen3.8-27b`
  - Current Gemini entries use `gemini-3.6-flash`
- `router/decision_engine.py`
  - Pure Redis selection logic
  - Per-candidate evaluation logs (healthy/skipped + reason)
- `router/circuit_breaker.py`
  - Dispatch retries, provider failover, cooldown marking
  - Per-attempt logs with latency and outcome
- `orchestrator/pipeline.py`
  - Thin orchestration layer
  - Sectioned request logs: `PROMPT`, `CLASSIFICATION`, `ROUTING ATTEMPTS`, `RESULT`
- `api/main.py`
  - FastAPI endpoints, adapter wiring, CORS
  - Structured logger setup to stdout with `[CipherAI]` prefix and timestamp
- `dashboard/monitor.py`
  - Live `rich` terminal monitor polling `/status`
  - Color-coded health table + explanatory note for unseen ranks

## Local Setup (Recommended)

1. Install dependencies

```bash
python -m pip install -r requirements.txt
```

2. Start Redis/Valkey

```bash
sudo systemctl enable --now valkey
```

3. Start Ollama model

```bash
ollama pull phi4-mini:latest
ollama run phi4-mini:latest
```

4. Configure environment

```bash
cp .env.example .env
# set GROQ_API_KEY / GEMINI_API_KEY / MISTRAL_API_KEY
```

5. Run API

```bash
./scripts/run_local.sh
```

## Endpoints

- `POST /route`
  - Request body: `{"prompt":"Write an optimized Rust module"}`
  - Response includes `completion`, `domain`, `complexity`, `provider`, `model`, `attempts`
- `GET /status`
  - Returns all `quota:*` hashes currently known in Redis
- `WS /ws/telemetry`
  - Sends initial status snapshot and then live `telemetry_updates`

## Local Dashboard

Run the API in one terminal:

```bash
./scripts/run_local.sh
```

Run monitor in a second terminal:

```bash
python dashboard/monitor.py
```

The monitor shows provider/key health while you test with `curl`.

## Observability

Structured logs are emitted at INFO level with `[CipherAI]` prefix and timestamp.

Per request:
- full prompt text
- classification details (`domain`, `complexity`, `complexity_score`)
- each routing attempt (rank/provider/model/status/reason)
- final result summary (provider/model + completion length only)

## Quick Test Commands

```bash
pytest -q
```

```bash
curl -X POST http://localhost:8000/route   -H "Content-Type: application/json"   -d '{"prompt":"Write an optimized, thread-safe memory pool module in Rust with full error handling."}'
```

## Optional Docker

`Dockerfile` and `docker-compose.yml` are included for API + Redis workflows.
For fast hackathon iteration, local host Ollama + `run_local.sh` remains the primary path.

## README Maintenance Rule

For every major change (routing behavior, model matrix strategy, observability, deployment flow, or API contract), update this README in the same commit/PR so docs stay accurate.
