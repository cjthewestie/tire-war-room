"""Audit N1: LLM_TIMEOUT_S must bound the whole streaming commander call, not only the gap between
two chunks. A fake OpenAI-compatible SSE server (127.0.0.1 only) drips a valid plan one chunk per
second for ~7 s; llm_timeout_s=2. Temporary audit test; reuses tests/test_agent helpers."""
from __future__ import annotations

import asyncio
import json
import time

from tests.test_agent import _llm_plan_json, _req_plan
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import OpenAICommander
from warroom.config import Settings
from warroom.contracts import LLMMode

PORT = 18733          # checked free with `ss -ltn` before use; bound to 127.0.0.1 only


def _events(text: str, n: int) -> list[bytes]:
    size = -(-len(text) // n)
    out = []
    for i in range(0, len(text), size):
        o = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
             "choices": [{"index": 0, "delta": {"content": text[i:i + size]}, "finish_reason": None}]}
        out.append(f"data: {json.dumps(o, ensure_ascii=False)}\n\n".encode())
    out.append(b"data: [DONE]\n\n")
    return out


async def _serve(gap_s: float, n_chunks: int):
    events = _events(_llm_plan_json(), n_chunks)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            if length:
                await reader.readexactly(length)
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                         b"transfer-encoding: chunked\r\nconnection: close\r\n\r\n")
            await writer.drain()
            for ev in events:
                await asyncio.sleep(gap_s)
                writer.write(f"{len(ev):x}\r\n".encode() + ev + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
    return await asyncio.start_server(handle, "127.0.0.1", PORT)


async def _call(monkeypatch, gap_s: float, n_chunks: int):
    monkeypatch.setenv("LLM_SERVER_KIND", "vllm")       # no probe of /version
    server = await _serve(gap_s, n_chunks)
    try:
        s = Settings(llm_mode=LLMMode.LLM, base_url=f"http://127.0.0.1:{PORT}/v1", model="m",
                     llm_timeout_s=2.0, llm_fallback_to_mock=True)
        cmd = OpenAICommander(s)                          # production path: SDK-owned httpx client
        t0 = time.perf_counter()
        plan, info = await cmd.plan(_req_plan(page11_anomaly()))
        return time.perf_counter() - t0, info, cmd
    finally:
        server.close()
        await server.wait_closed()


async def test_n1_control_a_single_gap_over_timeout_does_time_out(monkeypatch):
    """Control: one 3 s gap > 2 s timeout -> ReadTimeout -> fallback (proves the server/timeout wiring)."""
    took, info, cmd = await _call(monkeypatch, gap_s=3.0, n_chunks=2)
    print(f"\n[N1-control] took={took:.2f}s fell_back={info.fell_back_to_mock} cause={cmd.last_cause} "
          f"err={cmd.last_error}")
    assert info.fell_back_to_mock and cmd.last_cause == "timeout"


async def test_n1_total_stream_time_is_bounded_by_llm_timeout(monkeypatch):
    """1 s between chunks, 7 chunks + [DONE] (~8 s total), timeout 2 s: the call must not succeed."""
    took, info, cmd = await _call(monkeypatch, gap_s=1.0, n_chunks=7)
    print(f"\n[N1] took={took:.2f}s fell_back={info.fell_back_to_mock} valid_json={info.valid_json} "
          f"cause={cmd.last_cause} total_ms={info.total_ms:.0f}")
    assert took >= 6.0
    assert info.fell_back_to_mock, \
        f"stream ran {took:.1f}s > LLM_TIMEOUT_S=2s and the model's plan was accepted (valid_json={info.valid_json})"
