"""Provider selection logic based on benchmark ranking and Redis health."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time

from classifier.slm_classifier import ClassificationResult
from config.benchmark_matrix import BENCHMARK_MATRIX

logger = logging.getLogger("cipherai.router.decision_engine")


@dataclass
class RoutingDecision:
    """Result of routing decision without dispatch side effects."""

    provider: str
    model: str
    key_id: str
    credential_ref: str
    rank: int
    reason: str


async def select_provider(
    classification: ClassificationResult,
    redis_client,
    exclude_key_ids: set[str] | None = None,
) -> tuple[RoutingDecision | None, list[dict]]:
    """
    Select the highest-ranked healthy candidate from Redis state.

    This function only reads benchmark config and Redis quota hashes.
    """
    excluded = exclude_key_ids or set()
    domain = classification.domain if classification.domain in BENCHMARK_MATRIX else "GENERAL"
    candidates = BENCHMARK_MATRIX.get(domain, BENCHMARK_MATRIX["GENERAL"])

    skipped: list[str] = []
    evaluated: list[dict] = []
    for candidate in candidates:
        provider = str(candidate["provider"])
        model = str(candidate["model"])
        key_id = str(candidate["key_id"])
        credential_ref = str(candidate["credential_ref"])
        rank = int(candidate["rank"])

        if key_id in excluded:
            reason = "key already tried"
            logger.info(
                "candidate_evaluated provider=%s model=%s key_id=%s rank=%s healthy=false reason=key_already_tried",
                provider,
                model,
                key_id,
                rank,
            )
            skipped.append(f"Rank #{rank} ({model}) skipped: {reason}.")
            evaluated.append(
                {
                    "provider": provider,
                    "model": model,
                    "key_id": key_id,
                    "credential_ref": credential_ref,
                    "rank": rank,
                    "status": "skipped",
                    "reason": reason,
                }
            )
            continue

        redis_key = f"quota:{provider}:{credential_ref}"
        quota_hash = await redis_client.hgetall(redis_key)

        if not quota_hash:
            selected_reason = "healthy (no prior quota data)"
            logger.info(
                "candidate_evaluated provider=%s model=%s key_id=%s rank=%s healthy=true reason=no_quota_data_selected",
                provider,
                model,
                key_id,
                rank,
            )
            evaluated.append(
                {
                    "provider": provider,
                    "model": model,
                    "key_id": key_id,
                    "credential_ref": credential_ref,
                    "rank": rank,
                    "status": "selected",
                    "reason": selected_reason,
                }
            )
            reason = _build_reason(skipped, rank, model, "healthy (no prior quota data) — selected.")
            return RoutingDecision(
                provider=provider,
                model=model,
                key_id=key_id,
                credential_ref=credential_ref,
                rank=rank,
                reason=reason,
            ), evaluated

        status = str(quota_hash.get("status", "healthy"))
        if status == "cooling_down" and _cooldown_elapsed(quota_hash):
            status = "healthy"
            skipped.append(f"Rank #{rank} ({model}) cooldown elapsed, re-eligible.")

        if status != "healthy":
            usage_hint = _usage_hint(quota_hash)
            reason = f"status={status}{usage_hint}"
            logger.info(
                "candidate_evaluated provider=%s model=%s key_id=%s rank=%s healthy=false reason=status_%s%s",
                provider,
                model,
                key_id,
                rank,
                status,
                usage_hint.replace(" ", "_"),
            )
            skipped.append(f"Rank #{rank} ({model}) skipped: {reason}.")
            evaluated.append(
                {
                    "provider": provider,
                    "model": model,
                    "key_id": key_id,
                    "credential_ref": credential_ref,
                    "rank": rank,
                    "status": "skipped",
                    "reason": reason,
                }
            )
            continue

        usage_hint = _usage_hint(quota_hash)
        selected_reason = f"healthy{usage_hint}"
        logger.info(
            "candidate_evaluated provider=%s model=%s key_id=%s rank=%s healthy=true reason=selected%s",
            provider,
            model,
            key_id,
            rank,
            usage_hint.replace(" ", "_"),
        )
        evaluated.append(
            {
                "provider": provider,
                "model": model,
                "key_id": key_id,
                "credential_ref": credential_ref,
                "rank": rank,
                "status": "selected",
                "reason": selected_reason,
            }
        )
        reason = _build_reason(skipped, rank, model, f"healthy{usage_hint} — selected.")
        return RoutingDecision(
            provider=provider,
            model=model,
            key_id=key_id,
            credential_ref=credential_ref,
            rank=rank,
            reason=reason,
        ), evaluated

    return None, evaluated


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
