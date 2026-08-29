"""Mistral adapter with conservative self-tracked quotas."""

from __future__ import annotations

import time

import httpx

from adapters.base import ProviderAdapter, QuotaSnapshot

STATIC_LIMITS = {"rpm": 10, "tpm": 120_000}


class MistralAdapter(ProviderAdapter):
    """Dispatch prompts to Mistral's chat API endpoint."""

    provider_name = "mistral"

    def __init__(self, api_key: str, redis_client, base_url: str = "https://api.mistral.ai/v1") -> None:
        self.api_key = api_key
        self.redis = redis_client
        self.client = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def dispatch(self, prompt: str, **kwargs) -> tuple[str, QuotaSnapshot]:
        """Call Mistral and update local rolling-window counters."""
        resp = await self.client.post(
            "/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": kwargs.get("model", "mistral-large-latest"),
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        completion = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        used_tokens = int(usage.get("total_tokens") or max(len(prompt) // 4, 1))

        key_id = kwargs["key_id"]
        rkey = f"quota:mistral:{key_id}"
        used_requests = await self.redis.hincrby(rkey, "used_requests_window", 1)
        used_tokens_total = await self.redis.hincrby(rkey, "used_tokens_window", used_tokens)
        await self.redis.expire(rkey, 60)

        return completion, QuotaSnapshot(
            remaining_requests=max(STATIC_LIMITS["rpm"] - int(used_requests), 0),
            limit_requests=STATIC_LIMITS["rpm"],
            remaining_tokens=max(STATIC_LIMITS["tpm"] - int(used_tokens_total), 0),
            limit_tokens=STATIC_LIMITS["tpm"],
            reset_requests_at=time.time() + 60.0,
            reset_tokens_at=time.time() + 60.0,
            tracking_mode="self",
        )

    def parse_headers(self, headers: dict) -> QuotaSnapshot | None:
        """Mistral quota headers are not normalized in this prototype."""
        return None
