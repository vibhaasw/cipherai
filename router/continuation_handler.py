"""Mid-generation continuation handling with domain-aware context compression."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

import httpx

if __package__ is None or __package__ == "":
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from adapters.base import ProviderAdapter
from classifier.slm_classifier import OLLAMA_BASE_URL, OLLAMA_TIMEOUT_SECONDS
from router.decision_engine import select_provider
from utils.quota_store import write_quota_to_redis

logger = logging.getLogger("cipherai.router.continuation_handler")

STYLE_MATCH_INSTRUCTION = (
    "You are continuing a {domain} response that was cut off mid-generation by "
    "a different AI model. Continue EXACTLY where the text below stops — match "
    "its existing tone, formatting, and style as closely as possible. Do not "
    "repeat any part of the text below. Do not add commentary, headers, "
    "greetings, or acknowledgment of the handoff — output ONLY the raw "
    "continuation, as if it were one single uninterrupted response."
)


async def handle_continuation(
    original_prompt: str,
    partial_output: str,
    domain: str,
    redis_client,
    adapters: dict[str, ProviderAdapter],
    tried_key_ids: set[str],
    max_continuation_retries: int = 2,
) -> dict:
    """Continue a cut-off generation by switching providers and preserving style."""
    final_output = partial_output
    attempts: list[dict[str, Any]] = []
    initial_tried = set(tried_key_ids)
    total_est_tokens_sent = 0
    total_est_tokens_uncompressed = 0
    compressed_count = 0
    eligible_count = 0
    fallback_count = 0

    for _ in range(max_continuation_retries):
        decision, evaluated = await select_provider(
            classification=_SimpleClassification(domain=domain),
            redis_client=redis_client,
            exclude_key_ids=tried_key_ids,
        )
        if decision is None:
            attempts.extend(evaluated)
            event = _build_continuation_event(
                domain=domain,
                initial_tried=initial_tried,
                attempts=attempts,
                total_est_tokens_sent=total_est_tokens_sent,
                total_est_tokens_uncompressed=total_est_tokens_uncompressed,
                compressed_count=compressed_count,
                eligible_count=eligible_count,
                fallback_count=fallback_count,
                final_status="exhausted",
            )
            await _persist_continuation_event(redis_client, event)
            return {"final_output": final_output, "attempts": attempts}

        continuation_context, compression_mode, compression_meta = await _build_continuation_context(
            original_prompt=original_prompt,
            partial_output=final_output,
            domain=domain,
        )
        total_est_tokens_sent += int(compression_meta["est_tokens_sent"])
        total_est_tokens_uncompressed += int(compression_meta["est_tokens_if_uncompressed"])
        per_attempt_saved_pct = _tokens_saved_pct(
            int(compression_meta["est_tokens_sent"]),
            int(compression_meta["est_tokens_if_uncompressed"]),
        )
        if bool(compression_meta["eligible_for_compression"]):
            eligible_count += 1
        if compression_mode == "ollama_summary_plus_tail":
            compressed_count += 1
        if compression_mode == "fallback_tail_800":
            fallback_count += 1
        prompt = (
            STYLE_MATCH_INSTRUCTION.format(domain=domain)
            + "\n\n"
            + continuation_context
        )

        attempt = {
            "provider": decision.provider,
            "model": decision.model,
            "key_id": decision.key_id,
            "credential_ref": decision.credential_ref,
            "rank": decision.rank,
            "status": "attempting",
            "reason": decision.reason,
            "compression_mode": compression_mode,
            "compression_used": "N/A" if domain == "CODE_GEN" else ("yes" if compression_mode == "ollama_summary_plus_tail" else "no"),
            "est_tokens_sent": int(compression_meta["est_tokens_sent"]),
            "est_tokens_if_uncompressed": int(compression_meta["est_tokens_if_uncompressed"]),
            "tokens_saved_pct": round(per_attempt_saved_pct, 2),
        }
        attempts.append(attempt)
        adapter = adapters.get(decision.provider)
        if adapter is None:
            attempt["status"] = "failed"
            attempt["error"] = "Adapter not configured for provider."
            await _mark_cooling_down(
                redis_client,
                decision.provider,
                decision.credential_ref,
                decision.key_id,
            )
            tried_key_ids.add(decision.key_id)
            continue

        start = time.perf_counter()
        try:
            completion, snapshot = await adapter.dispatch(
                prompt,
                model=decision.model,
                key_id=decision.key_id,
                credential_ref=decision.credential_ref,
            )
            latency_seconds = time.perf_counter() - start
            await write_quota_to_redis(
                redis_client,
                decision.provider,
                decision.credential_ref,
                decision.key_id,
                snapshot,
            )
            final_output = final_output + completion
            finish_reason = str(getattr(adapter, "last_finish_reason", "") or "").lower()
            attempt["status"] = "success"
            attempt["latency_s"] = round(latency_seconds, 3)
            attempt["finish_reason"] = finish_reason or "unknown"
            logger.info(
                "continuation_attempt provider=%s model=%s key_id=%s status=success finish_reason=%s",
                decision.provider,
                decision.model,
                decision.key_id,
                finish_reason or "unknown",
            )
            if _is_midstream_rate_limited(adapter, completion):
                attempt["status"] = "partial_rate_limited"
                attempt["reason"] = "midstream_rate_limit_with_partial_output"
                tried_key_ids.add(decision.key_id)
                continue
            event = _build_continuation_event(
                domain=domain,
                initial_tried=initial_tried,
                attempts=attempts,
                total_est_tokens_sent=total_est_tokens_sent,
                total_est_tokens_uncompressed=total_est_tokens_uncompressed,
                compressed_count=compressed_count,
                eligible_count=eligible_count,
                fallback_count=fallback_count,
                final_status="success",
            )
            await _persist_continuation_event(redis_client, event)
            return {"final_output": final_output, "attempts": attempts}
        except Exception as exc:
            latency_seconds = time.perf_counter() - start
            attempt["status"] = "failed"
            attempt["error"] = f"{type(exc).__name__}: {exc}"
            attempt["latency_s"] = round(latency_seconds, 3)
            await _mark_cooling_down(
                redis_client,
                decision.provider,
                decision.credential_ref,
                decision.key_id,
            )
            tried_key_ids.add(decision.key_id)
            logger.info(
                "continuation_attempt provider=%s model=%s key_id=%s status=failure reason=%s",
                decision.provider,
                decision.model,
                decision.key_id,
                type(exc).__name__,
            )

    event = _build_continuation_event(
        domain=domain,
        initial_tried=initial_tried,
        attempts=attempts,
        total_est_tokens_sent=total_est_tokens_sent,
        total_est_tokens_uncompressed=total_est_tokens_uncompressed,
        compressed_count=compressed_count,
        eligible_count=eligible_count,
        fallback_count=fallback_count,
        final_status="exhausted",
    )
    await _persist_continuation_event(redis_client, event)
    return {"final_output": final_output, "attempts": attempts}


async def _build_continuation_context(
    original_prompt: str,
    partial_output: str,
    domain: str,
) -> tuple[str, str, dict[str, Any]]:
    """Build continuation context using domain-aware compression policy."""
    if domain == "CODE_GEN":
        restatement = original_prompt[:150]
        context = (
            f"Original prompt (restated): {restatement}\n\n"
            "[Continue directly from this exact point:]\n"
            f"{partial_output}"
        )
        est = max(len(context) // 4, 1)
        return context, "code_verbatim", {
            "est_tokens_sent": est,
            "est_tokens_if_uncompressed": est,
            "eligible_for_compression": False,
        }

    if len(partial_output) < 500:
        context = (
            "[Continue directly from this exact point:]\n"
            f"{partial_output}"
        )
        est = max(len(context) // 4, 1)
        return context, "short_verbatim", {
            "est_tokens_sent": est,
            "est_tokens_if_uncompressed": est,
            "eligible_for_compression": False,
        }

    head = partial_output[:-400]
    tail = partial_output[-400:]
    uncompressed_context = (
        "[Continue directly from this exact point:]\n"
        f"{partial_output}"
    )
    uncompressed_est = max(len(uncompressed_context) // 4, 1)
    summary = await _compress_head_with_ollama(head)
    if summary is None:
        fallback_tail = partial_output[-800:]
        context = (
            "[Continue directly from this exact point:]\n"
            f"{fallback_tail}"
        )
        return context, "fallback_tail_800", {
            "est_tokens_sent": max(len(context) // 4, 1),
            "est_tokens_if_uncompressed": uncompressed_est,
            "eligible_for_compression": True,
        }

    context = (
        f"Summary of text so far: {summary}\n\n"
        "[Continue directly from this exact point:]\n"
        f"{tail}"
    )
    return context, "ollama_summary_plus_tail", {
        "est_tokens_sent": max(len(context) // 4, 1),
        "est_tokens_if_uncompressed": uncompressed_est,
        "eligible_for_compression": True,
    }


async def _compress_head_with_ollama(head_text: str) -> str | None:
    """Compress head text via local Ollama into 2-3 sentence continuity summary."""
    prompt = (
        "Summarize the following partial response into 2-3 short sentences, focusing only "
        "on subject, tone, and key points already covered. Do not rewrite it verbatim.\n\n"
        + head_text
    )
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": "phi4-mini:latest",
                    "prompt": prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("response", "")).strip() or None
    except (httpx.HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    except Exception:
        return None


async def _mark_cooling_down(redis_client, provider: str, credential_ref: str, key_id: str) -> None:
    """Mark a failed continuation key as cooling_down with fallback 30s reset."""
    now = time.time()
    await redis_client.hset(
        f"quota:{provider}:{credential_ref}",
        mapping={
            "provider": provider,
            "credential_ref": credential_ref,
            "key_id": key_id,
            "status": "cooling_down",
            "reset_requests_at": now + 30,
            "last_updated": now,
        },
    )


class _SimpleClassification:
    """Small domain-only shape for select_provider compatibility."""

    def __init__(self, domain: str) -> None:
        self.domain = domain


def _build_continuation_event(
    domain: str,
    initial_tried: set[str],
    attempts: list[dict[str, Any]],
    total_est_tokens_sent: int,
    total_est_tokens_uncompressed: int,
    compressed_count: int,
    eligible_count: int,
    fallback_count: int,
    final_status: str,
) -> dict[str, Any]:
    """Build compact continuation event record for Redis list storage."""
    successful = next((a for a in reversed(attempts) if a.get("status") == "success"), None)
    fallback_provider = successful.get("provider") if successful else (attempts[-1].get("provider") if attempts else None)
    compression_used = (
        "N/A"
        if domain == "CODE_GEN"
        else ("yes" if compressed_count > 0 else "no")
    )
    tokens_saved_pct = 0.0
    if total_est_tokens_uncompressed > 0:
        tokens_saved_pct = max(
            (1 - (total_est_tokens_sent / total_est_tokens_uncompressed)) * 100,
            0.0,
        )
    return {
        "timestamp": int(time.time()),
        "domain": domain,
        "original_provider": _infer_provider_from_tried(initial_tried),
        "fallback_provider": fallback_provider,
        "compression_used": compression_used,
        "est_tokens_sent": total_est_tokens_sent,
        "est_tokens_if_uncompressed": total_est_tokens_uncompressed,
        "tokens_saved_pct": round(tokens_saved_pct, 2),
        "retry_count": max(len(attempts) - 1, 0),
        "final_status": final_status,
        "eligible_for_compression": eligible_count > 0,
        "compressed_count": compressed_count,
        "eligible_count": eligible_count,
        "fallback_count": fallback_count,
    }


def _infer_provider_from_tried(tried_key_ids: set[str]) -> str:
    """Best-effort provider inference from key_id naming convention."""
    for key_id in tried_key_ids:
        lower = key_id.lower()
        if lower.startswith("groq_"):
            return "groq"
        if lower.startswith("gemini_"):
            return "gemini"
        if lower.startswith("mistral_"):
            return "mistral"
    return "unknown"


async def _persist_continuation_event(redis_client, event: dict[str, Any]) -> None:
    """Append continuation event to capped Redis list for dashboard usage."""
    payload = json.dumps(event)
    await redis_client.lpush("continuation_events", payload)
    await redis_client.ltrim("continuation_events", 0, 49)


def _tokens_saved_pct(est_tokens_sent: int, est_tokens_if_uncompressed: int) -> float:
    """Compute per-attempt compression savings percentage safely."""
    if est_tokens_if_uncompressed <= 0:
        return 0.0
    return max((1 - (est_tokens_sent / est_tokens_if_uncompressed)) * 100, 0.0)


def _is_midstream_rate_limited(adapter: ProviderAdapter, completion: str) -> bool:
    """Check provider-equivalent mid-stream quota exhaustion signals."""
    if not completion:
        return False
    status_code = getattr(adapter, "last_midstream_status_code", None)
    if status_code == 429:
        return True
    if bool(getattr(adapter, "last_midstream_rate_limited", False)):
        return True
    signal = " ".join(
        str(getattr(adapter, attr, "")).lower()
        for attr in ("last_midstream_error_type", "last_midstream_error", "last_midstream_reason")
    )
    markers = ("429", "resourceexhausted", "rate limit", "quota", "too many requests")
    return any(marker in signal for marker in markers)


if __name__ == "__main__":
    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, dict[str, Any]] = {}

        async def hgetall(self, key: str) -> dict[str, Any]:
            return self.store.get(key, {})

        async def hset(self, key: str, mapping: dict[str, Any]) -> None:
            self.store.setdefault(key, {}).update(mapping)

        async def publish(self, channel: str, message: str) -> None:
            return None

    class _FakeSnapshot:
        remaining_requests = 20
        limit_requests = 30
        remaining_tokens = 4000
        limit_tokens = 6000
        reset_requests_at = time.time() + 60
        reset_tokens_at = time.time() + 60
        tracking_mode = "self"

    class _FakeAdapter:
        def __init__(self, name: str) -> None:
            self.name = name
            self.last_finish_reason: str | None = None

        async def dispatch(self, prompt: str, **kwargs) -> tuple[str, Any]:
            max_tokens = int(kwargs.get("max_tokens", 200))
            if max_tokens <= 40:
                self.last_finish_reason = "length"
                return ("PARTIAL_OUTPUT_" + self.name + "_"), _FakeSnapshot()
            self.last_finish_reason = "stop"
            return ("CONTINUED_" + self.name + "_"), _FakeSnapshot()

    async def _demo() -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        redis_client = _FakeRedis()
        adapters = {
            "mistral": _FakeAdapter("mistral"),
            "groq": _FakeAdapter("groq"),
            "gemini": _FakeAdapter("gemini"),
            "mistral_2": _FakeAdapter("mistral_2"),
            "groq_2": _FakeAdapter("groq_2"),
            "gemini_2": _FakeAdapter("gemini_2"),
        }

        # Case 1: CODE_GEN forced cut-off with low max_tokens, then verbatim continuation.
        first_text, _ = await adapters["mistral"].dispatch("code prompt", max_tokens=20)
        code_result = await handle_continuation(
            original_prompt="Write a Rust memory pool with error handling and tests.",
            partial_output=first_text,
            domain="CODE_GEN",
            redis_client=redis_client,
            adapters=adapters,
            tried_key_ids={"mistral_key_code1"},
            max_continuation_retries=2,
        )
        print("CODE_GEN continuation attempts:", code_result["attempts"])
        print("CODE_GEN final_output:", code_result["final_output"])

        # Case 2: CREATIVE_TEXT forced cut-off and compression path check.
        long_partial, _ = await adapters["gemini"].dispatch("creative prompt", max_tokens=20)
        long_partial = long_partial + ("A poetic skyline " * 80)
        creative_result = await handle_continuation(
            original_prompt="Write a futuristic poem about an AI city at dawn.",
            partial_output=long_partial,
            domain="CREATIVE_TEXT",
            redis_client=redis_client,
            adapters=adapters,
            tried_key_ids={"gemini_key1"},
            max_continuation_retries=2,
        )
        print("CREATIVE continuation attempts:", creative_result["attempts"])
        print("CREATIVE compression modes:", [a.get("compression_mode") for a in creative_result["attempts"]])

    asyncio.run(_demo())
