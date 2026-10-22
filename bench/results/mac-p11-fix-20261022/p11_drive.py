"""Drive p.11 (press7_temp_drop) N times on a running preview; approve the recommended option on every card.
Usage: p11_drive.py BASE AUDIT_PATH RUNS OUT_DIR"""
import json, sys, time
from pathlib import Path
import httpx

BASE, AUDIT, RUNS, OUT = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
OUT.mkdir(parents=True, exist_ok=True)
c = httpx.Client(base_url=BASE, timeout=30)

def log(f, *a):
    line = f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(x) for x in a)
    print(line, flush=True); f.write(line + "\n"); f.flush()

rows = []
for run in range(1, RUNS + 1):
    f = open(OUT / f"run{run}-drive.log", "w")
    start_line = sum(1 for _ in AUDIT.open()) if AUDIT.exists() else 0
    r = c.post("/api/sim/control", json={"action": "scenario", "scenario": "press7_temp_drop", "preroll": True})
    log(f, "load", r.status_code, r.text[:200])
    c.post("/api/sim/control", json={"action": "speed", "speed": 10})
    t0 = time.monotonic(); seen = set(); h2_card_s = None; h1_at = None; end = None; states = []
    while time.monotonic() - t0 < 600:
        evs = [e for e in c.get("/api/events").json() if e["machine_id"] == "CURE-07"]
        ev = evs[-1] if evs else None
        if ev:
            if not states or states[-1] != ev["state"]:
                states.append(ev["state"]); log(f, "STATE", ev["state"])
            for t in ev["timeline"]:
                if "H1 不成立" in t["text"] and h1_at is None:
                    h1_at = t["text"]; log(f, "H1REJ", t["text"])
        for a in c.get("/api/approvals", params={"status": "pending"}).json():
            if a["id"] in seen:
                continue
            seen.add(a["id"])
            hyps = [(h["id"], h["cause_code"], h["status"]) for h in (ev or {}).get("hypotheses", [])]
            log(f, "CARD", a["id"], "round", a.get("round"), a["title"], "| hyps", hyps, "| actions", a.get("actions"))
            if any(h[0] != "H1" and h[2] in ("active", "proposed") for h in hyps) and \
                    any(h[2] == "rejected" for h in hyps) and h2_card_s is None:
                h2_card_s = round(time.monotonic() - t0, 1); log(f, "H2 CARD at", h2_card_s, "s")
            opt = next((o["id"] for o in a["options"] if o.get("recommended")), a["options"][0]["id"] if a["options"] else None)
            d = c.post(f"/api/approvals/{a['id']}/decision", json={"request_id": a["id"], "decision": "approve", "option_id": opt})
            log(f, "  approve", opt, d.status_code)
        if ev and ev.get("closed_at"):
            end = ev["state"]; log(f, "END", ev["state"], ev.get("outcome")); break
        time.sleep(0.5)
    total = round(time.monotonic() - t0, 1)
    lines = AUDIT.read_text().splitlines()[start_line:]
    (OUT / f"run{run}-audit.jsonl").write_text("\n".join(lines) + "\n")
    src, calls = "-", []
    for ln in lines:
        e = json.loads(ln); p = e.get("payload", {})
        if e.get("kind") == "llm_call" and e.get("actor") == "commander":
            calls.append(f"{p.get('phase')}:{p.get('plan_source')}:{p.get('cause_code') or p.get('fallback_hypothesis')}:{round((p.get('total_ms') or 0)/1000,1)}s")
        if p.get("fell_back_to_rules"):
            src = "rules"
        if p.get("action") == "retry_with_feedback":
            calls.append("RETRY")
    ev = [e for e in c.get("/api/events").json() if e["machine_id"] == "CURE-07"][-1]
    hyps = [(h["id"], h["cause_code"], h["status"]) for h in ev["hypotheses"]]
    if src == "-" and len(hyps) > 1:
        src = "model"
    row = {"run": run, "h1_rejected": h1_at, "h2_source": src, "h2_card": h2_card_s is not None, "h2_card_s": h2_card_s,
           "end": end, "total_s": total, "hyps": hyps, "calls": calls}
    log(f, "ROW", json.dumps(row, ensure_ascii=False)); rows.append(row); f.close()
(OUT / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
