"""後台 狀態 live metrics (warroom/api/metrics.py): the parsers on recorded output, the Spark path (nvidia-smi + vLLM
/metrics) on recorded samples, the meter over llm_call rows, and the endpoint's access rule and secrets."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from warroom.api import Runtime, create_app
from warroom.api import metrics as M
from warroom.config import Settings
from warroom.contracts import AuditKind, LLMMode

FIX = Path(__file__).parent / "fixtures" / "metrics"
PIN = "metrics-pin-5678"


@pytest.fixture
def rt(tmp_path) -> Runtime:
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 sop_dir=tmp_path, api_key="sk-secret-should-never-leak", nvidia_api_key="nvapi-secret-never")
    return Runtime(s, use_real=False, run_loop=False)


def _client(rt, host: str = "testclient") -> TestClient:
    return TestClient(create_app(rt, supervisor_pin=PIN), client=(host, 50000))


# ---------------------------------------------------------------- parsers
def test_nvidia_smi_spark_unified_memory_is_none():
    [g] = M.parse_nvidia_smi((FIX / "nvidia_smi_spark.csv").read_text())
    assert g == {"name": "NVIDIA GB10", "util_pct": 87.0, "temp_c": 63.0, "mem_used_mb": None, "mem_total_mb": None,
                 "power_w": 31.42}


def test_nvidia_smi_discrete_gpu():
    [g] = M.parse_nvidia_smi("NVIDIA H100 80GB HBM3, 12, 40, 61234, 81559, 98.1\n")
    assert g["mem_used_mb"] == 61234 and g["mem_total_mb"] == 81559


def test_compute_apps_sums_processes():
    assert M.parse_compute_apps((FIX / "nvidia_smi_compute_apps_spark.csv").read_text()) == 58124 * 2**20
    assert M.parse_compute_apps("") is None


def test_vm_stat_mac():
    v = M.parse_vm_stat((FIX / "vm_stat_mac.txt").read_text())
    assert v["wired_down"] > 0 and v["wired_down"] % 16384 == 0
    assert "free" in v and "active" in v


def test_ollama_ps_mac():
    data = json.loads((FIX / "ollama_ps_mac.json").read_text())
    p = M.parse_ollama_ps(data, "nemotron-3.5-lightning:30b-a3b-nvfp4")
    assert p["loaded"] and p["size"] > 30e9 and p["size_vram"] == p["size"]
    assert not M.parse_ollama_ps(data, "other:7b")["loaded"]


def test_vllm_metrics_spark():
    v = M.vllm_view(M.parse_prometheus((FIX / "vllm_metrics_spark.txt").read_text()))
    assert v == {"cache_pct": 13.7, "running": 1.0, "waiting": 2.0, "gen_tokens_total": 18342.0}
    assert M.vllm_view(M.parse_prometheus("vllm:kv_cache_usage_perc 0.5\n"))["cache_pct"] == 50.0


# ---------------------------------------------------------------- Spark path end to end (recorded samples, no GPU)
class _Resp:
    def __init__(self, text: str):
        self.text = text

    def json(self):
        return json.loads(self.text)


class _Http:
    def __init__(self, text: str):
        self.texts = [text]

    async def get(self, url: str):
        assert url.endswith("/metrics")
        return _Resp(self.texts[-1])


def test_spark_path_reads_nvidia_smi_and_vllm(rt, monkeypatch):
    monkeypatch.setattr(type(rt), "llm_mode", property(lambda self: LLMMode.LLM))
    rt.settings.base_url = "http://10.0.0.5:8000/v1"
    text = (FIX / "vllm_metrics_spark.txt").read_text()
    http = _Http(text)
    out = {"--query-gpu": (FIX / "nvidia_smi_spark.csv").read_text(),
           "--query-compute-apps": (FIX / "nvidia_smi_compute_apps_spark.csv").read_text()}
    s = M.MetricsSampler(lambda: rt, http=http, run=lambda *cmd: next(v for k, v in out.items() if cmd[1].startswith(k)))
    s.has_smi, s.kind = True, "vllm"
    first = asyncio.run(s.sample())
    g = first["machine"]["gpus"][0]
    assert g["util_pct"]["v"] == 87.0 and g["temp_c"]["v"] == 63.0
    assert g["mem_used"]["v"] is None and "統一記憶體" in g["mem_used"]["na"]
    m = first["model"]
    assert m["server"] == "vllm" and m["host"] == "10.0.0.5:8000" and m["loaded"]["v"] is True
    assert m["cache_pct"]["v"] == 13.7 and m["vllm_running"]["v"] == 1.0 and m["vllm_waiting"]["v"] == 2.0
    assert m["memory"]["v"] == 58124 * 2**20
    http.texts.append(text.replace("18342.0", "18642.0"))       # 300 more tokens by the next sample
    s.gen_prev = (s.gen_prev[0] - 3.0, s.gen_prev[1])          # three seconds ago
    second = asyncio.run(s.sample())
    assert 90 <= second["model"]["tps_live"]["v"] <= 101
    assert "generation_tokens_total" in second["model"]["tps_live"]["src"]


# ---------------------------------------------------------------- the meter over llm_call rows
def test_llm_calls_latency_tps_fallbacks_and_calls_per_minute(rt):
    rt.audit.append(AuditKind.LLM_CALL, "commander", {"mode": "llm", "total_ms": 4000.0, "completion_tokens": 200})
    rt.audit.append(AuditKind.LLM_CALL, "commander", {"mode": "mock", "total_ms": 1.0})        # rules: not a call
    s = M.MetricsSampler(lambda: rt)
    s.has_smi, s.mac = False, False
    m = asyncio.run(s.sample())["model"]
    assert m["calls_seen"]["v"] == 1 and m["calls_last_min"]["v"] == 0      # already on the chain: wall time unknown
    assert m["tps_last"]["v"] == 50.0
    rt.audit.append(AuditKind.LLM_CALL, "main_agent", {"phase": "chat", "total_ms": 2000.0, "completion_tokens": 50})
    rt.audit.append(AuditKind.LLM_CALL, "commander", {"mode": "llm", "total_ms": 9000.0, "fell_back_to_mock": True})
    rt.audit.append(AuditKind.LLM_CALL, "ask", {"mode": "llm", "total_ms": 30.0, "error": "TimeoutError"})
    rt.audit.append(AuditKind.LLM_CALL, "commander", {"mode": "llm", "fell_back_to_mock": True, "cause": "budget",
                                                      "error": "budget"})
    m = asyncio.run(s.sample())["model"]
    assert m["budget_refused"]["v"] == 1
    assert m["calls_last_min"]["v"] == 4 and m["fallbacks"]["v"] == 1 and m["errors"]["v"] == 2
    assert m["p50_ms"]["v"] == 4000 and m["p90_ms"]["v"] == 8000            # 2000 / 4000 / 9000, errors left out
    assert m["tps_last"]["v"] == 25.0


def test_queue_shows_running_work(rt):
    q = rt.builder.model_queue

    async def go():
        s = M.MetricsSampler(lambda: rt)
        async with q.slot(4, "main_agent", None, "對話"):
            return (await s.sample())["model"]["queue"]
    v = asyncio.run(go())
    assert v["running"][0]["work"] == "對話" and v["waiting"] == 0


# ---------------------------------------------------------------- endpoint: access and secrets
def test_metrics_needs_factory_network_and_supervisor(rt):
    with _client(rt, host="8.8.8.8") as c:
        assert c.get("/api/admin/metrics", headers={"X-Supervisor-Pin": PIN}).status_code == 403
    with _client(rt) as c:
        assert c.get("/api/admin/metrics", headers={"X-Forwarded-For": "1.1.1.1", "X-Supervisor-Pin": PIN}).status_code == 403
    with _client(rt, host="192.168.1.20") as c:
        assert c.get("/api/admin/metrics").status_code == 401
        assert c.get("/api/admin/metrics", headers={"X-Supervisor-Pin": "wrong"}).status_code == 401


def test_metrics_shape_and_no_secrets(rt):
    with _client(rt) as c:
        r = c.get("/api/admin/metrics", headers={"X-Supervisor-Pin": PIN})
    assert r.status_code == 200
    body = r.json()
    for k in ("cpu_pct", "ram_used", "disk_free", "rss", "uptime_s"):
        assert "src" in body["machine"][k]
    assert body["history"]["t"] and len(body["history"]["cpu"]) == len(body["history"]["t"])
    text = r.text
    assert "sk-secret" not in text and "nvapi-secret" not in text and str(rt.settings.audit_path) not in text
