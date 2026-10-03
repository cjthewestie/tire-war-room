"""Tool-calling test for the tire war-room agent.

Runs 3 scripted factory incidents against an OpenAI-compatible endpoint and
counts bad tool calls. Standard library only.

Usage:
  export NVIDIA_API_KEY=nvapi-...
  python3 tool_call_test.py                 # NVIDIA cloud endpoint
  python3 tool_call_test.py --runs 3        # repeat each incident 3 times
  BASE_URL=http://<spark-ip>:8000/v1 MODEL=<name> python3 tool_call_test.py   # vLLM on the Spark
"""
import argparse, json, os, sys, time, urllib.error, urllib.request

BASE_URL = os.environ.get("BASE_URL", "https://integrate.api.nvidia.com/v1")
MODEL = os.environ.get("MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
API_KEY = os.environ.get("NVIDIA_API_KEY", "none")
MAX_TURNS = 8

SYSTEM = (
    "You are the commander agent of a solid-tire factory war room. "
    "Use tools to check facts before acting. Never guess sensor values. "
    "Actions that pause production or move workers must go through request_approval. "
    "When you are done, reply with a short plain-language summary for the supervisor (max 3 lines)."
)

def tool(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}

S = {"type": "string"}; N = {"type": "number"}; I = {"type": "integer"}
TOOLS = [
    tool("get_machine_state", "Current temperature (C), trend (C/min) and status of one machine.", {"machine_id": S}, ["machine_id"]),
    tool("get_cooling_status", "Cooling water inlet/outlet temperature difference (C) and running compressors for a machine.", {"machine_id": S}, ["machine_id"]),
    tool("start_compressor", "Start one more cooling compressor for a machine.", {"machine_id": S}, ["machine_id"]),
    tool("slow_raw_feed", "Reduce raw material feed rate on a line by a percentage (5-30).", {"line": S, "percent": I}, ["line", "percent"]),
    tool("notify_maintenance", "Send a maintenance ticket.", {"machine_id": S, "reason": S}, ["machine_id", "reason"]),
    tool("cure_extension", "Compute extra curing minutes needed when a curing press runs below 120C (Arrhenius).", {"machine_id": S, "temp_c": N}, ["machine_id", "temp_c"]),
    tool("reschedule", "Re-solve the curing schedule. scope = 'affected_only' or 'all'.", {"machine_ids": {"type": "array", "items": S}, "scope": S}, ["machine_ids", "scope"]),
    tool("check_shift", "Staff available per team for a time window like '00:00-06:00'.", {"window": S}, ["window"]),
    tool("request_approval", "Ask the supervisor to approve an action on their phone. Returns approved true/false.", {"action": S, "reason": S}, ["action", "reason"]),
]
TOOL_NAMES = {t["function"]["name"]: t["function"]["parameters"]["required"] for t in TOOLS}

# Fake factory: what each tool returns in each incident.
WORLD = {
    "open_mill_overheat": {
        "get_machine_state": {"temp_c": 108, "trend_c_per_min": 0.8, "status": "warning"},
        "get_cooling_status": {"water_delta_c": 0.6, "compressors_running": 2, "note": "delta near 0 means water is not carrying heat away"},
        "check_shift": {"A": 3, "B": 2},
    },
    "press7_temp_drop": {
        "get_machine_state": {"temp_c": 115, "trend_c_per_min": -0.2, "status": "warning", "current_job": "large tire #05"},
        "cure_extension": {"extra_minutes": 38},
        "reschedule": {"feasible": True, "moved_jobs": 3, "delay_minutes_max": 38},
        "check_shift": {"A": 1, "B": 3, "note": "team A is one person short at mold release"},
        "get_cooling_status": {"water_delta_c": 4.0, "compressors_running": 1},
    },
    "boiler_wide_drop": {
        "get_machine_state": {"temp_c": 112, "trend_c_per_min": -0.5, "status": "warning", "note": "presses 3, 5 and 7 all dropping at the same time"},
        "cure_extension": {"extra_minutes": 70},
        "reschedule": {"feasible": False, "reason": "boiler pressure low, all presses affected"},
        "check_shift": {"A": 3, "B": 3},
        "get_cooling_status": {"water_delta_c": 4.0, "compressors_running": 1},
    },
}
DEFAULT_RESULT = {"ok": True}

INCIDENTS = [
    {"id": "open_mill_overheat",
     "prompt": "ALARM 14:02: Open Mill (machine OM-1, line A) at 108C and rising. Safe limit 120C. Handle it.",
     "must_call": ["get_cooling_status", "notify_maintenance"],
     "judgment": "Water delta is 0.6C, so another compressor will not help: should NOT start_compressor; should notify maintenance and slow the feed."},
    {"id": "press7_temp_drop",
     "prompt": "ALARM 14:09: Curing press P-7 dropped to 115C (normal 120C). It is curing large tire #05. Handle it.",
     "must_call": ["cure_extension", "reschedule", "check_shift"],
     "judgment": "Only P-7 is affected: reschedule scope should be 'affected_only'."},
    {"id": "boiler_wide_drop",
     "prompt": "ALARM 14:20: Curing presses P-3, P-5 and P-7 are all dropping in temperature at the same time. Handle it.",
     "must_call": ["notify_maintenance", "request_approval"],
     "judgment": "Several presses at once points to the boiler: should notify maintenance and ask approval to pause new loading, not just extend each cure."},
]

def call_model(messages):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS,
                       "tool_choice": "auto", "temperature": 0.2, "max_tokens": 1024}).encode()
    req = urllib.request.Request(BASE_URL.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
    for attempt in range(6):  # free endpoint returns 503 when busy: back off and retry
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            return data["choices"][0]["message"], time.time() - t0, data.get("usage", {})
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503) or attempt == 5:
                raise
            time.sleep(10 * (attempt + 1))

def run_incident(inc):
    world = WORLD[inc["id"]]
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": inc["prompt"]}]
    calls, bad, total_s, final = [], [], 0.0, ""
    for _ in range(MAX_TURNS):
        msg, secs, _ = call_model(messages)
        total_s += secs
        tool_calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": tool_calls} if tool_calls else {})})
        if not tool_calls:
            final = (msg.get("content") or "").strip()
            break
        for tc in tool_calls:
            name = tc["function"]["name"]
            problem = None
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args, problem = {}, "invalid JSON arguments"
            if not problem and name not in TOOL_NAMES:
                problem = "unknown tool"
            if not problem:
                missing = [k for k in TOOL_NAMES[name] if k not in args]
                if missing:
                    problem = f"missing args {missing}"
            calls.append((name, args))
            if problem:
                bad.append(f"{name}: {problem}")
                result = {"error": problem}
            else:
                result = world.get(name, DEFAULT_RESULT)
                if name == "request_approval":
                    result = {"approved": True}
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})
    names = [c[0] for c in calls]
    missed = [m for m in inc["must_call"] if m not in names]
    return {"calls": calls, "bad": bad, "missed": missed, "seconds": total_s, "final": final}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    a = ap.parse_args()
    if API_KEY == "none" and "nvidia.com" in BASE_URL:
        sys.exit("Set NVIDIA_API_KEY first (get one at build.nvidia.com).")
    print(f"Model: {MODEL}\nEndpoint: {BASE_URL}\n")
    total_calls = total_bad = total_missed = 0
    for inc in INCIDENTS:
        for r in range(a.runs):
            try:
                res = run_incident(inc)
            except Exception as e:
                print(f"[{inc['id']} run {r+1}] request failed: {e}\n")
                continue
            total_calls += len(res["calls"]); total_bad += len(res["bad"]); total_missed += len(res["missed"])
            print(f"[{inc['id']} run {r+1}] {res['seconds']:.1f}s")
            for name, args in res["calls"]:
                print(f"   -> {name} {json.dumps(args, ensure_ascii=False)}")
            print(f"   bad calls: {res['bad'] or 'none'}")
            print(f"   missed required tools: {res['missed'] or 'none'}")
            print(f"   check by eye: {inc['judgment']}")
            print(f"   final: {res['final'][:300]}\n")
    rate = (total_bad / total_calls) if total_calls else 0
    print(f"SUMMARY: {total_calls} tool calls, {total_bad} bad ({rate:.0%}), {total_missed} required tools missed")
    print("Pass bar: bad <= 10% and no required tool missed.")

if __name__ == "__main__":
    main()
