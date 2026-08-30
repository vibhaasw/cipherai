# CIPHER AI — Context Strategy Comparison

> **Data source:** Redis `continuation_events` list, extracted by `scripts/generate_comparison_report.py`  
> **Last generated:** from live Redis at report build time  
> **Sample size:** 0 continuation events recorded in this session

---

## Why This Comparison, Not Others

This document compares **methods for reducing tokens sent to a provider during mid-generation continuation** — when a response is cut off mid-stream by rate-limit/quota exhaustion and must be resumed on a different model. It does **not** compare data storage or persistence approaches. A SQL database or DBMS is not a competing strategy here: where data lives at rest does not change how many tokens are billed on the next API call. Storage solves retention; continuation context strategy solves **what gets sent back to the LLM** on the failover request.

---

## Method Comparison Table

| Approach | Tokens sent (measured) | Continuity risk | Implementation complexity | Latency overhead |
| --- | --- | --- | --- | --- |
| **1. Naive full resend** — send entire raw `partial_output` every time | **N/A** — no continuation events recorded; would use avg `est_tokens_if_uncompressed` when available | Low — full verbatim context preserved | Trivial — no preprocessing | None |
| **2. Fixed tail-only truncation** — keep last N characters, discard head | **Not implemented — theoretical, discarded context risk** | High — loses tone, structure, and earlier reasoning/code setup | Low — string slice only | None |
| **3. Vector DB / RAG retrieval** — embed partial output, retrieve similar chunks | **Not applicable to this use case** — single linear continuation, not multi-document lookup | Medium — retrieval may miss exact resumption point | High — embeddings, index, retrieval pipeline | High — embed + query per continuation |
| **4. Our approach — Ollama head summary + verbatim tail** | **N/A** — no compressed continuations recorded; would use avg `est_tokens_sent` and avg `tokens_saved_pct` when available | Low for resumption point (tail verbatim); medium for distant context (summarized head) | Medium — domain rules + local Ollama call with fallback | Low–medium — one local SLM call when compression eligible |

**Session note:** With 0 events in `continuation_events`, no measured token figures are available for rows 1 or 4. Run additional mid-stream rate-limit continuation test cases and re-run `python scripts/generate_comparison_report.py` before presenting numeric claims to judges.

---

## Real Results From Testing

Aggregates below are computed directly from Redis — **not estimated**.

| Domain | Compression Used | Avg Tokens Saved % | Sample Size (N) |
| --- | --- | --- | --- |
| CODE_GEN | N/A — verbatim by design | N/A — verbatim by design | 0 |
| CREATIVE_TEXT | No events recorded | N/A | 0 |
| MATH_LOGIC | No events recorded | N/A | 0 |
| DOC_SUMMARIZATION | No events recorded | N/A | 0 |
| GENERAL | No events recorded | N/A | 0 |

### Aggregate metrics (all domains, all events)

| Metric | Value |
| --- | --- |
| Total continuation events | **0** |
| Overall avg `est_tokens_sent` | N/A |
| Overall avg `est_tokens_if_uncompressed` (naive full-resend baseline) | N/A |
| Overall avg `tokens_saved_pct` (compressed domains only) | N/A |
| Ollama fallback triggers (`fallback_count` sum) | **0** |
| Compressed continuation events | **0 / 0** |

> **Limited sample size (0 events)** — recommend running additional continuation test cases before presenting this data to judges.

To refresh this table with real numbers:

```bash
cd cipherai
python scripts/generate_comparison_report.py
# then regenerate or hand-update this doc from docs/comparison_report_stats.json
```

---

## Design Rationale

### Why CODE_GEN is exempt from compression

Code continuations are **syntactically fragile**. A model resuming mid-function needs the exact last lines — brace depth, variable names, indentation, and incomplete expressions. A natural-language summary of "what was written so far" cannot reliably tell the next model *which character* to continue from. Sending the full partial output verbatim (plus a short restatement of the original prompt) avoids corrupting structure. That is a deliberate design choice, not a failure to compress — hence **N/A — verbatim by design** in the results table rather than 0% savings.

### Why other domains use head summary + verbatim tail

For prose, reasoning, and summarization tasks, earlier content mainly carries **tone, topic, and argument direction**. The critical continuity constraint is the **exact resumption point** — typically the last ~400 characters. CIPHER AI therefore:

1. Keeps the **tail verbatim** (exact resumption point).
2. Summarizes the **head** with local Ollama (`phi4-mini:latest`) into 2–3 sentences when output exceeds ~500 characters.
3. Falls back to last ~800 characters verbatim if Ollama is unavailable — counted as an Ollama fallback trigger in `continuation_events`.

This reduces tokens sent on the continuation API call while preserving the precise handoff point. Measured savings appear in `tokens_saved_pct` per event once continuation tests populate Redis.

---

## How events are logged

Each `handle_continuation()` completion pushes a JSON record to Redis (`LPUSH continuation_events`, capped at 50). Fields used in this report:

- `domain`, `compression_used` (`yes` / `no` / `N/A` for CODE_GEN)
- `est_tokens_sent`, `est_tokens_if_uncompressed`, `tokens_saved_pct`
- `fallback_count` — Ollama compression attempted but fell back to verbatim tail
- `eligible_for_compression`, `final_status`

See `router/continuation_handler.py` for the full schema.
