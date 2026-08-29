"""Thin orchestrator that wires classification and routed dispatch."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from redis import asyncio as redis_async

from adapters.gemini_adapter import GeminiAdapter
from adapters.groq_adapter import GroqAdapter
from adapters.mistral_adapter import MistralAdapter
from classifier.slm_classifier import classify_prompt
from router.circuit_breaker import (
    AllProvidersExhaustedError,
    NoHealthyProviderError,
    dispatch_with_breaker,
)


async def handle_prompt(prompt: str, redis_client, adapters: dict) -> dict[str, Any]:
    """
    End-to-end prompt pipeline wrapper.

    Returns normalized success/error JSON payload and never leaks raw exceptions.
    """
    try:
        classification = await classify_prompt(prompt)
        result = await dispatch_with_breaker(classification, prompt, redis_client, adapters)
        return {
            "completion": result["completion"],
            "domain": classification.domain,
            "complexity": classification.complexity,
            "provider": result["provider"],
            "model": result["model"],
            "attempts": result["attempts"],
        }
    except NoHealthyProviderError as exc:
        return {
            "error": "NO_HEALTHY_PROVIDER",
            "message": str(exc),
            "attempts": exc.attempts,
        }
    except AllProvidersExhaustedError as exc:
        return {
            "error": "ALL_PROVIDERS_EXHAUSTED",
            "message": str(exc),
            "attempts": exc.attempts,
        }
    except Exception as exc:
        return {
            "error": "PIPELINE_ERROR",
            "message": f"{type(exc).__name__}: {exc}",
            "attempts": [],
        }


def _build_adapters(redis_client) -> dict[str, Any]:
    """Build provider adapters from environment variables."""
    adapters: dict[str, Any] = {}

    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        adapters["groq"] = GroqAdapter(api_key=groq_key)

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        adapters["gemini"] = GeminiAdapter(api_key=gemini_key, redis_client=redis_client)

    mistral_key = os.getenv("MISTRAL_API_KEY")
    if mistral_key:
        adapters["mistral"] = MistralAdapter(api_key=mistral_key, redis_client=redis_client)

    return adapters


async def _demo() -> None:
    """Manual pipeline smoke test entrypoint."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_client = redis_async.from_url(redis_url, decode_responses=True)
    adapters = _build_adapters(redis_client)
    test_prompt = "Write an optimized, thread-safe memory pool module in Rust with full error handling."
    result = await handle_prompt(test_prompt, redis_client, adapters)
    print(result)
    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(_demo())
