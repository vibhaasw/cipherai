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
        self.last_finish_reason: str | None = None

    async def dispatch(self, prompt: str, **kwargs) -> tuple[str, QuotaSnapshot]:
        """Call Mistral and update local rolling-window counters."""
        payload = {
            "model": kwargs.get("model", "mistral-large-latest"),
            "messages": [{"role": "user", "content": prompt}],
        }
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        resp = await self.client.post(
            "/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        payload = resp.json()
        choice = payload["choices"][0]
        completion = choice["message"]["content"]
        self.last_finish_reason = str(choice.get("finish_reason") or "").lower() or None
        usage = payload.get("usage", {})
        used_tokens = int(usage.get("total_tokens") or max(len(prompt) // 4, 1))

        credential_ref = kwargs.get("credential_ref", kwargs["key_id"])
        rkey = f"quota:mistral:{credential_ref}"
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
