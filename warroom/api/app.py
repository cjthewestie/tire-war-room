from __future__ import annotations

from fastapi import FastAPI

from warroom.api.runtime import Runtime


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Routes per contracts.REST_ENDPOINTS, WebSocket /ws, serves web/dist at / and /phone."""
    raise NotImplementedError
