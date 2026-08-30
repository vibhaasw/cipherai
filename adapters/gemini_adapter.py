"""Gemini adapter with self-tracked quota counters."""

from __future__ import annotations

import time

import google.generativeai as genai

from adapters.base import ProviderAdapter, QuotaSnapshot

STATIC_LIMITS = {"rpm": 15, "tpm": 1_000_000}


class GeminiAdapter(ProviderAdapter):
    """Dispatch prompts to Gemini and self-track minute windows."""

    provider_name = "gemini"

    def __init__(self, api_key: str, redis_client) -> None:
        self.api_key = api_key
        self.redis = redis_client
        genai.configure(api_key=api_key)
        self.last_finish_reason: str | None = None

    async def dispatch(self, prompt: str, **kwargs) -> tuple[str, QuotaSnapshot]:
        """Call Gemini and update self-tracked usage counters."""
        model_name = kwargs.get("model", "gemini-1.5-pro")
        model = genai.GenerativeModel(model_name=model_name)
        generation_config = None
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is not None:
            generation_config = {"max_output_tokens": int(max_tokens)}
        response = await model.generate_content_async(prompt, generation_config=generation_config)
        completion = (response.text or "").strip()
        finish_reason = None
        candidates = getattr(response, "candidates", None)
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
        if finish_reason is not None:
            self.last_finish_reason = str(finish_reason).lower()
        else:
            self.last_finish_reason = None

        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", None)
        completion_tokens = getattr(usage, "candidates_token_count", None)
        used_tokens = (prompt_tokens or len(prompt) // 4) + (completion_tokens or max(len(completion) // 4, 1))

        credential_ref = kwargs.get("credential_ref", kwargs["key_id"])
        rkey = f"quota:gemini:{credential_ref}"

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
        """Gemini free-tier headers are not used for quota parsing."""
        return None
