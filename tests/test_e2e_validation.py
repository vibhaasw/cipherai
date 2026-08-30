"""End-to-end validation coverage for CipherAI core flows."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import httpx
import pytest

from adapters.base import QuotaSnapshot
from classifier.slm_classifier import classify_prompt
from orchestrator import pipeline
from router import continuation_handler
from router.circuit_breaker import dispatch_with_breaker
from router.decision_engine import ClassificationResult, RoutingDecision, select_provider


class FakeRedis:
    """Minimal async Redis stub used across routing/continuation tests."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, Any]] = {}
        self.lists: dict[str, list[str]] = {}

    async def hgetall(self, key: str) -> dict[str, Any]:
        return self.hashes.get(key, {})

    async def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        bucket = self.hashes.setdefault(key, {})
        current = int(bucket.get(field, 0))
        updated = current + amount
        bucket[field] = updated
        return updated

    async def expire(self, key: str, ttl: int) -> None:
        _ = (key, ttl)

    async def publish(self, channel: str, message: str) -> None:
        _ = (channel, message)

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, [])
        self.lists[key].insert(0, value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        if key not in self.lists:
            return
        self.lists[key] = self.lists[key][start : end + 1]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        if key not in self.lists:
            return []
        return self.lists[key][start : end + 1]


@dataclass
class FakeAdapter:
    """Configurable fake adapter with queued outcomes."""

    name: str
    outcomes: list[dict[str, Any]]

    def __post_init__(self) -> None:
        self.last_finish_reason: str | None = None
        self.last_midstream_status_code: int | None = None
        self.last_midstream_rate_limited: bool = False
        self.last_midstream_reason: str | None = None

    async def dispatch(self, prompt: str, **kwargs) -> tuple[str, QuotaSnapshot]:
        _ = (prompt, kwargs)
        if not self.outcomes:
            raise RuntimeError("No configured outcome")
        outcome = self.outcomes.pop(0)
        if "raise_http_status" in outcome:
            status_code = int(outcome["raise_http_status"])
            req = httpx.Request("POST", "https://mock.provider/chat")
            resp = httpx.Response(status_code=status_code, request=req, headers=outcome.get("headers", {}))
            raise httpx.HTTPStatusError("mock", request=req, response=resp)
        if "raise_exc" in outcome:
            raise outcome["raise_exc"]

        self.last_finish_reason = outcome.get("finish_reason")
        self.last_midstream_status_code = outcome.get("midstream_status_code")
        self.last_midstream_rate_limited = bool(outcome.get("midstream_rate_limited", False))
        self.last_midstream_reason = outcome.get("midstream_reason")
        completion = str(outcome.get("completion", "ok"))
        snapshot = outcome.get(
            "snapshot",
            QuotaSnapshot(
                remaining_requests=20,
                limit_requests=30,
                remaining_tokens=5000,
                limit_tokens=6000,
                reset_requests_at=9999999999.0,
                reset_tokens_at=9999999999.0,
                tracking_mode="self",
            ),
        )
        return completion, snapshot


@pytest.mark.asyncio
async def test_01_classification_all_five_domains_with_unambiguous_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate all five domain labels using explicit keyword prompts."""

    class _NoOllamaClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr("classifier.slm_classifier.httpx.AsyncClient", lambda *a, **k: _NoOllamaClient())

    cases = [
        ("Write a Rust function with lifetimes and trait bounds.", "CODE_GEN"),
        ("Write a poem about monsoon evenings.", "CREATIVE_TEXT"),
        ("Solve this equation: 4x + 8 = 40.", "MATH_LOGIC"),
        ("Summarize this transcript in 5 bullets.", "DOC_SUMMARIZATION"),
        ("Explain the concept clearly.", "GENERAL"),
    ]
    for prompt, expected in cases:
        result = await classify_prompt(prompt)
        assert result.domain == expected


@pytest.mark.asyncio
async def test_02_classifier_fallback_when_ollama_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Classifier should not crash when Ollama is unreachable."""

    class _NoOllamaClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr("classifier.slm_classifier.httpx.AsyncClient", lambda *a, **k: _NoOllamaClient())
    result = await classify_prompt("Write a Python class with async methods.")
    assert result.domain == "CODE_GEN"
    assert result.est_input_tokens > 0


@pytest.mark.asyncio
async def test_03_circuit_breaker_failover_from_cooling_down_top_candidate() -> None:
    """When top-ranked is cooling_down, next healthy candidate should be selected."""
    redis = FakeRedis()
    await redis.hset("quota:mistral:MISTRAL_API_KEY", {"status": "cooling_down", "reset_requests_at": 9999999999.0})
    await redis.hset("quota:groq:GROQ_API_KEY", {"status": "healthy", "remaining_requests": 20, "limit_requests": 30})

    classification = ClassificationResult("CODE_GEN", "HIGH", 8, 200, 1000)
    adapters = {
        "groq": FakeAdapter("groq", [{"completion": "fallback_success", "finish_reason": "stop"}]),
        "mistral": FakeAdapter("mistral", [{"completion": "should_not_be_used", "finish_reason": "stop"}]),
    }
    result = await dispatch_with_breaker(classification, "prompt", redis, adapters, max_retries=3)
    assert result["provider"] == "groq"
    assert result["completion"] == "fallback_success"
    assert result["attempts"][0]["provider"] == "groq"


@pytest.mark.asyncio
async def test_04_full_domain_exhaustion_returns_no_healthy_with_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pipeline should return clean NO_HEALTHY_PROVIDER with evaluated attempts trail."""
    redis = FakeRedis()
    for key in [
        "quota:mistral:MISTRAL_API_KEY",
        "quota:groq:GROQ_API_KEY",
        "quota:gemini:GEMINI_API_KEY",
        "quota:groq_2:GROQ_API_KEY_2",
        "quota:mistral_2:MISTRAL_API_KEY_2",
        "quota:gemini_2:GEMINI_API_KEY_2",
    ]:
        await redis.hset(key, {"status": "cooling_down", "reset_requests_at": 9999999999.0})

    async def _fake_classify(prompt: str):
        _ = prompt
        return ClassificationResult("CODE_GEN", "HIGH", 8, 100, 800)

    monkeypatch.setattr("orchestrator.pipeline.classify_prompt", _fake_classify)
    result = await pipeline.handle_prompt("force exhaustion", redis, adapters={})
    assert result["error"] == "NO_HEALTHY_PROVIDER"
    assert len(result["attempts"]) >= 3
    assert all(attempt["status"] == "skipped" for attempt in result["attempts"])


@pytest.mark.asyncio
async def test_05_secondary_accounts_reachable_when_primary_exhausted() -> None:
    """Secondary provider accounts should be considered by matrix depth."""
    redis = FakeRedis()
    await redis.hset("quota:mistral:MISTRAL_API_KEY", {"status": "cooling_down", "reset_requests_at": 9999999999.0})
    await redis.hset("quota:groq:GROQ_API_KEY", {"status": "cooling_down", "reset_requests_at": 9999999999.0})
    await redis.hset("quota:gemini:GEMINI_API_KEY", {"status": "cooling_down", "reset_requests_at": 9999999999.0})

    classification = ClassificationResult("CODE_GEN", "HIGH", 8, 200, 1000)
    decision, evaluated = await select_provider(classification, redis)
    assert decision is not None
    assert decision.provider in {"groq_2", "mistral_2", "gemini_2"}
    assert len(evaluated) >= 4


@pytest.mark.asyncio
async def test_06_continuation_trigger_regression_429_partial_only() -> None:
    """Continuation trigger should require rate-limit signal + partial output."""
    should_continue = pipeline._needs_continuation(  # noqa: SLF001
        {
            "midstream_failure": True,
            "partial_output": "already streamed text",
            "midstream_status_code": 429,
        }
    )
    should_not_continue_on_length = pipeline._needs_continuation(  # noqa: SLF001
        {
            "finish_reason": "length",
            "midstream_failure": False,
            "partial_output": "",
        }
    )
    assert should_continue is True
    assert should_not_continue_on_length is False


@pytest.mark.asyncio
async def test_07_continuation_code_gen_never_calls_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """CODE_GEN continuation must always use full verbatim path without Ollama compression."""
    redis = FakeRedis()
    called = {"count": 0}

    async def _should_not_be_called(head_text: str):
        _ = head_text
        called["count"] += 1
        return "unexpected"

    monkeypatch.setattr(continuation_handler, "_compress_head_with_ollama", _should_not_be_called)
    adapters = {"mistral": FakeAdapter("mistral", [{"completion": "CONT", "finish_reason": "stop"}])}

    result = await continuation_handler.handle_continuation(
        original_prompt="Write Rust module for lock-free queue and tests.",
        partial_output="fn main() { println!(\"x\"); }" * 40,
        domain="CODE_GEN",
        redis_client=redis,
        adapters=adapters,
        tried_key_ids=set(),
        max_continuation_retries=1,
    )
    assert called["count"] == 0
    assert "CONT" in result["final_output"]


@pytest.mark.asyncio
async def test_08_non_code_compression_long_calls_ollama_short_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-code continuation should compress long outputs and skip compression for short outputs."""
    redis = FakeRedis()
    calls = {"count": 0}

    async def _compress(head_text: str):
        calls["count"] += 1
        assert len(head_text) > 0
        return "summary text"

    monkeypatch.setattr(continuation_handler, "_compress_head_with_ollama", _compress)
    adapters = {"mistral": FakeAdapter("mistral", [{"completion": "A", "finish_reason": "stop"}, {"completion": "B", "finish_reason": "stop"}])}

    long_result = await continuation_handler.handle_continuation(
        original_prompt="Write a story about stars.",
        partial_output="stars " * 150,
        domain="CREATIVE_TEXT",
        redis_client=redis,
        adapters=adapters,
        tried_key_ids={"gemini_key1"},
        max_continuation_retries=1,
    )
    short_result = await continuation_handler.handle_continuation(
        original_prompt="Write a story about stars.",
        partial_output="short text",
        domain="CREATIVE_TEXT",
        redis_client=redis,
        adapters={"mistral": FakeAdapter("mistral", [{"completion": "C", "finish_reason": "stop"}])},
        tried_key_ids={"gemini_key1"},
        max_continuation_retries=1,
    )

    assert calls["count"] == 1
    assert long_result["attempts"][0]["compression_mode"] == "ollama_summary_plus_tail"
    assert short_result["attempts"][0]["compression_mode"] == "short_verbatim"


@pytest.mark.asyncio
async def test_09_continuation_ollama_unavailable_falls_back_to_verbatim_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """When compression Ollama is unavailable, continuation should fallback safely."""
    redis = FakeRedis()

    async def _compression_fail(head_text: str):
        _ = head_text
        return None

    monkeypatch.setattr(continuation_handler, "_compress_head_with_ollama", _compression_fail)
    adapters = {"mistral": FakeAdapter("mistral", [{"completion": "CONT_OK", "finish_reason": "stop"}])}
    result = await continuation_handler.handle_continuation(
        original_prompt="Write a creative paragraph.",
        partial_output="alpha " * 200,
        domain="CREATIVE_TEXT",
        redis_client=redis,
        adapters=adapters,
        tried_key_ids={"gemini_key1"},
        max_continuation_retries=1,
    )
    assert result["attempts"][0]["compression_mode"] == "fallback_tail_800"
    assert result["final_output"].endswith("CONT_OK")


@pytest.mark.asyncio
async def test_10_chained_continuation_retries_and_token_instrumentation(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Force two chained continuation hops and verify retry cap, concatenation,
    and token-savings instrumentation fields per attempt.
    """
    redis = FakeRedis()

    async def _compress_ok(head_text: str):
        return f"summary({len(head_text)})"

    monkeypatch.setattr(continuation_handler, "_compress_head_with_ollama", _compress_ok)

    adapters = {
        "mistral": FakeAdapter(
            "mistral",
            [
                {
                    "completion": "_HOP1_",
                    "finish_reason": "stop",
                    "midstream_status_code": 429,
                    "midstream_rate_limited": True,
                    "midstream_reason": "quota exceeded during stream",
                }
            ],
        ),
        "groq": FakeAdapter("groq", [{"completion": "_HOP2_", "finish_reason": "stop"}]),
    }

    calls = {"n": 0}

    async def _select_provider(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                RoutingDecision(
                    provider="mistral",
                    model="mistral-small-latest",
                    key_id="mistral_key1",
                    credential_ref="MISTRAL_API_KEY",
                    rank=1,
                    reason="first",
                ),
                [],
            )
        if calls["n"] == 2:
            return (
                RoutingDecision(
                    provider="groq",
                    model="qwen/qwen3.8-27b",
                    key_id="groq_key4",
                    credential_ref="GROQ_API_KEY",
                    rank=2,
                    reason="second",
                ),
                [],
            )
        return None, []

    monkeypatch.setattr(continuation_handler, "select_provider", _select_provider)

    result = await continuation_handler.handle_continuation(
        original_prompt="Write a long creative story.",
        partial_output="base " * 300,
        domain="CREATIVE_TEXT",
        redis_client=redis,
        adapters=adapters,
        tried_key_ids={"gemini_key1"},
        max_continuation_retries=2,
    )

    # Concatenation should include both hops if chained.
    assert "_HOP1_" in result["final_output"]
    assert "_HOP2_" in result["final_output"]

    # Instrumentation should exist on every continuation attempt record.
    for attempt in result["attempts"]:
        assert "est_tokens_sent" in attempt
        assert "est_tokens_if_uncompressed" in attempt
        assert "tokens_saved_pct" in attempt
        assert "compression_used" in attempt
        sent = float(attempt["est_tokens_sent"])
        uncompressed = float(attempt["est_tokens_if_uncompressed"])
        pct = float(attempt["tokens_saved_pct"])
        if uncompressed > 0:
            expected_pct = max((1 - (sent / uncompressed)) * 100, 0.0)
            assert abs(pct - expected_pct) < 0.5
