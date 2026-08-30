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

    for _ in range(max_continuation_retries):
        decision, evaluated = await select_provider(
            classification=_SimpleClassification(domain=domain),
            redis_client=redis_client,
            exclude_key_ids=tried_key_ids,
        )
        if decision is None:
            attempts.extend(evaluated)
            return {"final_output": final_output, "attempts": attempts}

        continuation_context, compression_mode = await _build_continuation_context(
            original_prompt=original_prompt,
            partial_output=final_output,
            domain=domain,
        )
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
            if finish_reason == "length":
                tried_key_ids.add(decision.key_id)
                attempt["status"] = "partial_length"
                continue
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

    return {"final_output": final_output, "attempts": attempts}


async def _build_continuation_context(original_prompt: str, partial_output: str, domain: str) -> tuple[str, str]:
    """Build continuation context using domain-aware compression policy."""
    if domain == "CODE_GEN":
        restatement = original_prompt[:150]
        context = (
            f"Original prompt (restated): {restatement}\n\n"
            "[Continue directly from this exact point:]\n"
            f"{partial_output}"
        )
        return context, "code_verbatim"

    if len(partial_output) < 500:
        context = (
            "[Continue directly from this exact point:]\n"
            f"{partial_output}"
        )
        return context, "short_verbatim"

    head = partial_output[:-400]
    tail = partial_output[-400:]
    summary = await _compress_head_with_ollama(head)
    if summary is None:
        fallback_tail = partial_output[-800:]
        context = (
            "[Continue directly from this exact point:]\n"
            f"{fallback_tail}"
        )
        return context, "fallback_tail_800"

    context = (
        f"Summary of text so far: {summary}\n\n"
        "[Continue directly from this exact point:]\n"
        f"{tail}"
    )
    return context, "ollama_summary_plus_tail"


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
