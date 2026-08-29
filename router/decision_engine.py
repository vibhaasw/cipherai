"""Provider selection logic based on benchmark ranking and Redis health."""

from __future__ import annotations

from dataclasses import dataclass
import time

from classifier.slm_classifier import ClassificationResult
from config.benchmark_matrix import BENCHMARK_MATRIX


@dataclass
class RoutingDecision:
    """Result of routing decision without dispatch side effects."""

    provider: str
    model: str
    key_id: str
    rank: int
    reason: str


async def select_provider(
    classification: ClassificationResult,
    redis_client,
    exclude_key_ids: set[str] | None = None,
) -> RoutingDecision | None:
    """
    Select the highest-ranked healthy candidate from Redis state.

    This function only reads benchmark config and Redis quota hashes.
    """
    excluded = exclude_key_ids or set()
    domain = classification.domain if classification.domain in BENCHMARK_MATRIX else "GENERAL"
    candidates = BENCHMARK_MATRIX.get(domain, BENCHMARK_MATRIX["GENERAL"])

    skipped: list[str] = []
    for candidate in candidates:
        provider = str(candidate["provider"])
        model = str(candidate["model"])
        key_id = str(candidate["key_id"])
        rank = int(candidate["rank"])

        if key_id in excluded:
            skipped.append(f"Rank #{rank} ({model}) skipped: key already tried.")
            continue

        redis_key = f"quota:{provider}:{key_id}"
        quota_hash = await redis_client.hgetall(redis_key)

        if not quota_hash:
            reason = _build_reason(skipped, rank, model, "healthy (no prior quota data) — selected.")
            return RoutingDecision(provider=provider, model=model, key_id=key_id, rank=rank, reason=reason)

        status = str(quota_hash.get("status", "healthy"))
        if status == "cooling_down" and _cooldown_elapsed(quota_hash):
            status = "healthy"
            skipped.append(f"Rank #{rank} ({model}) cooldown elapsed, re-eligible.")

        if status != "healthy":
            usage_hint = _usage_hint(quota_hash)
            skipped.append(f"Rank #{rank} ({model}) skipped: status={status}{usage_hint}.")
            continue

        usage_hint = _usage_hint(quota_hash)
        reason = _build_reason(skipped, rank, model, f"healthy{usage_hint} — selected.")
        return RoutingDecision(provider=provider, model=model, key_id=key_id, rank=rank, reason=reason)

    return None


def _usage_hint(quota_hash: dict) -> str:
    """Compute optional usage percentage hint from quota fields."""
    try:
        remaining = float(quota_hash.get("remaining_requests", -1))
        limit = float(quota_hash.get("limit_requests", -1))
        if remaining < 0 or limit <= 0:
            return ""
        used_pct = int(round((1 - (remaining / limit)) * 100))
        return f", usage={used_pct}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def _build_reason(skipped: list[str], rank: int, model: str, selected_text: str) -> str:
    """Build human-readable explainability string for dashboard display."""
    parts = skipped + [f"Rank #{rank} ({model}) {selected_text}"]
    return " ".join(parts)


def _cooldown_elapsed(quota_hash: dict) -> bool:
    """Return True if reset_requests_at has passed current time."""
    try:
        reset_at = float(quota_hash.get("reset_requests_at", "0"))
        if reset_at <= 0:
            return False
        return time.time() >= reset_at
    except (TypeError, ValueError):
        return False
