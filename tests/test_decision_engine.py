"""Tests for Redis-based routing decision selection."""

from __future__ import annotations

import asyncio

from classifier.slm_classifier import ClassificationResult
from router.decision_engine import select_provider


class FakeRedis:
    """Very small async Redis stub for decision engine tests."""

    def __init__(self, store: dict[str, dict[str, str]]) -> None:
        self.store = store

    async def hgetall(self, key: str) -> dict[str, str]:
        return self.store.get(key, {})


def test_selects_first_healthy_candidate() -> None:
    """Should skip unavailable higher ranks and select first healthy candidate."""

    async def _run() -> None:
        redis = FakeRedis(
            {
                "quota:mistral:MISTRAL_API_KEY": {
                    "status": "healthy",
                    "remaining_requests": "9",
                    "limit_requests": "10",
                },
                "quota:groq:GROQ_API_KEY": {"status": "near_cap", "remaining_requests": "1", "limit_requests": "30"},
            }
        )
        classification = ClassificationResult(
            domain="CODE_GEN",
            complexity="HIGH",
            complexity_score=8,
            est_input_tokens=400,
            est_output_tokens=1500,
        )
        decision, evaluated = await select_provider(classification, redis)
        assert decision is not None
        assert decision.key_id == "mistral_key_code1"
        assert decision.provider == "mistral"
        assert len(evaluated) >= 1

    asyncio.run(_run())


def test_treats_missing_quota_as_healthy() -> None:
    """If key has no Redis hash yet, it should still be selectable."""

    async def _run() -> None:
        redis = FakeRedis({})
        classification = ClassificationResult(
            domain="GENERAL",
            complexity="LOW",
            complexity_score=2,
            est_input_tokens=20,
            est_output_tokens=200,
        )
        decision, evaluated = await select_provider(classification, redis)
        assert decision is not None
        assert "selected" in decision.reason.lower()
        assert len(evaluated) == 1

    asyncio.run(_run())
