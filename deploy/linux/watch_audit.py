"""Read the war-room audit log as one Chinese line per entry: who did what, the key numbers, and the
module responsible. A learning aid for following an event through the architecture; stdlib only.

  python3 deploy/linux/watch_audit.py              the whole data/runtime/audit.jsonl, then a summary
  python3 deploy/linux/watch_audit.py -f           follow it while the app runs (Ctrl+C to stop)
  python3 deploy/linux/watch_audit.py <file> [-f]  another audit log

Each line: #seq  factory time  [who]  what happened  ← module
"""
import collections
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLOR = {"llm": "35", "tool": "32", "gate": "33", "safety": "31", "detect": "36", "state": "34", "system": "90"}

STATE = {"detected": "偵測到", "classify": "分類", "plan": "規劃", "simulate": "模擬", "gate": "把關",
         "await_approval": "等核准", "execute": "執行", "verify": "驗證", "replan": "重規劃",
         "closed": "結案", "safe_stop": "安全停止", None: "（開始）"}

TOOL_CODE = {
    "read_sensors": "tools/impl.py（讀模擬工廠的歷史讀值）",
    "predict_anomaly": "detect/detector.py（時序預測）",
    "cure_formula": "tools/cure.py（Arrhenius 公式）",
    "query_orders_shifts": "tools/impl.py（訂單與班表）",
    "solve_schedule": "schedule/solver.py（OR-Tools CP-SAT）",
    "sop_search": "kb/store.py（Qdrant 向量搜尋）",
    "confirm_schedule": "schedule/store.py（排程版本）",
    "set_cure_time": "tools/impl.py（記錄硫化時間建議值）",
    "notify_teams": "tools/impl.py（廠內通知）",
}
KIND_CODE = {
    "anomaly": "detect/detector.py", "state_transition": "agent/state_machine.py", "llm_call": "agent/commander.py",
    "gate": "agent/gate.py", "approval_request": "agent/state_machine.py → agent/approvals.py",
    "approval_decision": "api/app.py → agent/approvals.py", "safety_action": "safety/core.py",
    "system": "api/runtime.py",
}
WHO = {"anomaly": ("detect", "事件偵測"), "state_transition": ("state", "狀態機"), "llm_call": ("llm", "指揮官"),
       "tool_call": ("tool", "工具"), "tool_result": ("tool", "工具"), "gate": ("gate", "動作把關"),
       "approval_request": ("gate", "核准卡"), "approval_decision": ("gate", "主管"),
       "safety_action": ("safety", "安全核心"), "system": ("system", "系統")}


def short(v, n=60):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_result_text(tool, p):
    d = p.get("data") or {}
    if not p.get("ok", True):
        return f"失敗：{short(p.get('error'), 80)}"
    if tool == "cure_formula":
        return f"總硫化 {d.get('total_cure_h')} h（+{d.get('extra_h')} h），速率比 {d.get('rate_ratio')}"
    if tool == "solve_schedule":
        moves = d.get("moves")
        n = len(moves) if isinstance(moves, list) else "多"
        sid = (d.get("schedule") or {}).get("id", "")
        solver = "CP-SAT" if sid.endswith("cp-sat") else (sid or "?")
        return (f"{solver}，可行={d.get('feasible')}，退回備援={d.get('fell_back')}，"
                f"交期全滿足={d.get('deliveries_met')}，異動 {n} 筆")
    if tool == "sop_search":
        hits = d.get("hits") or []
        return "；".join(f"{h.get('sop_id')} {h.get('title')}（{h.get('score')}）" for h in hits[:2]) or "沒有結果"
    if tool == "query_orders_shifts":
        return "缺口：" + ("、".join(d.get("gaps") or []) or "無")
    if tool == "read_sensors":
        summ = d.get("summary") or {}
        return "；".join(f"{m} {v.get('temp_c_now')}°C 蒸汽 {v.get('steam_kgf_cm2_now')}" for m, v in summ.items())
    if tool == "confirm_schedule":
        return f"排程 v{d.get('version')} {d.get('status')}（上一版 v{d.get('previous_version')}）"
    if tool == "set_cure_time":
        return f"{d.get('press_id')} 總硫化 {d.get('total_cure_h')} h，預計出模 {str(d.get('planned_demold_at'))[5:16]}"
    if tool == "notify_teams":
        return f"通知 {'、'.join(d.get('team_labels') or d.get('teams') or [])}：{short(d.get('message'), 40)}"
    return short(d, 80)


def describe(r):
    k, p = r.get("kind"), r.get("payload") or {}
    if k == "anomaly":
        pr = p.get("prediction") or {}
        return f"{p.get('message')}（發現延遲 {p.get('detection_latency_s')} 秒，預測區間 {pr.get('band_lo')}–{pr.get('band_hi')}）"
    if k == "state_transition":
        return f"{STATE.get(p.get('from'), p.get('from'))} → {STATE.get(p.get('to'), p.get('to'))}：{p.get('reason')}"
    if k == "llm_call":
        src = "規則（mock）" if p.get("mode") == "mock" else f"模型 {p.get('model')}"
        fb = "，⚠ 退回規則" if p.get("fell_back_to_mock") else ""
        return (f"{p.get('phase')}：{src} 寫計畫，假設 {p.get('hypothesis')}（{p.get('cause_code')}），"
                f"步驟 {','.join(x if isinstance(x, str) else str(x.get('tool')) for x in (p.get('steps') or []))}，{round(p.get('total_ms') or 0, 1)} ms{fb}")
    if k == "tool_call":
        return f"呼叫 {r.get('actor', '').split(':')[-1]}（狀態 {STATE.get(p.get('state'), p.get('state'))}）{short(p.get('args'), 70)}"
    if k == "tool_result":
        tool = r.get("actor", "").split(":")[-1]
        return f"{tool} 回傳（{round(p.get('duration_ms') or 0, 1)} ms）：{tool_result_text(tool, p)}"
    if k == "gate":
        d = p.get("decision") or {}
        return f"{d.get('tool')} 風險 {d.get('risk')} → {d.get('verdict')}"
    if k == "approval_request":
        opts = "／".join(f"{o.get('id')} {o.get('label', '')}".strip() for o in p.get("options") or [])
        return f"送出 {p.get('id')}（{p.get('risk', '')}），選項 {opts}，到期 {str(p.get('expires_at'))[11:19]}"
    if k == "approval_decision":
        return f"{p.get('decision')} 方案 {p.get('option_id')}（從 {p.get('via')}），理由：{short(p.get('reason'), 30)}"
    if k == "safety_action":
        return f"{p.get('message')}（{p.get('latency_ms')} ms，不經過 LLM）"
    if k == "system":
        rest = {kk: vv for kk, vv in p.items() if kk != "msg"}
        return f"{p.get('msg') or '紀錄'} {short(rest, 70)}"
    return short(p, 90)


def line(r, color=True):
    k = r.get("kind")
    group, who = WHO.get(k, ("system", k))
    code = TOOL_CODE.get(r.get("actor", "").split(":")[-1], "") if k in ("tool_call", "tool_result") else KIND_CODE.get(k, "")
    text = f"#{r.get('seq'):<3} {str(r.get('ts'))[11:19]}  [{who}] {describe(r)}  ← {code}"
    return f"\033[{COLOR[group]}m{text}\033[0m" if color else text


def rows(path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            yield json.loads(raw)


def summary(all_rows):
    kinds = collections.Counter(r.get("kind") for r in all_rows)
    llm = [r for r in all_rows if r.get("kind") == "llm_call"]
    fell = sum(1 for r in llm if (r.get("payload") or {}).get("fell_back_to_mock"))
    tools = collections.Counter(r.get("actor", "").split(":")[-1] for r in all_rows if r.get("kind") == "tool_call")
    print("\n小結")
    print(f"  共 {len(all_rows)} 筆；指揮官（LLM 的位置）{len(llm)} 次，其中退回規則 {fell} 次")
    print("  工具呼叫：" + "、".join(f"{t} {n}" for t, n in tools.most_common()))
    print(f"  把關 {kinds['gate']}、核准卡 {kinds['approval_request']}、主管決定 {kinds['approval_decision']}、"
          f"安全核心 {kinds['safety_action']}")


def main(argv):
    follow = "-f" in argv
    args = [a for a in argv if a != "-f"]
    path = Path(args[0]) if args else ROOT / "data" / "runtime" / "audit.jsonl"
    color = sys.stdout.isatty()
    if not follow:
        all_rows = list(rows(path))
        for r in all_rows:
            print(line(r, color))
        summary(all_rows)
        return 0
    pos = 0
    try:
        while True:
            size = path.stat().st_size if path.exists() else 0
            if size < pos:
                pos = 0                      # 情境重設後檔案變短，從頭再看
            if size > pos:
                with path.open(encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for raw in chunk.splitlines():
                    if raw.strip():
                        print(line(json.loads(raw), color), flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
