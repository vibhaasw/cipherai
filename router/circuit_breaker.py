"""Dispatch loop with retry, cooldown, and provider failover."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import httpx

from adapters.base import ProviderAdapter
from classifier.slm_classifier import ClassificationResult
from router.decision_engine import select_provider
from utils.quota_store import write_quota_to_redis


@dataclass
class NoHealthyProviderError(Exception):
    """Raised when no healthy provider/key is available for routing."""

    message: str
    attempts: list[dict[str, Any]]

    def __str__(self) -> str:
        return self.message


@dataclass
class AllProvidersExhaustedError(Exception):
    """Raised when retries are exhausted after repeated dispatch failures."""

    message: str
    attempts: list[dict[str, Any]]

    def __str__(self) -> str:
        return self.message


async def dispatch_with_breaker(
    classification: ClassificationResult,
    prompt: str,
    redis_client,
    adapters: dict[str, ProviderAdapter],
    max_retries: int = 3,
) -> dict:
    """
    Route and dispatch with cooldown circuit-breaker behavior.

    Returns completion payload with full attempt log on success.
    """
    attempts: list[dict[str, Any]] = []
    tried_so_far: set[str] = set()

    for _ in range(max_retries):
        decision = await select_provider(
            classification=classification,
            redis_client=redis_client,
            exclude_key_ids=tried_so_far,
        )
        if decision is None:
            raise NoHealthyProviderError(
                message="No healthy provider/key available for this request.",
                attempts=attempts,
            )

        attempt_entry: dict[str, Any] = {
            "provider": decision.provider,
            "model": decision.model,
            "key_id": decision.key_id,
            "rank": decision.rank,
            "decision_reason": decision.reason,
            "status": "attempting",
        }
        attempts.append(attempt_entry)

        adapter = adapters.get(decision.provider)
        if adapter is None:
            attempt_entry["status"] = "failed"
            attempt_entry["error"] = f"Adapter not configured for provider '{decision.provider}'."
            await _mark_key_cooling_down(redis_client, decision.provider, decision.key_id, cooldown_seconds=30)
            tried_so_far.add(decision.key_id)
            continue

        try:
            completion, snapshot = await adapter.dispatch(
                prompt,
                model=decision.model,
                key_id=decision.key_id,
            )
            await write_quota_to_redis(redis_client, decision.provider, decision.key_id, snapshot)
            attempt_entry["status"] = "success"
            return {
                "completion": completion,
                "provider": decision.provider,
                "model": decision.model,
                "attempts": attempts,
            }
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 429:
                retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
                await _mark_key_cooling_down(
                    redis_client,
                    decision.provider,
                    decision.key_id,
                    cooldown_seconds=retry_after or 30,
                )
            else:
                await _mark_key_cooling_down(redis_client, decision.provider, decision.key_id, cooldown_seconds=30)
            attempt_entry["status"] = "failed"
            attempt_entry["error"] = f"HTTPStatusError: {status_code}"
            tried_so_far.add(decision.key_id)
        except Exception as exc:
            await _mark_key_cooling_down(redis_client, decision.provider, decision.key_id, cooldown_seconds=30)
            attempt_entry["status"] = "failed"
            attempt_entry["error"] = f"{type(exc).__name__}: {exc}"
            tried_so_far.add(decision.key_id)

    raise AllProvidersExhaustedError(
        message="All candidate providers exhausted after retries.",
        attempts=attempts,
    )


async def _mark_key_cooling_down(redis_client, provider: str, key_id: str, cooldown_seconds: int) -> None:
    """Mark a key as cooling_down with a manual reset timestamp."""
    now = time.time()
    redis_key = f"quota:{provider}:{key_id}"
    await redis_client.hset(
        redis_key,
        mapping={
            "provider": provider,
            "key_id": key_id,
            "status": "cooling_down",
            "reset_requests_at": now + cooldown_seconds,
            "last_updated": now,
        },
    )


def _parse_retry_after(value: str | None) -> int | None:
    """Parse Retry-After seconds header into integer when possible."""
    if value is None:
        return None
    try:
        seconds = int(float(value.strip()))
        return max(seconds, 1)
    except (TypeError, ValueError):
        return None
