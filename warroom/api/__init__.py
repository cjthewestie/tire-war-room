"""warroom/api — FastAPI server: REST (contracts.REST_ENDPOINTS) + one WebSocket /ws relaying the
bus. Also owns Runtime (wires sim → safety → detect → state machine) and the main loop.
Owner: api builder. Public: create_app, Runtime. Run: uv run python -m warroom.api"""
from warroom.api.app import create_app
from warroom.api.runtime import Runtime

__all__ = ["create_app", "Runtime"]
