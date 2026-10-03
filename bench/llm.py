"""Shared OpenAI-compatible streaming client for every benchmark.

One code path for Ollama (Mac), vLLM (DGX Spark) and NVIDIA cloud:
  BASE_URL=http://localhost:11434/v1  MODEL=nemotron-3.5-lightning:30b-a3b-nvfp4   (Ollama, Mac)
  BASE_URL=http://spark:8000/v1       MODEL=nemotron                               (vLLM, Spark)

Structured output is requested with `response_format={"type":"json_schema",...}`, which both vLLM
(>=0.12, where `guided_json` was removed; backend picked server-side with
`--structured-outputs-config.backend xgrammar`) and Ollama accept.
Source: https://docs.vllm.ai/en/latest/features/structured_outputs.html

Nemotron 3.5 reasons by default. Latency paths turn it off:
  vLLM:   extra_body={"chat_template_kwargs": {"enable_thinking": False}}   (model card)
  Ollama: extra_body={"think": False, "reasoning_effort": "none"}
Source: https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

RESULTS_DIR = Path(__file__).resolve().parent / "results"


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    model: str
    api_key: str = "none"
    kind: str = "openai"          # "ollama" | "vllm" | "openai"

    @classmethod
    def from_env(cls, base_url: str | None = None, model: str | None = None) -> "Endpoint":
        url = base_url or os.environ.get("BENCH_BASE_URL") or os.environ.get("BASE_URL") \
            or "http://localhost:11434/v1"
        mdl = model or os.environ.get("BENCH_MODEL") or os.environ.get("MODEL") \
            or "nemotron-3.5-lightning:30b-a3b-nvfp4"
        key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("API_KEY") or "none"
        return cls(url.rstrip("/"), mdl, key, detect_kind(url))

    def client(self, timeout_s: float = 120.0) -> AsyncOpenAI:
        return AsyncOpenAI(base_url=self.base_url, api_key=self.api_key, timeout=timeout_s, max_retries=0)


def detect_kind(base_url: str, probe: bool = True) -> str:
    """ollama | vllm | openai. BENCH_SERVER_KIND overrides; else probe the server, else guess by port."""
    forced = os.environ.get("BENCH_SERVER_KIND")
    if forced:
        return forced
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    if probe:
        try:
            with httpx.Client(timeout=1.5) as c:
                if c.get(root + "/api/version").status_code == 200:
                    return "ollama"
                r = c.get(root + "/version")
                if r.status_code == 200 and "version" in r.text:
                    return "vllm"
        except Exception:  # noqa: BLE001
            pass
    u = urlparse(base_url)
    if u.port in (11434, 11435):
        return "ollama"
    if u.port in (8000, 8001, 8002) or "vllm" in (u.hostname or ""):
        return "vllm"
    return "openai"


def thinking_extra(kind: str, think: bool) -> dict[str, Any]:
    """extra_body that switches Nemotron reasoning on/off for this server kind."""
    if kind == "ollama":
        # Ollama's OpenAI endpoint: `think: false` alone does NOT stop Nemotron reasoning when streaming
        # (measured 2026-10-03, Ollama 0.33.2); `reasoning_effort: "none"` does.
        return {"think": True} if think else {"think": False, "reasoning_effort": "none"}
    if kind == "vllm":
        return {"chat_template_kwargs": {"enable_thinking": think}}
    return {}


def json_schema_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}


@dataclass
class CallResult:
    ok: bool
    ttft_s: float | None           # first visible content token
    ttft_any_s: float | None       # first token of any kind (incl. reasoning)
    total_s: float
    prompt_tokens: int | None
    completion_tokens: int | None
    text: str
    reasoning_chars: int = 0
    error: str | None = None
    chunks: int = 0
    alert_card_s: float | None = None      # watch_alert_card: alert_card object complete in the plan stream
    alert_card: dict[str, Any] | None = None
    stopped_early: bool = False            # stop_on_alert_card closed the stream after the card

    @property
    def decode_tps(self) -> float | None:
        if not self.completion_tokens or self.ttft_any_s is None:
            return None
        gen_s = self.total_s - self.ttft_any_s
        return round(self.completion_tokens / gen_s, 2) if gen_s > 0 else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decode_tps"] = self.decode_tps
        d["text"] = self.text[:2000]
        return d


async def stream_chat(ep: Endpoint, messages: list[dict[str, Any]], *, max_tokens: int = 256,
                      temperature: float = 0.0, response_format: dict[str, Any] | None = None,
                      think: bool = False, extra_body: dict[str, Any] | None = None,
                      timeout_s: float = 120.0, client: AsyncOpenAI | None = None,
                      watch_alert_card: bool = False, stop_on_alert_card: bool = False) -> CallResult:
    """Streams one chat completion and times it. Never raises: errors come back as ok=False.

    watch_alert_card: time the moment the ``alert_card`` object of a streamed CommanderPlan is complete,
    with the commander's own parser (warroom.agent.commander.extract_alert_card) -- that is when the app's
    on_alert fires and the first card reaches the screen. stop_on_alert_card closes the stream right
    after it (the card time does not depend on what follows; usage tokens are then unknown)."""
    extract = None
    if watch_alert_card or stop_on_alert_card:
        from warroom.agent.commander import extract_alert_card as extract
    cli = client or ep.client(timeout_s)
    body = dict(thinking_extra(ep.kind, think))
    body.update(extra_body or {})
    kwargs: dict[str, Any] = dict(model=ep.model, messages=messages, max_tokens=max_tokens,
                                  temperature=temperature, stream=True,
                                  stream_options={"include_usage": True}, extra_body=body or None)
    if response_format:
        kwargs["response_format"] = response_format
    t0 = time.perf_counter()
    ttft = ttft_any = None
    parts: list[str] = []
    reasoning = 0
    usage = None
    chunks = 0
    card_s = None
    card = None
    stopped = False
    text_so_far = ""
    try:
        stream = await cli.chat.completions.create(**kwargs)
        async for ch in stream:
            chunks += 1
            if getattr(ch, "usage", None):
                usage = ch.usage
            if not ch.choices:
                continue
            delta = ch.choices[0].delta
            r = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if r:
                reasoning += len(r)
                if ttft_any is None:
                    ttft_any = time.perf_counter() - t0
            if delta.content:
                now = time.perf_counter() - t0
                if ttft is None:
                    ttft = now
                if ttft_any is None:
                    ttft_any = now
                parts.append(delta.content)
                if extract is not None and card_s is None:
                    text_so_far += delta.content
                    c = extract(text_so_far)
                    if c is not None:
                        card_s, card = now, c.model_dump(mode="json")
                        if stop_on_alert_card:
                            stopped = True
                            await stream.close()
                            break
        total = time.perf_counter() - t0
        text = "".join(parts)
        ctoks = usage.completion_tokens if usage else None
        ptoks = usage.prompt_tokens if usage else None
        return CallResult(True, ttft, ttft_any, total, ptoks, ctoks, text, reasoning, None, chunks,
                          card_s, card, stopped)
    except Exception as e:  # noqa: BLE001 - a benchmark reports failures, it does not crash
        return CallResult(False, ttft, ttft_any, time.perf_counter() - t0, None, None, "".join(parts),
                          reasoning, f"{type(e).__name__}: {e}", chunks, card_s, card, stopped)
    finally:
        if client is None:
            await cli.close()


def parse_json(text: str) -> Any | None:
    """Parses model JSON, tolerating a ```json fence or leading prose (non-constrained servers)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        s, e = t.find("{"), t.rfind("}")
        if 0 <= s < e:
            try:
                return json.loads(t[s:e + 1])
            except json.JSONDecodeError:
                return None
    return None


async def endpoint_info(ep: Endpoint) -> dict[str, Any]:
    """Server identity for the results file (model list, Ollama version / vLLM version if exposed)."""
    info: dict[str, Any] = {"base_url": ep.base_url, "model": ep.model, "kind": ep.kind}
    root = ep.base_url[:-3] if ep.base_url.endswith("/v1") else ep.base_url
    async with httpx.AsyncClient(timeout=5) as c:
        for path, key in (("/api/version", "ollama_version"), ("/version", "vllm_version")):
            try:
                r = await c.get(root + path)
                if r.status_code == 200:
                    info[key] = r.json().get("version")
            except Exception:  # noqa: BLE001
                pass
        if ep.kind == "ollama":
            try:
                r = await c.post(root + "/api/show", json={"model": ep.model})
                if r.status_code == 200:
                    d = r.json().get("details", {})
                    info["ollama_details"] = {k: d.get(k) for k in
                                              ("format", "family", "parameter_size", "quantization_level")}
            except Exception:  # noqa: BLE001
                pass
    return info


def host_info() -> dict[str, Any]:
    import psutil
    return {"hostname": socket.gethostname(), "platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
            "cpu_count": os.cpu_count()}


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower()[:60]


def save_result(name: str, ep: Endpoint | None, data: dict[str, Any], out_dir: Path | None = None,
                tag: str | None = None) -> Path:
    out = out_dir or RESULTS_DIR
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone()
    tag = slug(tag) if tag else (slug(ep.model) if ep else "local")
    path = out / f"{name}-{tag}-{now:%Y%m%d-%H%M%S}.json"
    from bench.provenance import code_stamp as _code_stamp
    payload = {"bench": name, "recorded_at": now.isoformat(), "host": host_info(), "code": _code_stamp(name),
               "host_load_avg_1_5_15": [round(x, 2) for x in os.getloadavg()], **data}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def pct(values: list[float], p: float) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 4)


def summarize(values: list[float | None]) -> dict[str, Any]:
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": round(min(v), 4), "p50": pct(v, 0.5), "p90": pct(v, 0.9),
            "max": round(max(v), 4), "mean": round(sum(v) / len(v), 4)}


def run(coro):  # small helper so every runner has the same entry point
    return asyncio.run(coro)


@dataclass
class Verdict:
    name: str
    target: str
    measured: Any
    passed: bool | None           # None = could not be measured here
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
