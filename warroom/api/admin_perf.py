"""後台 效能與追蹤 (issue #199). CJ, 2026-10-22: 「trace and load 測試的一些數據要放在後台」.

Two read-only views and one background job:

- bench(): the measured numbers in bench/results/ (the JSON each bench wrote, judged by SUMMARY.md for staleness),
  one row per page-9/10 metric with a DGX Spark cell and a Mac cell: measured, PASS / FAIL / 未量, date, source file.
  Targets come from warroom/factory.py. Nothing here is typed in by hand: a metric with no file reads 未量.
- trace(): live numbers from the running app's audit log (llm_call, tool_result and the model queue's rows):
  calls, p50 / p90 per phase, tokens, tool latency, the last 20 model calls with their Nemo task number, queue waits.
- LoadJob: 「跑一次負載測試」, `bench.concurrent` against the app's own local model server in a subprocess, one at a time,
  result saved under bench/results/<YYYYMMDD>-spark-load/."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from warroom import factory as F

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "bench" / "results"
SPARK, MAC = "DGX Spark", "Mac"

# key, label, unit, target (factory.py where it has the number; else the bench's own target text, None = from file)
METRICS: list[tuple[str, str, str, str | None]] = [
    ("decode_tps_p50", "單流解碼速度 p50", "tok/s", "≥ 95 tok/s（p.10 寫 95–108）"),
    ("first_alert_card_p90", "第一張告警卡 p90（冷）", "s", f"≤ {F.LATENCY_FIRST_ALERT_S:g} s"),
    ("first_alert_card_warm_p90", "第一張告警卡 p90（同請求重複）", "s", "參考"),
    ("ttft_warm_32k_p90", "32k 首字 p90（熱快取）", "s", f"≤ {F.TTFT_TARGET_S:g} s"),
    ("ttft_cold_32k_p50", "32k 首字 p50（冷，無快取）", "s", "參考"),
    ("concurrent_3_worst_alert", f"{F.CONCURRENT_ANOMALIES} 件同時，最慢一件告警卡", "s",
     f"每件 ≤ {F.CONCURRENT_EACH_S:g} s，批次處理"),
    ("concurrent_batched", f"{F.CONCURRENT_ANOMALIES} 件同時有被批次處理", "輪", "每輪都批次"),
    ("full_event_p50", "完整事件 p50（偵測到結案）", "s", f"≈ {F.LATENCY_FULL_EVENT_S:g} s 以內"),
    ("state_machine_closed_rate", "完整事件結案率", "", "1.0"),
    ("p11_closed", "p.11 真模型跑完（換 H2、結案）", "次", "每次都結案"),
    ("commander_ttft_app", "指揮官首字（app 實跑，p.11）", "s", "參考"),
    ("stack_plus_os_gb", "記憶體（整套加 OS）", "GB", None),
    ("alert_offline", "斷網時第一張告警卡", "s", f"≤ {F.LATENCY_FIRST_ALERT_S:g} s"),
    ("outbound_connections", "斷網時對外連線", "次", "0"),
    ("page9_unplug", "實體拔網路線測試", "", "斷線、0 外連、無 DNS"),
    ("tokens_per_joule", "每瓦 token（tokens/J，插座實測）", "tok/J", "公布數字"),
]
KEYS = {m[0] for m in METRICS}


# ---------------------------------------------------------------- bench files
def host_label(d: dict[str, Any]) -> str:
    h = d.get("host") or {}
    plat, name = str(h.get("platform", "")).lower(), str(h.get("hostname", "")).lower()
    if "macos" in plat or "darwin" in plat:
        return MAC
    if "aitopatom" in name or "spark" in name or ("linux" in plat and h.get("machine") == "aarch64"):
        return SPARK
    return str(h.get("hostname") or "?")


def _stale_files(results: Path) -> set[str]:
    """File names SUMMARY.md (bench/py.sh -m bench.report) marks STALE: older bench code, not counted."""
    try:
        text = (results / "SUMMARY.md").read_text(encoding="utf-8")
    except OSError:
        return set()
    out = set()
    for ln in text.splitlines():
        if ln.startswith("|") and "STALE" in ln:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if cells and cells[-1].endswith(".json"):
                out.add(cells[-1])
    return out


def _verdict(passed: Any, measured: Any, text: str = "") -> str:
    if measured is None:
        return "未量"
    if "UNJUDGED" in text:
        return "未判定"
    return {True: "PASS", False: "FAIL"}.get(passed, "參考")


def _file_rows(d: dict[str, Any]) -> list[dict[str, Any]]:
    """(metric, measured, verdict, note, target) from one bench JSON, the same rows bench/report.py prints."""
    out = []
    for v in d.get("verdicts") or []:
        out.append({"key": v.get("name"), "measured": v.get("measured"), "target_file": str(v.get("target", "")),
                    "verdict": _verdict(v.get("passed"), v.get("measured")), "note": v.get("note") or ""})
    b = d.get("bench")
    sm = d.get("state_machine")
    if b == "full_event" and isinstance(sm, dict) and "closed_rate" in sm:
        cr = sm.get("closed_rate")
        out.append({"key": "state_machine_closed_rate", "measured": cr, "target_file": "1.0",
                    "verdict": _verdict(None if cr is None else cr >= 1.0, cr), "note": "真的狀態機"})
    j = d.get("judgement") if b == "memory" else None
    if j:
        lo, hi = j.get("band_gb", [None, None])
        out.append({"key": "stack_plus_os_gb", "measured": j.get("total_gb"), "target_file": f"{lo}–{hi} GB，ctx ≥ {(j.get('context') or {}).get('required')}",
                    "verdict": {"PASS": "PASS", "FAIL": "FAIL"}.get(j.get("verdict"), "未判定"),
                    "note": "；".join(j.get("reasons") or [])})
    if b == "tokens_per_watt":
        pl = d.get("plug") or {}
        tpj = pl.get("tokens_per_joule")
        out.append({"key": "tokens_per_joule", "measured": tpj, "target_file": "publish", "verdict": "參考" if tpj else "未量",
                    "note": "插座電表" if pl.get("avg_watts") else f"沒有插座電表讀數（只量了 {pl.get('tokens_per_s')} tok/s）"})
    return out


def _busy(d: dict[str, Any]) -> str | None:
    la, cpus = d.get("host_load_avg_1_5_15"), (d.get("host") or {}).get("cpu_count") or 0
    if la and cpus and max(la[0], la[1]) > cpus / 2:
        return f"主機忙（load {la[0]}，{cpus} 核），數字偏慢"
    return None


def _cell(d: dict[str, Any], row: dict[str, Any], rel: str, stale: bool) -> dict[str, Any]:
    ep = d.get("endpoint") or {}
    notes = [x for x in (_busy(d), "舊版 bench 程式跑的，不計（SUMMARY.md 標 STALE）" if stale else None) if x]
    return {"measured": row["measured"], "verdict": "過期" if stale else row["verdict"],
            "date": str(d.get("recorded_at", ""))[:10] or None, "recorded_at": d.get("recorded_at"),
            "source": rel, "model": ep.get("model"), "code": (d.get("code") or {}).get("git_sha"),
            "target_file": row.get("target_file"), "note": "；".join(notes), "detail": row["note"]}


def _p11_spark(results: Path) -> list[tuple[str, dict[str, Any]]]:
    """spark-lightning-*/p11/summary.txt: runs that reached END:closed, and the commander's app TTFT lines."""
    out = []
    for p in sorted(results.glob("*/p11/summary.txt")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        runs = [ln for ln in text.splitlines() if ln.startswith("p11-")]
        ttft = [float(x) for x in re.findall(r"ttft=([\d.]+)s", text)]
        rel = str(p.relative_to(REPO))
        date = _dir_date(p)
        if runs:
            ok = sum(1 for r in runs if "END:closed" in r)
            sha = re.match(r"p11-([0-9a-f]{7,})", runs[0])
            out.append(("p11_closed", {"measured": f"{ok}/{len(runs)}", "verdict": "PASS" if ok == len(runs) else "FAIL",
                                       "date": date, "recorded_at": date, "source": rel, "model": "lightning",
                                       "code": sha.group(1) if sha else None, "target_file": "每次都結案",
                                       "note": "修正前的版本；結束：" + "、".join(sorted({r.rsplit('END:', 1)[-1] for r in runs}))}))
        if ttft:
            out.append(("commander_ttft_app", {"measured": _pct(ttft, 0.5), "verdict": "參考", "date": date, "recorded_at": date,
                                               "source": rel, "model": "lightning", "code": None, "target_file": "",
                                               "note": f"指揮官 {len(ttft)} 次，範圍 {min(ttft):.2f}–{max(ttft):.2f} s"}))
    return out


def _p11_mac(results: Path) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for p in sorted(results.glob("mac-*/runs/summary.json")):
        try:
            runs = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(runs, list) or not runs:
            continue
        ok = sum(1 for r in runs if isinstance(r, dict) and r.get("end") == "closed")
        h2 = [r.get("h2_card_s") for r in runs if isinstance(r, dict) and r.get("h2_card_s") is not None]
        date = _dir_date(p)
        out.append(("p11_closed", {"measured": f"{ok}/{len(runs)}", "verdict": "PASS" if ok == len(runs) else "FAIL",
                                   "date": date, "recorded_at": date, "source": str(p.relative_to(REPO)),
                                   "model": "nemotron-3.5-lightning:30b-a3b-nvfp4", "code": None, "target_file": "每次都結案",
                                   "note": f"修正後；H2 卡出現在第 {min(h2):.0f}–{max(h2):.0f} 秒" if h2 else "修正後"}))
    return out


def _dir_date(p: Path) -> str | None:
    for part in p.parts[::-1]:
        m = re.search(r"(20\d{2})(\d{2})(\d{2})", part)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _models(results: Path) -> list[dict[str, Any]]:
    """Same main agent, Lightning vs Bolt, on the Spark (spark-bolt-*/compare.json) plus Lightning's own trace."""
    out = []
    for p in sorted(results.glob("spark-*/compare.json")):
        try:
            s = json.loads(p.read_text(encoding="utf-8")).get("summary") or {}
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        name = {"lightning_replay": "Lightning・固定腳本", "bolt_replay": "Bolt・固定腳本", "bolt_manual": "Bolt・手動問"}
        for k, v in s.items():
            m, c = v.get("main") or {}, v.get("commander") or {}
            out.append({"run": name.get(k, k), "host": SPARK, "date": _dir_date(p), "source": str(p.relative_to(REPO)),
                        "main_calls": m.get("calls"), "main_p50_s": (m.get("total_s") or {}).get("median"),
                        "main_in_p50": (m.get("prompt_tokens") or {}).get("median"),
                        "commander_calls": c.get("calls"), "commander_ttft_s": (c.get("ttft_s") or {}).get("median"),
                        "commander_tps": (c.get("decode_tps") or {}).get("median"), "commander_total_s": (c.get("total_s") or {}).get("median")})
    for p in sorted(results.glob("spark-*/llm-trace.jsonl")):
        main, cmd = [], []
        try:
            for ln in p.open(encoding="utf-8"):
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                (main if r.get("role") == "主 agent" else cmd).append(r)
        except OSError:
            continue
        tot = [r["total_s"] for r in main if isinstance(r.get("total_s"), (int, float))]
        ins = [r["prompt_tokens"] for r in main if isinstance(r.get("prompt_tokens"), (int, float))]
        ct = [r["ttft_s"] for r in cmd if isinstance(r.get("ttft_s"), (int, float))]
        tps = [r["decode_tps"] for r in cmd if isinstance(r.get("decode_tps"), (int, float))]
        ctot = [r["total_s"] for r in cmd if isinstance(r.get("total_s"), (int, float))]
        out.append({"run": "Lightning・手動 14 輪", "host": SPARK, "date": _dir_date(p), "source": str(p.relative_to(REPO)),
                    "main_calls": len(main), "main_p50_s": _pct(tot, 0.5), "main_in_p50": _pct(ins, 0.5),
                    "commander_calls": len(cmd), "commander_ttft_s": _pct(ct, 0.5), "commander_tps": _pct(tps, 0.5),
                    "commander_total_s": _pct(ctot, 0.5)})
    return out


def bench(results: Path = RESULTS) -> dict[str, Any]:
    """One row per page-9/10 metric: the newest file with a number, per host (DGX Spark / Mac)."""
    stale = _stale_files(results)
    cand: dict[tuple[str, str], list[dict[str, Any]]] = {}
    files = 0
    for p in sorted(results.rglob("*.json")):
        if "before-fix" in p.parts:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(d, dict) or "bench" not in d:
            continue
        files += 1
        host, rel = host_label(d), str(p.relative_to(REPO))
        for row in _file_rows(d):
            if row["key"] in KEYS:
                cand.setdefault((row["key"], host), []).append(_cell(d, row, rel, p.name in stale))
    for key, c in [*_p11_spark(results), *_p11_mac(results)]:
        host = MAC if Path(c["source"]).parts[2].startswith("mac") else SPARK
        cand.setdefault((key, host), []).append(c)

    def best(cs: list[dict[str, Any]]) -> dict[str, Any]:
        # a number beats 未量 (e.g. a run whose model server was down), a counted run beats a stale one, newest wins
        return max(cs, key=lambda c: (c["measured"] is not None, c["verdict"] != "過期", str(c.get("recorded_at") or "")))

    rows = []
    for key, label, unit, target in METRICS:
        cells = {h: best(cand[(key, h)]) if (key, h) in cand else None for h in (SPARK, MAC)}
        tgt = target or next((c["target_file"] for c in cells.values() if c and c.get("target_file")), "—")
        rows.append({"key": key, "label": label, "unit": unit, "target": tgt, "cells": cells,
                     "others": {h: len(cand.get((key, h), [])) for h in (SPARK, MAC)}})
    counts = {h: {v: sum(1 for r in rows if (r["cells"][h] or {}).get("verdict", "未量") == v)
                  for v in ("PASS", "FAIL", "未量", "參考", "未判定", "過期")} for h in (SPARK, MAC)}
    return {"rows": rows, "counts": counts, "models": _models(results), "files": files,
            "summary": "bench/results/SUMMARY.md", "targets_from": "warroom/factory.py（p.9–10）",
            "note": "數字都來自 bench/results/ 裡的結果檔；Mac 是代跑（Ollama），DGX Spark 是正式機（vLLM）。沒有檔案的指標寫「未量」。"}


# ---------------------------------------------------------------- live trace (audit log)
PHASE = {"commander/plan": "指揮官・擬計劃", "commander/replan": "指揮官・重新規劃", "main_agent/chat": "Agent Nemo・對話",
         "main_agent/casual": "Agent Nemo・閒聊", "ask/plan": "問戰情室・查詢", "ask/answer": "問戰情室・回答",
         "discuss/classify": "討論・分類"}
_CONV_EVENT = re.compile(r"task:([^|]+)\|")


def _pct(xs: list[float], q: float) -> float | None:
    v = sorted(x for x in xs if isinstance(x, (int, float)))
    if not v:
        return None
    return round(v[max(0, math.ceil(q * len(v)) - 1)], 3)


def _stats(xs: list[float]) -> dict[str, Any]:
    return {"n": len(xs), "p50": _pct(xs, 0.5), "p90": _pct(xs, 0.9), "max": round(max(xs), 3) if xs else None}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def event_of(row: dict[str, Any]) -> str | None:
    if row.get("event_id"):
        return row["event_id"]
    m = _CONV_EVENT.match(str((row.get("payload") or {}).get("conversation") or ""))
    return m.group(1) if m else None


def trace(newest_first: Iterable[bytes], *, limit: int = 200, day: str | None = None,
          session_no: Callable[[str], tuple[str, int] | None] | None = None, max_scan: int = 100_000) -> dict[str, Any]:
    """Model, tool and queue numbers from the audit log, newest first. `day` (factory date, YYYY-MM-DD) keeps only that
    day's rows; else the newest `limit` model calls (tools and queue rows: the newest 5× / 3× that)."""
    llm: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    scanned = 0
    for ln in newest_first:
        scanned += 1
        if scanned > max_scan or (len(llm) >= limit and len(tools) >= limit * 5 and len(queue) >= limit * 3):
            break
        if b'"llm_call"' not in ln and b'"tool_result"' not in ln and b'"queue"' not in ln:
            continue
        try:
            r = json.loads(ln)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if day and str(r.get("ts", ""))[:10] != day:
            continue
        k, p = r.get("kind"), r.get("payload") or {}
        if k == "llm_call" and len(llm) < limit:
            llm.append(r)
        elif k == "tool_result" and len(tools) < limit * 5 and _num(p.get("duration_ms")) is not None:
            tools.append(r)
        elif k == "system" and p.get("orch") == "queue" and len(queue) < limit * 3:
            queue.append(r)

    groups: dict[str, list[dict[str, Any]]] = {}
    for r in llm:
        groups.setdefault(f"{r.get('actor')}/{(r.get('payload') or {}).get('phase')}", []).append(r)
    phases = []
    for key, rs in groups.items():
        ok = [r for r in rs if not (r.get("payload") or {}).get("skipped")]
        ps = [r.get("payload") or {} for r in ok]
        tot = [x for x in (_num(q.get("total_ms")) for q in ps) if x is not None]
        ttft = [x for x in (_num(q.get("ttft_ms")) for q in ps) if x is not None]
        tin = [x for x in (_num(q.get("prompt_tokens")) for q in ps) if x is not None]
        tout = [x for x in (_num(q.get("completion_tokens")) for q in ps) if x is not None]
        phases.append({"key": key, "label": PHASE.get(key, key.replace("/", "・")), "calls": len(rs),
                       "errors": sum(1 for q in ps if q.get("error") or q.get("fell_back_to_mock")),
                       "skipped": len(rs) - len(ok), "ms": _stats(tot), "ttft_ms": _stats(ttft),
                       "tokens_in": int(sum(tin)), "tokens_out": int(sum(tout)),
                       "in_p50": _pct(tin, 0.5), "out_p50": _pct(tout, 0.5)})
    phases.sort(key=lambda x: -x["calls"])

    by_tool: dict[str, list[dict[str, Any]]] = {}
    for r in tools:
        by_tool.setdefault(str(r.get("actor", "")).removeprefix("tool:"), []).append(r.get("payload") or {})
    tool_rows = sorted(({"tool": t, **_stats([float(q["duration_ms"]) for q in qs]),
                         "errors": sum(1 for q in qs if q.get("ok") is False)} for t, qs in by_tool.items()),
                       key=lambda x: -(x["p90"] or 0))

    waits = [x for x in (_num((r.get("payload") or {}).get("waited_ms")) for r in queue) if x is not None]
    by_who: dict[str, list[float]] = {}
    for r in queue:
        p = r.get("payload") or {}
        if _num(p.get("waited_ms")) is not None:
            by_who.setdefault(str(p.get("label") or p.get("who") or "?"), []).append(float(p["waited_ms"]))

    seen: dict[str, str | None] = {}

    def sess(eid: str | None) -> str | None:
        if not eid or session_no is None:
            return None
        if eid not in seen:
            seen[eid] = _sess(eid)
        return seen[eid]

    def _sess(eid: str) -> str | None:
        try:
            got = session_no(eid)
        except Exception:  # noqa: BLE001 - a label only
            return None
        if not got:
            return None
        from warroom.api.task_sessions import session_label
        return session_label(*got)["session"]

    last = []
    for r in llm[:20]:
        p = r.get("payload") or {}
        eid = event_of(r)
        last.append({"seq": r.get("seq"), "ts": r.get("ts"), "actor": r.get("actor"), "phase": p.get("phase"),
                     "label": PHASE.get(f"{r.get('actor')}/{p.get('phase')}", f"{r.get('actor')}・{p.get('phase')}"),
                     "ms": _num(p.get("total_ms")), "ttft_ms": _num(p.get("ttft_ms")),
                     "tokens_in": p.get("prompt_tokens"), "tokens_out": p.get("completion_tokens"), "model": p.get("model"),
                     "event_id": eid, "session": sess(eid), "tool_calls": len(p["tool_calls"]) if isinstance(p.get("tool_calls"), list) else None,
                     "error": str(p.get("error"))[:120] if p.get("error") else ("改用規則" if p.get("fell_back_to_mock") else None),
                     "skipped": bool(p.get("skipped"))})
    all_p = [r.get("payload") or {} for r in llm]
    tot_ms = [x for x in (_num(q.get("total_ms")) for q in all_p if not q.get("skipped")) if x is not None]
    return {"scope": "day" if day else "recent", "day": day, "limit": limit, "scanned": scanned,
            "totals": {"calls": len(llm), "errors": sum(1 for q in all_p if q.get("error") or q.get("fell_back_to_mock")),
                       "tokens_in": int(sum(_num(q.get("prompt_tokens")) or 0 for q in all_p)),
                       "tokens_out": int(sum(_num(q.get("completion_tokens")) or 0 for q in all_p)),
                       "model_s": round(sum(tot_ms) / 1000, 1), "ms": _stats(tot_ms),
                       "first_ts": llm[-1].get("ts") if llm else None, "last_ts": llm[0].get("ts") if llm else None,
                       "tool_calls": len(tools)},
            "phases": phases, "tools": tool_rows, "last": last,
            "queue": {**_stats(waits), "over_1s": sum(1 for w in waits if w > 1000),
                      "by_who": sorted(({"who": w, **_stats(v)} for w, v in by_who.items()), key=lambda x: -(x["p90"] or 0))}}


# ---------------------------------------------------------------- 跑一次負載測試
class LoadJob:
    """bench.concurrent against the app's own model server, one run at a time, in a subprocess (never on the event loop).
    It only reads the model: no tool, no machine, no schedule is touched."""

    TIMEOUT_S = 600

    def __init__(self, results: Path = RESULTS):
        self._lock = threading.Lock()
        self.results = results
        self.state = "idle"
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: str | None = None
        self.file: str | None = None
        self.verdicts: list[dict[str, Any]] = []
        self.by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        end = self.finished_at or time.time()
        return {"state": self.state, "started_at": self.started_at, "finished_at": self.finished_at,
                "elapsed_s": round(end - self.started_at, 1) if self.started_at else None, "error": self.error,
                "file": self.file, "verdicts": self.verdicts, "by": self.by}

    @staticmethod
    def refuse(llm_mode: str, base_url: str, queue_view: dict[str, Any] | None) -> str | None:
        """Why the job may not start now, in words; None when it may."""
        if llm_mode == "mock":
            return "規則模式沒有模型可以測；以 LLM_MODE=llm 啟動才能跑"
        host = (urlparse(base_url).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return f"模型不在本機（{host or '?'}）；負載測試只打本機的模型伺服器"
        q = queue_view or {}
        if q.get("running") or q.get("waiting"):
            return "模型正在處理任務；等佇列空了再跑，免得拖慢現場"
        return None

    def start(self, base_url: str, model: str, by: str, on_done: Callable[[LoadJob], None] | None = None,
              runs: int = 3, cmd: list[str] | None = None, api_key: str | None = None) -> bool:
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.error, self.file, self.verdicts, self.by = "running", None, None, [], by
            self.started_at, self.finished_at = time.time(), None
        out_dir = self.results / f"{datetime.now(F.TZ):%Y%m%d}-spark-load"
        argv = cmd or [sys.executable, "-m", "bench.concurrent", "--runs", str(runs), "--out-dir", str(out_dir)]
        env = {**os.environ, "BENCH_BASE_URL": base_url, "BENCH_MODEL": model, "PYTHONPATH": str(REPO)}
        if api_key:
            env["API_KEY"] = api_key

        def run() -> None:
            try:
                p = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=self.TIMEOUT_S,
                                   check=False)
                saved = re.search(r"^saved (.+\.json)\s*$", p.stdout or "", re.MULTILINE)
                if p.returncode != 0 or not saved:
                    tail = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()[-3:]
                    raise RuntimeError(f"exit {p.returncode}: {' / '.join(tail)[:300]}")
                path = Path(saved.group(1).strip())
                d = json.loads(path.read_text(encoding="utf-8"))
                try:
                    self.file = str(path.resolve().relative_to(REPO))
                except ValueError:
                    self.file = str(path)
                self.verdicts = [{"name": v.get("name"), "measured": v.get("measured"), "target": v.get("target"),
                                  "verdict": _verdict(v.get("passed"), v.get("measured"))} for v in d.get("verdicts") or []]
                self.state = "done"
            except subprocess.TimeoutExpired:
                self.state, self.error = "failed", f"超過 {self.TIMEOUT_S} 秒沒跑完"
            except Exception as e:  # noqa: BLE001 - report on the job
                self.state, self.error = "failed", f"{type(e).__name__}: {e}"[:300]
            finally:
                self.finished_at = time.time()
                if on_done:
                    try:
                        on_done(self)
                    except Exception:  # noqa: BLE001, S110 - the audit row is best effort here
                        pass
        threading.Thread(target=run, name="admin-load-test", daemon=True).start()
        return True
