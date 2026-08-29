"""Base adapter interfaces for provider-specific model dispatch."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class QuotaSnapshot:
    """Normalized quota snapshot shared by all provider adapters."""

    remaining_requests: int | None
    limit_requests: int | None
    remaining_tokens: int | None
    limit_tokens: int | None
    reset_requests_at: float | None
    reset_tokens_at: float | None
    tracking_mode: str  # "header" or "self"


class ProviderAdapter(ABC):
    """Contract for provider adapters used by the routing pipeline."""

    provider_name: str

    @abstractmethod
    async def dispatch(self, prompt: str, **kwargs) -> tuple[str, QuotaSnapshot]:
        """Send prompt to provider and return completion plus quota snapshot."""

    @abstractmethod
    def parse_headers(self, headers: dict) -> QuotaSnapshot | None:
        """Parse provider headers into a normalized quota snapshot."""
