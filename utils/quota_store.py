"""Redis quota persistence helpers used by router and API."""

from __future__ import annotations

import json
import time

from adapters.base import QuotaSnapshot


async def write_quota_to_redis(redis_client, provider: str, key_id: str, snap: QuotaSnapshot) -> None:
    """Persist normalized quota snapshot and publish telemetry update."""
    pct_used = 0.0
    if snap.limit_requests and snap.limit_requests > 0 and snap.remaining_requests is not None:
        pct_used = 1 - (snap.remaining_requests / snap.limit_requests)

    status = "healthy"
    if pct_used >= 1:
        status = "cooling_down"
    elif pct_used >= 0.85:
        status = "near_cap"

    rkey = f"quota:{provider}:{key_id}"
    mapping = {
        "provider": provider,
        "key_id": key_id,
        "remaining_requests": snap.remaining_requests if snap.remaining_requests is not None else -1,
        "limit_requests": snap.limit_requests if snap.limit_requests is not None else -1,
        "remaining_tokens": snap.remaining_tokens if snap.remaining_tokens is not None else -1,
        "limit_tokens": snap.limit_tokens if snap.limit_tokens is not None else -1,
        "reset_requests_at": snap.reset_requests_at if snap.reset_requests_at is not None else 0.0,
        "reset_tokens_at": snap.reset_tokens_at if snap.reset_tokens_at is not None else 0.0,
        "tracking_mode": snap.tracking_mode,
        "status": status,
        "last_updated": time.time(),
    }
    await redis_client.hset(rkey, mapping=mapping)
    await redis_client.publish("telemetry_updates", json.dumps({"type": "quota_update", "data": mapping}))
