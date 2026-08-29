# CipherAI (Hackathon Prototype)

CipherAI is an async LLM orchestration backend that:
- classifies prompts locally via Ollama (`phi4-mini:latest`)
- routes requests using a benchmark-ranked matrix
- avoids rate-limit failures with Redis-backed key health + circuit breaker
- streams telemetry snapshots for a dashboard

## Architecture

- `classifier/slm_classifier.py`: offline-first intent + complexity classification
- `config/benchmark_matrix.py`: static ranked provider/model/key candidates by domain
- `router/decision_engine.py`: pure Redis-based healthy candidate selection
- `router/circuit_breaker.py`: retry/failover/cooldown dispatch flow
- `orchestrator/pipeline.py`: thin end-to-end composition
- `api/main.py`: FastAPI endpoints + websocket telemetry
- `utils/quota_store.py`: normalized quota writes + telemetry publish

## Quick Start (Local)

1) Install dependencies

```bash
python -m pip install -r requirements.txt
```

2) Start Redis (host)

```bash
docker run --rm -p 6379:6379 redis:7-alpine
```

3) Start Ollama and pull classifier model

```bash
ollama pull phi4-mini:latest
ollama run phi4-mini:latest
```

4) Configure environment

```bash
cp .env.example .env
# add GROQ_API_KEY / GEMINI_API_KEY / MISTRAL_API_KEY as available
```

5) Run API

```bash
./scripts/run_local.sh
```

## Quick Start (Docker Compose)

```bash
cp .env.example .env
# For Linux Docker + host Ollama:
# set OLLAMA_BASE_URL=http://host.docker.internal:11434

docker compose up --build
```

## Endpoints

- `POST /route`
  - body: `{"prompt":"Write an optimized Rust concurrency module"}`
- `GET /status`
  - returns all `quota:*` hashes in Redis
- `WS /ws/telemetry`
  - sends initial status snapshot, then live `telemetry_updates`

## Example Request

```bash
curl -X POST http://localhost:8000/route \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Write an optimized, thread-safe memory pool module in Rust with full error handling."}'
```

## Notes

- If Ollama is unavailable, classifier auto-falls back to keyword heuristics.
- If a provider returns 429, the key is marked `cooling_down` and another candidate is retried.
- Benchmark routing is static by design for fast hackathon iteration.

## Quick Tests

```bash
pytest -q
```
