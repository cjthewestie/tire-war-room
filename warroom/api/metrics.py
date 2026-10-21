"""後台 狀態: live machine and model numbers (CJ, 2026-10-22: 「系統部分是要實時報告模型跟機器記憶體運做之類的數據」).

One sampler per app, started by the first GET /api/admin/metrics and stopped after IDLE_STOP_S with nobody asking, so a
server nobody watches pays nothing. Every SAMPLE_S it reads the machine (psutil; nvidia-smi on DGX Spark / Linux GPUs;
vm_stat on macOS), the model server (Ollama /api/ps or vLLM /metrics on the BASE_URL host), the ModelQueue and the new
llm_call rows of the audit chain, and keeps HISTORY_S of points for the sparklines.

Each number carries its source (`src`) for the tooltip. A number the platform cannot give is None with `na` saying why,
and the page shows 「此平台無法取得」 — never a made-up value. No env values, keys or paths leave this module: the model
host is the BASE_URL netloc only, the disk is named by its mount basename."""
from __future__ import annotations

import asyncio
import contextlib
import platform
import re
import shutil
import statistics
import subprocess
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SAMPLE_S = 3.0
HISTORY_S = 600
IDLE_STOP_S = 600
CALLS_KEPT = 300
NA_MAC_GPU = "macOS 的 GPU 使用率與溫度要 sudo powermetrics，伺服器不以 root 執行"
NA_NO_GPU = "找不到 nvidia-smi"
NA_UNIFIED = "統一記憶體：GPU 與 CPU 共用系統記憶體，見「記憶體」"

# who took the ModelQueue slot -> what the supervisor calls it
WORK = {"commander": "事件計劃", "main_agent": "對話", "ask": "提問", "reporter": "報告", "dispatch": "派發",
        "qa": "QA 模式", "discuss": "討論"}


def metric(v: Any, src: str, na: str | None = None, **extra: Any) -> dict[str, Any]:
    out = {"v": v, "src": src, **extra}
    if v is None:
        out["na"] = na or "此平台無法取得"
    return out


# ------------------------------------------------------------------------------------------------ parsers (pure)
def _num(s: str) -> float | None:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None                                   # "[N/A]", "[Not Supported]", ""


GPU_QUERY = "name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw"


def parse_nvidia_smi(text: str) -> list[dict[str, Any]]:
    """`nvidia-smi --query-gpu=<GPU_QUERY> --format=csv,noheader,nounits`, one line per GPU. DGX Spark (GB10) prints
    [N/A] for memory: its GPU shares the system memory, so memory is None there and the RAM line covers it."""
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        name, util, temp, used, total, power = parts[:6]
        gpus.append({"name": name, "util_pct": _num(util), "temp_c": _num(temp),
                     "mem_used_mb": _num(used), "mem_total_mb": _num(total), "power_w": _num(power)})
    return gpus


def parse_compute_apps(text: str) -> float | None:
    """`nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits` -> bytes held by GPU processes
    (MiB per line). Works on the Spark's unified memory where --query-gpu memory is [N/A]; bench/memory_check.py reads
    the same query."""
    total, seen = 0.0, False
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and (mb := _num(parts[1])) is not None:
            total, seen = total + mb, True
    return total * 2**20 if seen else None


def parse_vm_stat(text: str) -> dict[str, int]:
    """macOS `vm_stat` -> bytes per counter (wired, active, compressed...). The page size is on the first line."""
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    out: dict[str, int] = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        v = v.strip().rstrip(".")
        if v.isdigit():
            key = k.strip().strip('"').lower().replace("pages ", "").replace(" ", "_")
            out[key] = int(v) * page
    return out


def parse_ollama_ps(data: dict[str, Any], model: str) -> dict[str, Any]:
    """Ollama GET /api/ps: is `model` loaded, its size and the part in GPU memory."""
    rows = data.get("models") or []
    row = next((r for r in rows if r.get("name") == model or r.get("model") == model), None)
    return {"loaded": row is not None, "size": (row or {}).get("size"), "size_vram": (row or {}).get("size_vram"),
            "context_length": (row or {}).get("context_length"), "expires_at": (row or {}).get("expires_at"),
            "others": [r.get("name") for r in rows if r is not row]}


_PROM = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEinfNa]+)")


def parse_prometheus(text: str) -> dict[str, float]:
    """Prometheus text -> {metric name: value summed over its label sets}. Enough for vLLM's gauges and counters."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _PROM.match(line)
        if m:
            v = _num(m.group(3))
            if v is not None:
                out[m.group(1)] = out.get(m.group(1), 0.0) + v
    return out


def vllm_view(prom: dict[str, float]) -> dict[str, Any]:
    """The vLLM /metrics numbers the page shows. vLLM renamed gpu_cache_usage_perc to kv_cache_usage_perc (0.10+)."""
    cache = prom.get("vllm:gpu_cache_usage_perc", prom.get("vllm:kv_cache_usage_perc"))
    return {"cache_pct": round(cache * 100, 1) if cache is not None else None,
            "running": prom.get("vllm:num_requests_running"), "waiting": prom.get("vllm:num_requests_waiting"),
            "gen_tokens_total": prom.get("vllm:generation_tokens_total")}


def call_row(e: Any) -> dict[str, Any] | None:
    """An llm_call audit row -> what the meter needs, or None for a rules-mode row (no model was called)."""
    p = e.payload or {}
    if p.get("mode") == "mock":
        return None
    ms, out = p.get("total_ms"), p.get("completion_tokens")
    tps = round(out / (ms / 1000), 1) if isinstance(out, (int, float)) and isinstance(ms, (int, float)) and ms > 0 and out else None
    return {"actor": e.actor, "ms": ms if isinstance(ms, (int, float)) else None, "tps": tps, "error": bool(p.get("error")),
            "fell_back": bool(p.get("fell_back_to_mock") or p.get("fell_back"))}


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    return statistics.quantiles(xs, n=100, method="inclusive")[int(q) - 1]


# ------------------------------------------------------------------------------------------------ sampler
class MetricsSampler:
    def __init__(self, rt: Callable[[], Any], http: Any = None, run: Callable[..., str] | None = None):
        self.rt = rt
        self._http = http
        self._run = run or self._subprocess
        self.points: deque[dict[str, Any]] = deque(maxlen=int(HISTORY_S / SAMPLE_S))
        self.calls: deque[dict[str, Any]] = deque(maxlen=CALLS_KEPT)
        self.last: dict[str, Any] = {}
        self.last_seq = 0
        self.primed = False
        self.last_ask = time.monotonic()
        self.task: asyncio.Task | None = None
        self.kind: str | None = None
        self.gen_prev: tuple[float, float] | None = None
        self.sample_ms: deque[float] = deque(maxlen=20)
        self.has_smi = shutil.which("nvidia-smi") is not None
        self.mac = platform.system() == "Darwin"
        import psutil
        self.ps = psutil
        self.proc = psutil.Process()
        psutil.cpu_percent(None)                        # first call primes the counter
        self.proc.cpu_percent(None)

    @staticmethod
    def _subprocess(*cmd: str) -> str:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False).stdout

    # ---- lifecycle
    def touch(self) -> None:
        self.last_ask = time.monotonic()
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self._loop(), name="admin-metrics")

    async def _loop(self) -> None:
        while time.monotonic() - self.last_ask < IDLE_STOP_S:
            with contextlib.suppress(Exception):       # one bad sample never kills the meter
                await self.sample()
            await asyncio.sleep(SAMPLE_S)

    # ---- one sample
    async def sample(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        machine = await asyncio.to_thread(self.machine)
        model = await self.model()
        self.sample_ms.append((time.perf_counter() - t0) * 1000)
        now = time.time()
        point = {"t": round(now, 1), "cpu": machine["cpu_pct"]["v"], "ram": machine["ram_used"]["v"],
                 "gpu": (machine["gpus"][0]["util_pct"]["v"] if machine["gpus"] else None),
                 "model_mem": model["memory"]["v"], "tps": model["tps_live"]["v"],
                 "queue": model["queue"]["waiting"] + len(model["queue"]["running"]),
                 "calls_min": model["calls_last_min"]["v"]}
        self.points.append(point)
        self.last = {"machine": machine, "model": model, "at": datetime.fromtimestamp(now, UTC).isoformat()}
        return self.last

    def machine(self) -> dict[str, Any]:
        ps = self.ps
        vm = ps.virtual_memory()
        r = self.rt()
        data_dir = Path(getattr(r.settings, "audit_path", ".")).parent
        try:
            du = shutil.disk_usage(data_dir)
            disk = metric(du.free, "shutil.disk_usage（資料資料夾所在磁碟）", total=du.total)
        except OSError:
            disk = metric(None, "shutil.disk_usage")
        out: dict[str, Any] = {
            "platform": "DGX Spark / Linux GPU" if self.has_smi else ("macOS" if self.mac else platform.system()),
            "cpu_pct": metric(ps.cpu_percent(None), "psutil.cpu_percent（全機，所有核心平均）", cores=ps.cpu_count()),
            "ram_used": metric(vm.total - vm.available, "psutil.virtual_memory（total − available）", total=vm.total),
            "disk_free": disk,
            "rss": metric(self.proc.memory_info().rss, "psutil.Process().memory_info().rss（戰情室伺服器行程）"),
            "proc_cpu": metric(self.proc.cpu_percent(None), "psutil.Process().cpu_percent（戰情室伺服器行程，單核 = 100%）"),
            "uptime_s": metric(round(r.uptime_s()), "Runtime.uptime_s（伺服器啟動至今）"),
            "boot_s": metric(round(time.time() - ps.boot_time()), "psutil.boot_time（機器開機至今）"),
            "sampler_ms": metric(round(statistics.median(self.sample_ms), 1) if self.sample_ms else None,
                                 "本取樣器每次耗時中位數", na="第一次取樣"),
            "gpus": [], "gpu_na": None, "wired": None,
        }
        if self.has_smi:
            gpus = parse_nvidia_smi(self._run("nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"))
            src = "nvidia-smi --query-gpu"
            for g in gpus:
                unified = g["mem_total_mb"] is None
                out["gpus"].append({
                    "name": g["name"],
                    "util_pct": metric(g["util_pct"], src + " utilization.gpu"),
                    "temp_c": metric(g["temp_c"], src + " temperature.gpu"),
                    "power_w": metric(g["power_w"], src + " power.draw"),
                    "mem_used": metric(None if unified else g["mem_used_mb"] * 2**20, src + " memory.used",
                                       na=NA_UNIFIED if unified else None,
                                       total=None if unified else g["mem_total_mb"] * 2**20)})
            if not gpus:
                out["gpu_na"] = "nvidia-smi 沒有回傳 GPU"
        elif self.mac:
            out["gpu_na"] = NA_MAC_GPU
            w = parse_vm_stat(self._run("vm_stat")).get("wired_down")
            out["wired"] = metric(w, "vm_stat Pages wired down（含 GPU／模型佔用的統一記憶體）", total=vm.total)
        else:
            out["gpu_na"] = NA_NO_GPU
        return out

    async def _get(self, url: str) -> Any:
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=1.5)
        return await self._http.get(url)

    async def model(self) -> dict[str, Any]:
        r = self.rt()
        s = r.settings
        mode = getattr(r.llm_mode, "value", str(r.llm_mode))
        base = s.base_url.rstrip("/")
        root = base.removesuffix("/v1")
        host = urlparse(s.base_url).netloc
        out: dict[str, Any] = {"mode": mode, "model": s.model, "host": host if mode != "mock" else None}
        if self.kind is None and mode != "mock":
            from warroom.agent.commander import detect_server_kind
            self.kind = await asyncio.to_thread(detect_server_kind, s.base_url)
        out["server"] = self.kind if mode != "mock" else None
        loaded = metric(None, "規則模式不呼叫模型", na="規則模式（LLM_MODE=mock）")
        memory = metric(None, "—", na="規則模式（LLM_MODE=mock）")
        vram = metric(None, "—", na="規則模式（LLM_MODE=mock）" if mode == "mock" else "只有 Ollama 提供")
        cache = running = waiting = metric(None, "—", na="只有 vLLM 提供")
        gen_rate = None
        if mode != "mock" and self.kind == "ollama":
            try:
                ps = parse_ollama_ps((await self._get(root + "/api/ps")).json(), s.model)
                loaded = metric(ps["loaded"], "Ollama GET /api/ps", context=ps["context_length"], others=ps["others"])
                memory = metric(ps["size"] if ps["loaded"] else 0, "Ollama /api/ps size（模型佔用記憶體）")
                vram = metric(ps["size_vram"] if ps["loaded"] else 0, "Ollama /api/ps size_vram（放在 GPU 的部分）")
            except Exception as e:  # noqa: BLE001 - server down: say so, keep the rest
                loaded = metric(None, "Ollama GET /api/ps", na=f"模型伺服器沒有回應（{type(e).__name__}）")
        elif mode != "mock" and self.kind == "vllm":
            try:
                v = vllm_view(parse_prometheus((await self._get(root + "/metrics")).text))
                loaded = metric(True, "vLLM GET /metrics 有回應（vLLM 啟動時就載入模型）")
                held = parse_compute_apps(await asyncio.to_thread(
                    self._run, "nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits",
                )) if self.has_smi else None
                memory = metric(held, "nvidia-smi --query-compute-apps used_memory（GPU 行程佔用，含 vLLM 預先配置的 KV cache）",
                                na="vLLM 不回報模型大小，且找不到 nvidia-smi")
                cache = metric(v["cache_pct"], "vLLM /metrics gpu_cache_usage_perc（KV cache 使用率）")
                running = metric(v["running"], "vLLM /metrics num_requests_running")
                waiting = metric(v["waiting"], "vLLM /metrics num_requests_waiting")
                now, tot = time.monotonic(), v["gen_tokens_total"]
                if tot is not None and self.gen_prev and now > self.gen_prev[0]:
                    gen_rate = max(0.0, (tot - self.gen_prev[1]) / (now - self.gen_prev[0]))
                self.gen_prev = (now, tot) if tot is not None else None
            except Exception as e:  # noqa: BLE001
                loaded = metric(None, "vLLM GET /metrics", na=f"模型伺服器沒有回應（{type(e).__name__}）")
        elif mode != "mock":
            loaded = metric(None, "—", na="雲端 API：沒有本機模型記憶體可讀")
        out.update(loaded=loaded, memory=memory, vram=vram, cache_pct=cache, vllm_running=running, vllm_waiting=waiting)

        self._read_calls(r)
        ok = [c for c in self.calls if not c["error"] and c["ms"] is not None]
        ms = [c["ms"] for c in ok][-50:]
        tps = [c["tps"] for c in ok if c["tps"]][-10:]
        now_w = time.time()
        src_calls = "稽核鏈 llm_call 紀錄（最近 50 次成功呼叫）"
        out["p50_ms"] = metric(round(pct(ms, 50)) if ms else None, src_calls, na="還沒有成功的模型呼叫")
        out["p90_ms"] = metric(round(pct(ms, 90)) if ms else None, src_calls, na="還沒有成功的模型呼叫")
        last_tps = tps[-1] if tps else None
        out["tps_last"] = metric(last_tps, "稽核鏈 llm_call：completion_tokens ÷ total_ms（最近一次）", na="還沒有帶 token 數的呼叫")
        if gen_rate is not None:
            out["tps_live"] = metric(round(gen_rate, 1), "vLLM /metrics generation_tokens_total 每秒增量")
        else:
            out["tps_live"] = metric(round(statistics.median(tps), 1) if tps else None,
                                     "稽核鏈 llm_call：最近 10 次 tokens/s 中位數（Ollama 不提供即時產生速率）",
                                     na="還沒有帶 token 數的呼叫")
        out["calls_last_min"] = metric(sum(1 for c in self.calls if c.get("seen") and now_w - c["seen"] <= 60),
                                       "稽核鏈新增的 llm_call（取樣時看到的時間）")
        out["calls_seen"] = metric(len(self.calls), f"稽核鏈 llm_call（最近 {CALLS_KEPT} 筆）")
        out["fallbacks"] = metric(sum(1 for c in self.calls if c["fell_back"]),
                                  f"稽核鏈 llm_call fell_back_to_mock（最近 {CALLS_KEPT} 筆）")
        out["errors"] = metric(sum(1 for c in self.calls if c["error"]), f"稽核鏈 llm_call error（最近 {CALLS_KEPT} 筆）")
        out["queue"] = self._queue(r)
        return out

    def _read_calls(self, r: Any) -> None:
        """New llm_call rows since the last sample. Rows already on the chain when the meter starts count for the
        latency and fallback numbers but not for calls-per-minute: their wall time is unknown (ts is factory time)."""
        first, self.primed = not self.primed, True
        entries = r.audit.tail(0, 2000) if first else r.audit.tail(self.last_seq, 10_000)
        now = time.time()
        for e in entries:
            self.last_seq = max(self.last_seq, e.seq)
            if str(getattr(e.kind, "value", e.kind)) != "llm_call":
                continue
            row = call_row(e)
            if row is not None:
                row["seen"] = None if first else now
                self.calls.append(row)

    def _queue(self, r: Any) -> dict[str, Any]:
        q = getattr(getattr(r, "builder", None), "model_queue", None)
        src = "ModelQueue（warroom/agent/orchestrator.py）"
        if q is None:
            return {"running": [], "waiting": 0, "slots": None, "src": src}
        now = time.perf_counter()
        run = [{"work": WORK.get(t.who, t.who), "label": t.label, "priority": t.priority,
                "secs": round(now - t.since, 1)} for t in list(getattr(q, "_running", []))]
        return {"running": run, "waiting": len(getattr(q, "_waiting", [])), "slots": q.slots, "src": src}

    def view(self) -> dict[str, Any]:
        pts = list(self.points)
        return {"interval_s": SAMPLE_S, "history_s": HISTORY_S, **self.last,
                "history": {k: [p[k] for p in pts] for k in ("t", "cpu", "ram", "gpu", "model_mem", "tps", "queue", "calls_min")}}


async def metrics_view(app: Any, rt: Callable[[], Any]) -> dict[str, Any]:
    """GET /api/admin/metrics: start (or keep alive) the sampler and return its latest sample plus history."""
    s: MetricsSampler | None = getattr(app.state, "metrics", None)
    if s is None:
        s = app.state.metrics = MetricsSampler(rt)
    s.touch()
    if not s.last:
        await s.sample()
    return s.view()


