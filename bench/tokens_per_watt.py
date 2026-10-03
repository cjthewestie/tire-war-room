"""tokens/W (page 10: 每瓦效能, 插座電表量整機功耗, tokens/W 公布).

Method: hold a steady generation load for --duration seconds at --concurrency streams of the commander's
real call (bench.prompts.commander_messages, CommanderPlan response_format; plan and replan rounds, the
alert card leads each plan), count generated tokens from the server's usage, and divide by the whole-machine
power read from a wall-plug meter during the same window.

  tokens/s      = generated tokens / load seconds
  tokens/J      = generated tokens / (avg watts x load seconds)    (= "tokens per watt-second")
  tokens/s/W    = tokens/s / avg watts                             (numerically equal to tokens/J)
  net variants  = same with (avg watts - idle watts), to separate the model from the idle box

Power input, one of:
  --watts 180                      average reading off the plug meter during the run
  --meter-csv meter.csv            CSV with columns ts (ISO 8601 or unix s), watts; averaged over the run window
  --powermetrics                   macOS only, needs sudo: samples CPU+GPU+ANE package power (NOT wall power;
                                   reported separately, never as the published plug number)
  --idle-watts 45                  optional plug reading with the stack idle

  uv run python -m bench.tokens_per_watt --duration 60 --watts 175 --idle-watts 40
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench.llm import Endpoint, endpoint_info, run, save_result, stream_chat
from bench.prompts import (
    ANOMALIES,
    COMMANDER_MAX_TOKENS,
    commander_format,
    commander_messages,
    live_anomaly,
    page11_rejected_h1,
)


def meter_average(path: Path, t_start: float, t_end: float) -> float | None:
    """Average watts from a plug-meter CSV within [t_start, t_end] (unix seconds)."""
    vals = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts_raw = (row.get("ts") or row.get("timestamp") or "").strip()
            w = row.get("watts") or row.get("W") or row.get("power_w")
            if not ts_raw or w is None:
                continue
            try:
                ts = float(ts_raw)
            except ValueError:
                dt = datetime.fromisoformat(ts_raw)
                ts = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
            if t_start <= ts <= t_end:
                vals.append(float(w))
    return round(sum(vals) / len(vals), 2) if vals else None


def efficiency(tokens: int, seconds: float, avg_w: float | None, idle_w: float | None = None) -> dict[str, Any]:
    tps = tokens / seconds if seconds > 0 else 0.0
    out: dict[str, Any] = {"tokens": tokens, "seconds": round(seconds, 3), "tokens_per_s": round(tps, 3),
                           "avg_watts": avg_w, "idle_watts": idle_w}
    if avg_w:
        out["tokens_per_joule"] = round(tokens / (avg_w * seconds), 4)
        out["tokens_per_s_per_watt"] = round(tps / avg_w, 4)
        out["joules_per_token"] = round(avg_w * seconds / tokens, 4) if tokens else None
        if idle_w is not None and avg_w > idle_w:
            out["net_tokens_per_joule"] = round(tokens / ((avg_w - idle_w) * seconds), 4)
    return out


class PowerMetrics:
    """macOS `powermetrics` sampler (sudo -n). Package power only, not wall power."""

    def __init__(self, interval_ms: int = 500):
        self.interval_ms = interval_ms
        self.proc: subprocess.Popen | None = None

    def start(self) -> bool:
        try:
            self.proc = subprocess.Popen(["sudo", "-n", "powermetrics", "--samplers", "cpu_power,gpu_power",
                                          "-i", str(self.interval_ms)], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
            time.sleep(1.0)
            return self.proc.poll() is None
        except FileNotFoundError:
            return False

    def stop(self) -> float | None:
        if not self.proc:
            return None
        self.proc.terminate()
        out, _ = self.proc.communicate(timeout=10)
        mw = [float(x) for x in re.findall(r"Combined Power \(CPU \+ GPU \+ ANE\):\s*([\d.]+)\s*mW", out)]
        return round(sum(mw) / len(mw) / 1000, 2) if mw else None


async def load(ep: Endpoint, duration_s: float, concurrency: int) -> tuple[int, float, int, int]:
    deadline = time.perf_counter() + duration_s
    tokens = calls = errors = 0
    cli = ep.client(timeout_s=300)

    async def worker(k: int):
        nonlocal tokens, calls, errors
        i = k
        while time.perf_counter() < deadline:
            a = live_anomaly(ANOMALIES[i % len(ANOMALIES)])
            if i % 2 and a["machine_id"] == ANOMALIES[0]["machine_id"]:   # page-11 replan round (H1 rejected)
                msgs = commander_messages(a, "replan", rejected=page11_rejected_h1(),
                                          extra={"failed_expectation": {"hypothesis_id": "H1", "observed": 119.5}})
            else:
                msgs = commander_messages(a, "plan")
            r = await stream_chat(ep, msgs, max_tokens=COMMANDER_MAX_TOKENS, client=cli,
                                  response_format=commander_format())
            calls += 1
            if r.ok:
                tokens += r.completion_tokens or 0
            else:
                errors += 1
            i += concurrency

    t0 = time.perf_counter()
    await asyncio.gather(*(worker(k) for k in range(concurrency)))
    await cli.close()
    return tokens, time.perf_counter() - t0, calls, errors


async def main_async(args) -> dict[str, Any]:
    ep = Endpoint.from_env(args.base_url, args.model)
    await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)
    pm = PowerMetrics() if args.powermetrics else None
    pm_ok = pm.start() if pm else False
    t_start = time.time()
    tokens, secs, calls, errors = await load(ep, args.duration, args.concurrency)
    t_end = time.time()
    pkg_w = pm.stop() if pm_ok else None
    plug_w = args.watts
    if args.meter_csv:
        plug_w = meter_average(args.meter_csv, t_start, t_end)
    out: dict[str, Any] = {"endpoint": await endpoint_info(ep), "concurrency": args.concurrency,
                           "prompt": "commander: warroom.agent.commander.build_messages(CommanderRequest), "
                                     "plan + replan rounds",
                           "window_unix": [t_start, t_end], "calls": calls, "errors": errors,
                           "plug": efficiency(tokens, secs, plug_w, args.idle_watts),
                           "power_source": "plug_meter" if plug_w else "none (tokens/W not computed)"}
    if pm:
        out["package_power_powermetrics"] = efficiency(tokens, secs, pkg_w) if pkg_w else \
            {"error": "powermetrics unavailable (needs passwordless sudo)"}
    out["saved_to"] = str(save_result("tokens_per_watt", ep, out, args.out_dir))
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--watts", type=float)
    p.add_argument("--idle-watts", type=float)
    p.add_argument("--meter-csv", type=Path)
    p.add_argument("--powermetrics", action="store_true")
    p.add_argument("--out-dir", type=Path)
    out = run(main_async(p.parse_args(argv)))
    print("plug:", out["plug"])
    if "package_power_powermetrics" in out:
        print("package (powermetrics):", out["package_power_powermetrics"])
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
