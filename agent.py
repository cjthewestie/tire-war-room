"""Tire war-room: 1 commander + 4 specialist agents, all sharing one model.

Code fixes the order (監控 → 排程 → 投料 ∥ 人力 → 程式閘 → 執行).
Each agent's LLM only picks from fixed options; the rule answer is the fallback.
The commander writes the supervisor note. Safety limits and permissions live in code.

Run with no model (rules only, works now):
  python3 agent.py --mock
Run with a model (Ollama on the Mac, vLLM on the Spark, or NVIDIA cloud):
  BASE_URL=http://localhost:11434/v1 MODEL=<model-name> python3 agent.py
"""
import argparse, json, os, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_URL = os.environ.get("BASE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("MODEL", "nemotron")
API_KEY = os.environ.get("NVIDIA_API_KEY", "none")
MOCK = False

# ---------- Fake factory (stands in for MES / SCADA / shift system) ----------
FACTORY = {
    "P-3": {"temp_c": 112, "trend": -0.5, "job": "medium #11", "line": "B"},
    "P-5": {"temp_c": 113, "trend": -0.4, "job": "large #02", "line": "C"},
    "P-7": {"temp_c": 115, "trend": -0.2, "job": "large #05", "line": "C"},
    "OM-1": {"temp_c": 108, "trend": 0.8, "water_delta_c": 0.6, "line": "A"},
}
SHIFTS = {"00:00-06:00": {"A": 1, "B": 3}}
LINE_START = {"A": "06:00", "B": "06:30", "C": "07:00", "D": "07:30"}

# ---------- Code gate (the LLM can never change these) ----------
HARD_STOP_C = 120
PERMISSION = {"notify_maintenance": "auto", "slow_feed": "notify", "extend_cure": "auto",
              "reschedule": "auto", "shift_feed": "notify", "move_worker": "approval", "pause_feed": "approval"}

def gate(action):
    return PERMISSION.get(action, "forbidden")

def request_approval(action, args):
    print(f"   📱 Approval card → supervisor: {action} {json.dumps(args, ensure_ascii=False)}  [auto-approved in demo]")
    return True

# ---------- Shared model call ----------
def llm(system, user, options=None):
    body = {"model": MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if options:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "pick", "schema": {
            "type": "object", "properties": {"choice": {"type": "string", "enum": options}}, "required": ["choice"]}}}
    req = urllib.request.Request(BASE_URL.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = json.load(r)["choices"][0]["message"]["content"] or ""
    if not options:
        return text.strip()
    try:
        choice = json.loads(text)["choice"]
    except Exception:
        choice = next((o for o in options if o in text), None)
    return choice if choice in options else None

def decide(system, user, options, rule_answer):
    """LLM picks one option; if it fails or --mock, the rule answers. Returns (answer, who)."""
    if MOCK:
        return rule_answer, "rule"
    try:
        pick = llm(system, user, options)
    except Exception:
        pick = None
    return (pick, "llm") if pick else (rule_answer, "rule-fallback")

# ---------- 監控 Agent: 判定紅綠燈、找原因 ----------
def monitor(machines):
    states = {m: FACTORY[m] for m in machines}
    if any(s.get("water_delta_c", 99) < 1 for s in states.values()):
        rule = "cooling_failure"
    elif len(machines) >= 3:
        rule = "boiler"
    else:
        rule = "single_machine"
    cause, who = decide("You are the monitoring agent of a solid-tire factory. Pick the most likely cause.",
                        f"Machine readings: {json.dumps(states)}",
                        ["single_machine", "boiler", "cooling_failure"], rule)
    return {"states": states, "cause": cause, "by": who}

# ---------- 排程 Agent: 排定 11 台硫化機 ----------
def cure_extension(temp_c, normal_c=120, base_min=60):
    return round(base_min * (2 ** ((normal_c - temp_c) / 10) - 1))   # rate halves every 10 C below normal

def scheduler(report):
    affected = [m for m in report["states"] if m.startswith("P-")]
    if not affected:
        return {"scope": "none", "delay_min": 0, "machines": []}
    delay = max(cure_extension(report["states"][m]["temp_c"]) for m in affected)
    rule = "affected_only" if len(affected) == 1 else "all"
    scope, who = decide("You are the scheduling agent. Choose how much of the curing schedule to re-solve.",
                        f"Affected presses: {affected}, extra cure minutes: {delay}",
                        ["affected_only", "all"], rule)
    # Placeholder for OR-Tools CP-SAT
    return {"scope": scope, "delay_min": delay, "machines": affected, "moved_jobs": 3 * len(affected), "by": who}

# ---------- 投料 Agent: 四條產線開工時間 ----------
def feeder(schedule):
    lines = sorted({FACTORY[m]["line"] for m in schedule["machines"]})
    rule = "shift_start" if schedule["delay_min"] > 0 else "keep"
    action, who = decide("You are the feeding agent. Decide whether to shift the line start time.",
                         f"Curing delay {schedule['delay_min']} min on lines {lines}",
                         ["shift_start", "keep"], rule)
    return {"action": action, "lines": lines, "shift_min": schedule["delay_min"], "by": who}

# ---------- 人力 Agent: 各組工時與班表 ----------
def staffing(schedule, window="00:00-06:00"):
    teams = SHIFTS[window]
    short = [t for t, n in teams.items() if n < 2]
    rule = "move_worker" if short and schedule["delay_min"] > 0 else "no_change"
    action, who = decide("You are the staffing agent. Decide whether a worker must be moved.",
                         f"Staff per team {teams} in {window}; curing delay {schedule['delay_min']} min",
                         ["move_worker", "no_change"], rule)
    donor = max(teams, key=teams.get)
    return {"action": action, "window": window, "from": donor, "to": short[0] if short else None, "by": who}

# ---------- 總指揮 Agent: 拆目標、派工、判斷權限、寫說明 ----------
LOG = []

def run(action, args):
    level = gate(action)
    if level == "forbidden":
        print(f"   🚫 {action} blocked by gate"); return
    if level == "approval" and not request_approval(action, args):
        print(f"   ⏸ {action} rejected"); return
    print(f"   ✅ {action} {json.dumps(args, ensure_ascii=False)} [{level}]")
    LOG.append({"action": action, "args": args, "level": level})

def commander(machines):
    print(f"\n🚨 Alarm on {machines}")
    if any(FACTORY[m]["temp_c"] >= HARD_STOP_C for m in machines):
        print("   ⛔ Hard stop (code, not LLM): pause feed, hold batch"); return

    report = monitor(machines)
    print(f"   監控 → cause = {report['cause']} (by {report['by']})")
    start = len(LOG)

    if report["cause"] == "cooling_failure":
        m = machines[0]
        run("notify_maintenance", {"machine": m, "reason": "cooling water not carrying heat away"})
        run("slow_feed", {"line": FACTORY[m]["line"], "percent": 20})
    elif report["cause"] == "boiler":
        run("notify_maintenance", {"machine": "boiler", "reason": f"{len(machines)} presses dropping together"})
        run("pause_feed", {"line": "curing"})
    else:
        schedule = scheduler(report)
        print(f"   排程 → re-solve {schedule['scope']}, +{schedule['delay_min']} min (by {schedule['by']})")
        run("extend_cure", {"machine": schedule["machines"][0], "minutes": schedule["delay_min"]})
        run("reschedule", {"machines": schedule["machines"], "scope": schedule["scope"]})
        with ThreadPoolExecutor(2) as pool:                      # 投料 ∥ 人力: both depend only on the schedule
            feed_f, staff_f = pool.submit(feeder, schedule), pool.submit(staffing, schedule)
            feed, staff = feed_f.result(), staff_f.result()
        print(f"   投料 → {feed['action']} (by {feed['by']})   人力 → {staff['action']} (by {staff['by']})")
        if feed["action"] == "shift_start":
            run("shift_feed", {"lines": feed["lines"], "minutes": feed["shift_min"]})
        if staff["action"] == "move_worker":
            run("move_worker", {"window": staff["window"], "from": staff["from"], "to": staff["to"]})

    done = LOG[start:]
    note = f"Cause: {report['cause']}. Done: {', '.join(d['action'] for d in done)}."
    if not MOCK:
        try:
            note = llm("You are the commander agent. Write a 3-line plain-language note to the factory supervisor.",
                       json.dumps({"cause": report["cause"], "actions": done}, ensure_ascii=False)) or note
        except Exception:
            pass
    print(f"   📝 {note}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--mock", action="store_true")
    MOCK = ap.parse_args().mock
    commander(["P-7"])               # one press cooling
    commander(["P-3", "P-5", "P-7"]) # boiler
    commander(["OM-1"])              # open mill overheating, cooling ineffective
    print(f"\n紀錄 {len(LOG)} actions logged")
