"""Groq adapter with header-based quota tracking."""

from __future__ import annotations

import re
import time

import httpx

from adapters.base import ProviderAdapter, QuotaSnapshot


class GroqAdapter(ProviderAdapter):
    """Dispatch prompts to Groq's OpenAI-compatible endpoint."""

    provider_name = "groq"

    def __init__(self, api_key: str, base_url: str = "https://api.groq.com/openai/v1") -> None:
        self.api_key = api_key
        self.client = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def dispatch(self, prompt: str, **kwargs) -> tuple[str, QuotaSnapshot]:
        """Send a chat completion request and parse quota headers."""
        resp = await self.client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": kwargs.get("model", "llama-3.1-70b-versatile"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": kwargs.get("temperature", 0.3),
            },
        )
        resp.raise_for_status()
        snapshot = self.parse_headers(dict(resp.headers))
        if snapshot is None:
            snapshot = QuotaSnapshot(
                remaining_requests=None,
                limit_requests=None,
                remaining_tokens=None,
                limit_tokens=None,
                reset_requests_at=None,
                reset_tokens_at=None,
                tracking_mode="header",
            )
        completion = resp.json()["choices"][0]["message"]["content"]
        return completion, snapshot

    def parse_headers(self, headers: dict) -> QuotaSnapshot | None:
        """Extract Groq x-ratelimit headers when present."""
        if "x-ratelimit-remaining-requests" not in headers:
            return None
        now = time.time()
        return QuotaSnapshot(
            remaining_requests=int(headers["x-ratelimit-remaining-requests"]),
            limit_requests=int(headers.get("x-ratelimit-limit-requests", "0") or 0),
            remaining_tokens=int(headers.get("x-ratelimit-remaining-tokens", "0") or 0),
            limit_tokens=int(headers.get("x-ratelimit-limit-tokens", "0") or 0),
            reset_requests_at=now + _parse_reset_seconds(headers.get("x-ratelimit-reset-requests")),
            reset_tokens_at=now + _parse_reset_seconds(headers.get("x-ratelimit-reset-tokens")),
            tracking_mode="header",
        )


def _parse_reset_seconds(value: str | None) -> float:
    """Convert reset header values such as '3s' or '1m26.4' to seconds."""
    if not value:
        return 0.0
    raw = value.strip().lower()

    # Fast path for plain number strings (assume seconds).
    try:
        return float(raw)
    except ValueError:
        pass

    # Support composite units like "1m26.4s" or "1m26.4".
    total = 0.0
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", raw):
        number = float(amount)
        if unit == "h":
            total += number * 3600
        elif unit == "m":
            total += number * 60
        elif unit == "ms":
            total += number / 1000
        else:
            total += number

    return total
