"""FastAPI app exposing CipherAI routing and telemetry endpoints."""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from redis import asyncio as redis_async

from adapters.gemini_adapter import GeminiAdapter
from adapters.groq_adapter import GroqAdapter
from adapters.mistral_adapter import MistralAdapter
from orchestrator.pipeline import handle_prompt


class RouteRequest(BaseModel):
    """Request payload for /route endpoint."""

    prompt: str = Field(..., min_length=1)


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(title="CipherAI", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def on_startup() -> None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        app.state.redis = redis_async.from_url(redis_url, decode_responses=True)
        app.state.adapters = _build_adapters(app.state.redis)

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await app.state.redis.aclose()
        for adapter in app.state.adapters.values():
            client = getattr(adapter, "client", None)
            if client is not None:
                await client.aclose()

    @app.post("/route")
    async def route_prompt(payload: RouteRequest) -> dict[str, Any]:
        """Route prompt through classifier + decision engine + breaker pipeline."""
        return await handle_prompt(payload.prompt, app.state.redis, app.state.adapters)

    @app.get("/status")
    async def get_status() -> list[dict[str, Any]]:
        """Return full quota hash snapshots for all tracked keys."""
        return await _read_all_quota_status(app.state.redis)

    @app.websocket("/ws/telemetry")
    async def telemetry_socket(websocket: WebSocket) -> None:
        """Stream status snapshots and Redis telemetry updates to clients."""
        await websocket.accept()
        pubsub = app.state.redis.pubsub()
        await pubsub.subscribe("telemetry_updates")
        try:
            initial = await _read_all_quota_status(app.state.redis)
            await websocket.send_json({"type": "initial_status", "data": initial})
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("data"):
                    data = message["data"]
                    try:
                        parsed = json.loads(data) if isinstance(data, str) else data
                    except json.JSONDecodeError:
                        parsed = {"type": "raw_message", "data": data}
                    await websocket.send_json(parsed)
        except WebSocketDisconnect:
            pass
        finally:
            await pubsub.unsubscribe("telemetry_updates")
            await pubsub.aclose()

    return app


def _build_adapters(redis_client) -> dict[str, Any]:
    """Instantiate provider adapters from environment variables."""
    adapters: dict[str, Any] = {}
    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    mistral_key = os.getenv("MISTRAL_API_KEY")

    if groq_key:
        adapters["groq"] = GroqAdapter(api_key=groq_key)
    if gemini_key:
        adapters["gemini"] = GeminiAdapter(api_key=gemini_key, redis_client=redis_client)
    if mistral_key:
        adapters["mistral"] = MistralAdapter(api_key=mistral_key, redis_client=redis_client)
    return adapters


async def _read_all_quota_status(redis_client) -> list[dict[str, Any]]:
    """Scan Redis quota keys and return their hash payloads."""
    status: list[dict[str, Any]] = []
    async for key in redis_client.scan_iter(match="quota:*"):
        data = await redis_client.hgetall(key)
        status.append({"redis_key": key, **data})
    return status


app = create_app()
