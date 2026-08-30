"""Dispatch loop with retry, cooldown, and provider failover."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

import httpx

from adapters.base import ProviderAdapter
from classifier.slm_classifier import ClassificationResult
from router.decision_engine import select_provider
from utils.quota_store import write_quota_to_redis

logger = logging.getLogger("cipherai.router.circuit_breaker")


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
        decision, evaluated_candidates = await select_provider(
            classification=classification,
            redis_client=redis_client,
            exclude_key_ids=tried_so_far,
        )
        if decision is None:
            logger.info("final_outcome status=no_healthy_provider attempts=%s", len(attempts))
            raise NoHealthyProviderError(
                message="No healthy provider/key available for this request.",
                attempts=attempts + evaluated_candidates,
            )

        attempt_entry: dict[str, Any] = {
            "provider": decision.provider,
            "model": decision.model,
            "key_id": decision.key_id,
            "credential_ref": decision.credential_ref,
            "rank": decision.rank,
            "reason": decision.reason,
            "decision_reason": decision.reason,
            "status": "attempting",
        }
        attempts.append(attempt_entry)

        adapter = adapters.get(decision.provider)
        if adapter is None:
            attempt_entry["status"] = "failed"
            attempt_entry["error"] = f"Adapter not configured for provider '{decision.provider}'."
            await _mark_key_cooling_down(
                redis_client,
                decision.provider,
                decision.credential_ref,
                decision.key_id,
                cooldown_seconds=30,
            )
            logger.info(
                "dispatch_attempt provider=%s model=%s key_id=%s status=failure latency_s=0 token_count=- reason=adapter_not_configured",
                decision.provider,
                decision.model,
                decision.key_id,
            )
            tried_so_far.add(decision.key_id)
            continue

        try:
            start = time.perf_counter()
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
            attempt_entry["status"] = "success"
            token_count = _token_count_from_snapshot(snapshot)
            logger.info(
                "dispatch_attempt provider=%s model=%s key_id=%s status=success latency_s=%.3f token_count=%s",
                decision.provider,
                decision.model,
                decision.key_id,
                latency_seconds,
                token_count if token_count is not None else "-",
            )
            logger.info(
                "final_outcome status=success provider=%s model=%s key_id=%s attempts=%s",
                decision.provider,
                decision.model,
                decision.key_id,
                len(attempts),
            )
            finish_reason = str(getattr(adapter, "last_finish_reason", "") or "").lower() or None
            return {
                "completion": completion,
                "provider": decision.provider,
                "model": decision.model,
                "attempts": attempts,
                "finish_reason": finish_reason,
            }
        except httpx.HTTPStatusError as exc:
            latency_seconds = time.perf_counter() - start
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 429:
                retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
                await _mark_key_cooling_down(
                    redis_client,
                    decision.provider,
                    decision.credential_ref,
                    decision.key_id,
                    cooldown_seconds=retry_after or 30,
                )
            else:
                await _mark_key_cooling_down(
                    redis_client,
                    decision.provider,
                    decision.credential_ref,
                    decision.key_id,
                    cooldown_seconds=30,
                )
            attempt_entry["status"] = "failed"
            attempt_entry["error"] = f"HTTPStatusError: {status_code}"
            logger.info(
                "dispatch_attempt provider=%s model=%s key_id=%s status=failure latency_s=%.3f token_count=- reason=http_status_%s",
                decision.provider,
                decision.model,
                decision.key_id,
                latency_seconds,
                status_code,
            )
            tried_so_far.add(decision.key_id)
        except Exception as exc:
            latency_seconds = time.perf_counter() - start
            await _mark_key_cooling_down(
                redis_client,
                decision.provider,
                decision.credential_ref,
                decision.key_id,
                cooldown_seconds=30,
            )
            attempt_entry["status"] = "failed"
            attempt_entry["error"] = f"{type(exc).__name__}: {exc}"
            logger.info(
                "dispatch_attempt provider=%s model=%s key_id=%s status=failure latency_s=%.3f token_count=- reason=%s",
                decision.provider,
                decision.model,
                decision.key_id,
                latency_seconds,
                type(exc).__name__,
            )
            tried_so_far.add(decision.key_id)

    logger.info("final_outcome status=all_providers_exhausted attempts=%s", len(attempts))
    raise AllProvidersExhaustedError(
        message="All candidate providers exhausted after retries.",
        attempts=attempts,
    )


async def _mark_key_cooling_down(
    redis_client,
    provider: str,
    credential_ref: str,
    key_id: str,
    cooldown_seconds: int,
) -> None:
    """Mark a key as cooling_down with a manual reset timestamp."""
    now = time.time()
    redis_key = f"quota:{provider}:{credential_ref}"
    await redis_client.hset(
        redis_key,
        mapping={
            "provider": provider,
            "credential_ref": credential_ref,
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


def _token_count_from_snapshot(snapshot) -> int | None:
    """Best-effort used-token estimate from quota snapshot fields."""
    remaining_tokens = snapshot.remaining_tokens
    limit_tokens = snapshot.limit_tokens
    if remaining_tokens is None or limit_tokens is None:
        return None
    if remaining_tokens < 0 or limit_tokens < 0:
        return None
    return max(limit_tokens - remaining_tokens, 0)
