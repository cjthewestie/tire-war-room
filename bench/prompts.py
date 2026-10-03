"""Benchmark prompts built from the real factory model and SOP corpus, so the token mix matches what
the commander sees in production (Traditional Chinese factory context + JSON schema + sensor window).

The commander's own production prompt lives in warroom/agent (agent owner). The bench keeps its own
copy so it can run before, and independently of, the agent module.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from warroom import factory as F
from warroom.contracts import AlertCard, CommanderPlan, ToolName

ROOT = Path(__file__).resolve().parent.parent
SOP_DIR = ROOT / "warroom" / "kb" / "sop"

SYSTEM = (
    "你是實心胎工廠戰情室的事件指揮官（Event Commander），是系統中唯一的 LLM。"
    "你只輸出符合 JSON schema 的內容，不輸出其他文字。物理與排程數字一律由工具（硫化公式、排程求解器）計算，"
    "不可自行估計。安全核心（PLC 規則）獨立運作，你不能呼叫或覆寫它。寫入工具（確認排程、設定硫化時間、通知各組）"
    "需經 Action Gate 與主管核准。沒有任何致動器或 PLC 寫入權限。"
)


def factory_context() -> str:
    """Static plant description: 28 machines, presses, headers, cure physics, shifts."""
    lines = ["# 工廠模型（28 台機台）"]
    for m in F.MACHINES:
        extra = []
        if m.handles_compound:
            extra.append("含膠料")
        if m.steam_header:
            extra.append(f"蒸汽管線 {m.steam_header}")
            extra.append("僅小胎" if m.small_only else "中/大胎共用")
        lines.append(f"- {m.id}｜{F.LINE_LABELS[m.line]}｜{m.name}｜製程 {m.process}" +
                     (f"｜{'，'.join(extra)}" if extra else ""))
    lines += [
        f"# 硫化：小 {F.CURE_HOURS['S']} h／中 {F.CURE_HOURS['M']} h／大 {F.CURE_HOURS['L']} h，"
        f"{F.CURE_TEMP_MIN_C:.0f}–{F.CURE_TEMP_MAX_C:.0f}°C，{F.CURE_PRESSURE_KGF_CM2:.0f} kgf/cm²，"
        f"活化能 {F.ACTIVATION_ENERGY_KJ_MOL} kJ/mol（假設值）",
        f"# 安全核心：煉膠 ≥{F.COMPOUND_STOP_C:.0f}°C 停機；已加硫 ≥{F.CURATIVE_STOP_C:.0f}°C 停機；"
        f"硫化機 <{F.CURE_LOW_STOP_C:.0f}°C 保持",
        "# 班表（優化前提）",
    ]
    for s in F.OPTIMISED_SHIFTS:
        lines.append(f"- {F.TEAM_LABELS[s.team]} {s.name} {s.start:%H:%M}–{s.end:%H:%M} 人數 {s.headcount}")
    lines.append(f"# 組裝順序：{F.ASSEMBLY_ORDER}")
    return "\n".join(lines)


def schedule_facts_context() -> str:
    """Page-6 scheduling numbers (back-propagation table, per-press capacity, lead times) from warroom.factory.
    In production these come back from the scheduling solver / orders DB tools; the open-book quiz gives
    them to the model the same way so every quiz item has its source in the prompt."""
    size = {"S": "小胎", "M": "中胎", "L": "大胎"}

    def hm(minutes: int) -> str:
        h, m = divmod(minutes, 60)
        return f"T−{h} 時" + (f" {m} 分" if m else "") if h else f"T−{m} 分"

    lines = ["# 排程倒推（page 6，由入硫化機時刻 T 往前推）"]
    for k, b in F.BACKPROP.items():
        lines.append(f"- {size[str(getattr(k, 'value', k))]}：開始貼合 {hm(b['bond_start_min'])}；A 線投料 "
                     f"{hm(b['a_feed_min'])}；胎側膠／鋼絲圈各 {b['side_pieces']} 片")
    lines.append("# 每台硫化機每週產量（模擬，小／中／大）")
    for r in F.CAPACITY_TARGETS:
        lines.append(f"- {r.label}：{r.small:g}／{r.medium:g}／{r.large:g}")
    ml, sm = F.LEAD_TIME_TARGET_WEEKS["ML_100_each"], F.LEAD_TIME_TARGET_WEEKS["small"]
    lines.append(f"# 交期：中、大胎各 100 條 {ml[0]} → {ml[1]} 週；小胎 {sm[0]} → {sm[1]} 週")
    lines.append(f"# 模擬條件：{F.CAPACITY_SIM_CONDITIONS}")
    return "\n".join(lines)


def sop_context() -> str:
    parts = []
    for p in sorted(SOP_DIR.glob("*.md")):
        parts.append(f"## SOP 檔案 {p.name}\n{p.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def sensor_window(machine_id: str, start_c: float, end_c: float, minutes: int = 30,
                  t_end: datetime | None = None) -> str:
    """Deterministic 1-per-minute temperature window as CSV (what read_sensors returns)."""
    t_end = t_end or datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ)
    rows = ["ts,machine_id,temp_c,steam_kgf_cm2"]
    for i in range(minutes):
        frac = i / max(minutes - 1, 1)
        temp = start_c + (end_c - start_c) * max(0.0, frac - 0.7) / 0.3 + 0.1 * math.sin(i)
        ts = t_end - timedelta(minutes=minutes - 1 - i)
        rows.append(f"{ts:%H:%M},{machine_id},{temp:.1f},{4.8 - 0.4 * max(0.0, frac - 0.7) / 0.3:.2f}")
    return "\n".join(rows)


ANOMALIES: list[dict[str, Any]] = [
    {   # page 11
        "machine_id": F.press_id(7), "kind": "temp_drop", "channel": "temp_c", "value": 122.0, "baseline": 128.0,
        "message": "7 號硫化機 128 → 122°C，預測穩定在 122±1°C（中胎，已硫化 10 h／28 h）",
        "window": (128.0, 122.0),
    },
    {   # page 2
        "machine_id": "A-BIGOPEN", "kind": "overheat", "channel": "temp_c", "value": 112.0, "baseline": 95.0,
        "message": "A 線大開煉機膠溫 112°C 且持續上升，水流偏低（未加硫上限 120°C）",
        "window": (95.0, 112.0),
    },
    {   # third concurrent anomaly for the page-10 "同時 3 件異常" test
        "machine_id": F.press_id(3), "kind": "steam_low", "channel": "steam_kgf_cm2", "value": 3.9, "baseline": 4.8,
        "message": "3 號硫化機蒸汽壓 4.8 → 3.9 kgf/cm²，溫度 126°C 仍在範圍內（小胎）",
        "window": (127.0, 126.0),
    },
]


def live_anomaly(a: dict[str, Any], nonce: str | None = None) -> dict[str, Any]:
    """A fresh copy of anomaly `a` as a new detection would deliver it: unique event id, its own
    detection time and live sensor values (deterministic jitter derived from the nonce).

    Every alert-card benchmark call uses one of these so the model must prefill the anomaly block cold;
    only the shared system prefix (commander role + factory model) can come from the prefix cache, which
    is the real situation on the edge box. Repeating one fixed prompt measures a cache hit instead."""
    nonce = nonce or uuid.uuid4().hex[:12]
    h = int(hashlib.sha256(nonce.encode()).hexdigest()[:8], 16)
    j1 = ((h & 0xFF) / 255 - 0.5) * 0.8            # ±0.4 on the reported value
    j2 = (((h >> 8) & 0xFF) / 255 - 0.5) * 0.8     # ±0.4 on the window start
    t_end = datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ) + timedelta(seconds=(h >> 16) % 3600)
    out = dict(a)
    out["value"] = round(float(a["value"]) + j1, 1)
    out["window"] = (round(a["window"][0] + j2, 1), round(a["window"][1] + j1, 1))
    out["t_end"] = t_end
    out["event_id"] = f"EVT-{nonce}"
    return out


def anomaly_block(a: dict[str, Any]) -> str:
    head = (f"事件 {a['event_id']}｜偵測時間 {a['t_end']:%Y-%m-%d %H:%M:%S}\n") if a.get("event_id") else ""
    return (head + f"異常：{a['message']}\n機台 {a['machine_id']}，種類 {a['kind']}，通道 {a['channel']}，"
            f"現值 {a['value']}，基準 {a['baseline']}\n近 30 分鐘感測值：\n"
            f"{sensor_window(a['machine_id'], *a['window'], t_end=a.get('t_end'))}")


def alert_card_messages(a: dict[str, Any], prefix: str = "") -> list[dict[str, str]]:
    """First alert card: ≤100 tokens, must reach the screen ≤2 s (page 9)."""
    schema = json.dumps(AlertCard.model_json_schema(), ensure_ascii=False)
    return [
        {"role": "system", "content": SYSTEM + "\n\n" + factory_context() + (("\n\n" + prefix) if prefix else "")},
        {"role": "user", "content": anomaly_block(a) + "\n\n請輸出第一張告警卡（title ≤ 20 字，summary ≤ 2 行），"
                                    f"JSON schema：{schema}"},
    ]


def plan_messages(a: dict[str, Any], phase: str = "plan", context: dict[str, Any] | None = None,
                  prefix: str = "") -> list[dict[str, str]]:
    tools = ", ".join(t.value for t in ToolName)
    sys = SYSTEM + "\n\n" + factory_context() + "\n\n" + sop_context() + (("\n\n" + prefix) if prefix else "")
    user = (anomaly_block(a) + f"\n\n階段：{phase}。可用工具：{tools}。"
            "請輸出 CommanderPlan：goal、hypothesis（含可驗證的 expectation）、steps S1..Sn（depends_on、"
            "引用前步結果用 \"$S1.total_cure_h\"）、alert_card。精簡輸出：steps ≤ 6，每個 purpose ≤ 20 字，"
            "rationale ≤ 60 字，不要重複輸入資料。")
    if context:
        user += "\n\n前一輪結果：" + json.dumps(context, ensure_ascii=False)
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def diagnosis_messages(a: dict[str, Any], results: dict[str, Any], prefix: str = "") -> list[dict[str, str]]:
    """Final phone card text after verify (page 11 '7號機停用，重排中')."""
    return [
        {"role": "system", "content": SYSTEM + "\n\n" + factory_context() + (("\n\n" + prefix) if prefix else "")},
        {"role": "user", "content": anomaly_block(a) + "\n\n工具與驗證結果：" + json.dumps(results, ensure_ascii=False)
         + "\n\n請寫給主管手機的核准卡：標題、原因、已做、缺口、交期、選項 A/B，各一行。"},
    ]


def long_prefix(target_chars: int) -> str:
    """Deterministic filler that looks like production context (shift hand-off log + sensor history),
    used to reach the 32k-token context of the page-10 TTFT target."""
    out: list[str] = ["# 交班紀錄與感測歷史（長上下文）"]
    n = 0
    i = 0
    ids = F.MACHINE_IDS
    while n < target_chars:
        mid = ids[i % len(ids)]
        line = (f"{i // 60:02d}:{i % 60:02d} {mid} temp={110 + (i * 7) % 20:.1f} "
                f"press={18 + (i * 3) % 5:.1f} flow={30 + (i * 11) % 9:.1f} 狀態正常，交班人員確認。")
        out.append(line)
        n += len(line) + 1
        i += 1
    return "\n".join(out)


def plan_schema() -> dict[str, Any]:
    return CommanderPlan.model_json_schema()


def card_schema() -> dict[str, Any]:
    return AlertCard.model_json_schema()
