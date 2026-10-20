"""bench/: math, parsers, socket classification, and every runner end-to-end against a fake
OpenAI-compatible streaming server (no model needed). Real-model numbers live in bench/results/."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from bench import accuracy, concurrent, decode, full_event, memory_check, report, tokens_per_watt, ttft, unplug_test
from bench.llm import Endpoint, detect_kind, parse_json, stream_chat, summarize, thinking_extra
from warroom.contracts import CommanderPlan
from warroom import factory as F


# ---------------------------------------------------------------- fake OpenAI-compatible server
PLAN = {
    "goal": "7 號降溫，大胎不能延誤",
    "hypothesis": {"id": "H1", "statement": "蒸汽壓不足，溫度會穩定", "cause_code": "steam_pressure_low",
                   "expectation": {"machine_id": "CURE-07", "channel": "temp_c", "op": ">=", "value": 121,
                                   "within_min": 30}},
    "steps": [{"id": "S1", "tool": "cure_formula", "args": {"machine_id": "CURE-07"}, "purpose": "硫化公式"}],
    "alert_card": {"title": "7 號機溫度下降", "summary": "128→122°C"},
}


def fake_app(state: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/api/version")
    def version():
        return {"version": "fake-0.1"}

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        state.setdefault("bodies", []).append(body)
        prompt = json.dumps(body["messages"], ensure_ascii=False)
        if body.get("max_tokens", 0) <= 8 and "題目" not in prompt:
            text = "OK"
        elif "題目" in prompt:
            text = json.dumps({"answer": "C"})
        elif "CommanderPlan" in prompt:
            text = json.dumps(PLAN, ensure_ascii=False)
        else:
            text = json.dumps({"title": "7 號機溫度下降 128→122°C", "summary": "預測穩定在 122±1°C"},
                              ensure_ascii=False)
        pieces = [text[i:i + 8] for i in range(0, len(text), 8)]
        n_prompt = len(prompt) // 2

        async def gen():
            await asyncio.sleep(0.02)          # "prefill"
            for i, p in enumerate(pieces):
                ch = {"id": "x", "object": "chat.completion.chunk", "model": body["model"],
                      "choices": [{"index": 0, "delta": {"content": p} if i else {"role": "assistant", "content": p},
                                   "finish_reason": None}]}
                yield f"data: {json.dumps(ch, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.002)
            done = {"id": "x", "object": "chat.completion.chunk", "model": body["model"], "choices": [],
                    "usage": {"prompt_tokens": n_prompt, "completion_tokens": len(pieces),
                              "total_tokens": n_prompt + len(pieces)}}
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


@pytest.fixture(scope="module")
def fake_server():
    state: dict = {}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fake_app(state), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1", state
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def env_fake(fake_server, monkeypatch):
    url, state = fake_server
    monkeypatch.setenv("BASE_URL", url)
    monkeypatch.setenv("MODEL", "fake-nemotron")
    monkeypatch.delenv("BENCH_BASE_URL", raising=False)
    monkeypatch.delenv("BENCH_MODEL", raising=False)
    monkeypatch.delenv("BENCH_SERVER_KIND", raising=False)
    return url, state


# ---------------------------------------------------------------- pure functions
def test_arrhenius_matches_page_11():
    total = full_event.arrhenius_total_h()
    assert round(total, 1) == 36.5 and round(total - 28.0, 1) == 8.5


def test_cpsat_reschedule_never_uses_press_7():
    r = full_event.cpsat_reschedule(time_limit_s=5)
    assert r["status"] in ("OPTIMAL", "FEASIBLE")
    assert F.press_id(7) not in r["placement"].values()
    assert set(r["placement"].values()) <= set(F.ML_PRESSES)


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure: {"a": 2} done') == {"a": 2}
    assert parse_json("no json") is None


def test_summarize_and_thinking_switches():
    s = summarize([1.0, 2.0, 3.0, None])
    assert s["n"] == 3 and s["p50"] == 2.0 and s["max"] == 3.0
    assert thinking_extra("vllm", False) == {"chat_template_kwargs": {"enable_thinking": False}}
    assert thinking_extra("ollama", False) == {"think": False, "reasoning_effort": "none"}
    assert thinking_extra("openai", False) == {}


def test_detect_kind_by_port_without_probe(monkeypatch):
    monkeypatch.delenv("BENCH_SERVER_KIND", raising=False)
    assert detect_kind("http://localhost:11434/v1", probe=False) == "ollama"
    assert detect_kind("http://spark:8001/v1", probe=False) == "vllm"
    monkeypatch.setenv("BENCH_SERVER_KIND", "vllm")
    assert detect_kind("http://localhost:11434/v1") == "vllm"


def test_is_outbound():
    for ip in ("127.0.0.1", "10.1.2.3", "192.168.1.10", "172.20.0.5", "169.254.1.1", "::1", "fe80::1",
               "fd00::5", "::ffff:192.168.0.1", "224.0.0.251", "::127.0.0.1", "::10.0.0.2", "::"):
        assert not unplug_test.is_outbound(ip), ip
    for ip in ("1.1.1.1", "8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8"):
        assert unplug_test.is_outbound(ip), ip
    import ipaddress
    assert not unplug_test.is_outbound("100.64.0.9", [ipaddress.ip_network("100.64.0.0/10")])


def test_socket_watcher_counts_only_outbound():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    c = socket.create_connection(srv.getsockname())
    import os
    with unplug_test.SocketWatcher({os.getpid()}, match_roles=False, interval_s=0.02) as w:
        time.sleep(0.1)
    rep = w.report()
    c.close()
    srv.close()
    assert rep["samples"] >= 2 and rep["outbound_connections"] == 0
    assert os.getpid() in rep["watched_pids"]


def test_block_commands_per_platform():
    blk, unblk = unplug_test.block_commands("ip link set eth1 down", "ip link set eth1 up")
    assert blk[-1] == "ip link set eth1 down" and unblk[-1] == "ip link set eth1 up"


def test_linux_block_keeps_boot_firewall(monkeypatch):
    monkeypatch.setattr(unplug_test.platform, "system", lambda: "Linux")
    # boot service already loaded the table: neither re-load (duplicate rules) nor delete it afterwards
    blk, unblk = unplug_test.block_commands(None, None, table_loaded=True)
    assert blk == ["true"] and unblk == ["true"]
    assert "delete" not in unblk
    # no table yet: the test applies it and removes only what it added
    blk, unblk = unplug_test.block_commands(None, None, table_loaded=False)
    assert blk[:2] == ["nft", "-f"] and unblk == ["nft", "delete", "table", "inet", "warroom_offline"]
    monkeypatch.setattr(unplug_test, "_run", lambda cmd, timeout=10: (cmd[:3] == ["nft", "list", "table"], ""))
    assert unplug_test.block_commands(None, None) == (["true"], ["true"])


def test_tokens_per_watt_math_and_meter(tmp_path):
    e = tokens_per_watt.efficiency(6000, 60.0, 200.0, idle_w=50.0)
    assert e["tokens_per_s"] == 100.0
    assert e["tokens_per_joule"] == pytest.approx(0.5)
    assert e["tokens_per_s_per_watt"] == pytest.approx(0.5)
    assert e["net_tokens_per_joule"] == pytest.approx(6000 / (150 * 60), rel=1e-3)
    assert "tokens_per_joule" not in tokens_per_watt.efficiency(10, 1.0, None)
    csv = tmp_path / "meter.csv"
    csv.write_text("ts,watts\n100,150\n101,170\n102,190\n200,999\n2026-10-03T10:00:00+08:00,5\n")
    assert tokens_per_watt.meter_average(csv, 100, 102) == 170.0


def test_memory_budget_and_parsers():
    assert memory_check.budget_total() == 49.0
    assert memory_check.budget_total() <= memory_check.BAND_GB[1]
    assert memory_check.ROLES["model_server"][0] == 30
    assert memory_check.parse_size("1.5GiB") == pytest.approx(1.5)
    assert memory_check.parse_size("512MiB") == pytest.approx(0.5)
    assert memory_check.classify("/usr/bin/python3 -m vllm.entrypoints.openai.api_server") == "model_server"
    assert memory_check.classify("/usr/local/bin/ollama runner --model x") == "model_server"
    assert memory_check.classify("./qdrant") == "qdrant"
    assert memory_check.classify("python -m warroom.api serve") == "app"
    assert memory_check.role_of("zsh", ["zsh", "-c", "uv run python -m warroom.api"]) is None
    m = memory_check.measure()
    assert set(m["roles"]) == {"model_server", "qdrant", "app", "os_and_other"}


def test_quiz_loading_shapes(tmp_path):
    seed = accuracy.load_quiz(accuracy.SEED)
    assert len(seed) >= 40 and all(q["answer_letter"] in "ABCD" for q in seed)
    j = tmp_path / "q.json"
    j.write_text(json.dumps({"items": [
        {"id": "a", "q": "x?", "options": {"A": "1", "B": "2"}, "answer": 1},
        {"id": "b", "question": "y?", "choices": ["甲", "乙"], "answer": "乙"},
        {"id": "c", "question": "z?", "answer": "36.5 小時", "keywords": ["36.5"]},
    ]}, ensure_ascii=False))
    items = accuracy.load_quiz(j)
    assert [i.get("answer_letter") for i in items] == ["B", "B", None]
    assert accuracy.score_free("總共約 36.5 小時", items[2]) and not accuracy.score_free("約 30 小時", items[2])


def test_find_quiz_prefers_kb_file(tmp_path, monkeypatch):
    monkeypatch.setattr(accuracy, "ROOT", tmp_path)
    assert accuracy.find_quiz() == accuracy.SEED
    (tmp_path / "warroom" / "kb" / "quiz").mkdir(parents=True)
    kb = tmp_path / "warroom" / "kb" / "quiz" / "factory_quiz.jsonl"
    kb.write_text('{"question": "q", "choices": ["a","b"], "answer": "A"}\n')
    assert accuracy.find_quiz() == kb


# ---------------------------------------------------------------- runners against the fake server
async def test_stream_chat_measures_ttft(env_fake):
    url, state = env_fake
    ep = Endpoint.from_env()
    assert ep.kind == "ollama"                           # probed via /api/version
    r = await stream_chat(ep, [{"role": "user", "content": "hi CommanderPlan"}], max_tokens=50)
    assert r.ok and r.ttft_s is not None and 0 < r.ttft_s < r.total_s
    assert r.completion_tokens and r.prompt_tokens
    assert CommanderPlan.model_validate(parse_json(r.text))
    assert state["bodies"][-1]["reasoning_effort"] == "none"


async def test_stream_chat_reports_errors_instead_of_raising():
    ep = Endpoint("http://127.0.0.1:9/v1", "x", kind="openai")
    r = await stream_chat(ep, [{"role": "user", "content": "hi"}], timeout_s=2)
    assert not r.ok and r.error


def test_ttft_runner(env_fake, tmp_path):
    out = ttft.main(["--runs", "2", "--context-tokens", "2000", "--out-dir", str(tmp_path)])
    names = {v["name"]: v for v in out["verdicts"]}
    assert names["first_alert_card_p90"]["passed"] is True
    assert "ttft_warm_32k_p90" in names and "ttft_cold_32k_p50" in names
    assert names["ttft_cold_32k_p50"]["passed"] is None
    lc = out["long_context"]
    assert names["ttft_warm_32k_p90"]["measured"] == lc["warm_ttft_s"]["p90"]
    assert out["alert_card"]["valid_card_rate"] == 1.0
    saved = json.loads(Path(out["saved_to"]).read_text())
    assert saved["bench"] == "ttft" and saved["host"]["hostname"]


def test_concurrent_runner(env_fake, tmp_path):
    out = concurrent.main(["--runs", "1", "--with-plan", "--out-dir", str(tmp_path)])
    assert out["alert_each_s"]["n"] == F.CONCURRENT_ANOMALIES
    assert out["verdicts"][0]["passed"] is True


def test_full_event_pipeline_breakdown(env_fake, tmp_path):
    out = full_event.main(["--runs", "1", "--mode", "pipeline", "--out-dir", str(tmp_path)])
    run = out["runs"][0]
    assert run["llm_ok"] and out["plan_valid_rate"] == 1.0
    assert run["rounds"]["compute"]["cure_formula"] == {"total_cure_h": 36.5, "extra_h": 8.5}
    assert "state_machine" not in out
    assert [v["name"] for v in out["verdicts"]] == ["full_event_p50"]


@pytest.mark.timeout(600)
def test_full_event_headline_is_the_state_machine(env_fake, tmp_path):
    """Default mode runs the real Runtime (every module real) against the endpoint; the headline
    full_event_p50 verdict comes from it, the scripted pipeline is only pipeline_p50."""
    out = full_event.main(["--runs", "1", "--out-dir", str(tmp_path)])
    sm = out["state_machine"]["runs"][0]
    assert "skipped" not in sm, sm
    assert sm["llm_calls"] and all(c["phase"] for c in sm["llm_calls"])     # the commander really went over HTTP
    assert sm["tool_calls"] > 0 and sm["compute_s"] >= sm["llm_s"] > 0
    assert sm["page11_checks"]["audit_chain_ok"] is True
    names = [v["name"] for v in out["verdicts"]]
    assert names == ["full_event_p50", "pipeline_p50"]
    head = out["verdicts"][0]
    if out["state_machine"]["closed_rate"]:
        assert head["measured"] == out["state_machine"]["compute_s"]["p50"]
    else:                                   # the fake model's toy plan need not close the event: a FAIL
        assert head["passed"] is False and "0/1 closed" in head["note"]
        assert head["measured"] == out["state_machine"]["compute_s_all_runs"]["p50"]


def test_accuracy_compare_runner(env_fake, tmp_path):
    url, _ = env_fake
    out = accuracy.main(["--compare", f"{url}|a", f"{url}|b", "--limit", "10", "--out-dir", str(tmp_path)])
    a, b = out["results"]
    assert a["n"] == 10 and a["score_points"] == b["score_points"]     # fake always answers C
    assert out["gap_points"] == 0 and out["verdicts"][0]["passed"] is True


def test_tokens_per_watt_runner(env_fake, tmp_path):
    out = tokens_per_watt.main(["--duration", "0.5", "--concurrency", "2", "--watts", "120", "--out-dir",
                                str(tmp_path)])
    assert out["plug"]["tokens"] > 0 and out["plug"]["tokens_per_joule"] > 0


def test_unplug_runner_direct(env_fake, tmp_path):
    out = unplug_test.main(["--runs", "2", "--watch", "0", "--out-dir", str(tmp_path)])
    v = {x["name"]: x for x in out["verdicts"]}
    assert v["alert_offline"]["passed"] is True
    assert v["outbound_connections"]["extra"]["socket_samples"] == out["sockets"]["outbound_connections"]
    assert out["block"]["applied"] is False
    # no block applied -> socket samples are not evidence of 0 outbound: verdict is None, never True
    assert v["outbound_connections"]["passed"] is None
    assert out["egress"]["firewall_dropped_packets"] is None


def test_unplug_watch_window_is_fixed(env_fake, tmp_path):
    out = unplug_test.main(["--runs", "1", "--watch", "1.5", "--out-dir", str(tmp_path)])
    assert out["watched_for_s"] >= 1.5 and out["sockets"]["samples"] >= 10


def _hex4(ip: str, port: int) -> str:
    import ipaddress as _ip
    return _ip.IPv4Address(ip).packed[::-1].hex().upper() + f":{port:04X}"


def test_linux_proc_reader_uses_the_process_own_netns(tmp_path):
    """Container sockets live in /proc/<pid>/net/tcp of that pid's namespace, not the host /proc/net/tcp."""
    pid = 4242
    fd = tmp_path / str(pid) / "fd"
    fd.mkdir(parents=True)
    os.symlink("socket:[111]", fd / "3")
    os.symlink("socket:[222]", fd / "4")
    os.symlink("/dev/null", fd / "5")
    net = tmp_path / str(pid) / "net"
    net.mkdir()
    hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    rows = [
        f"   0: {_hex4('0.0.0.0', 6333)} {_hex4('0.0.0.0', 0)} 0A 00000000:00000000 00:00000000 00000000  1000 0 111",
        f"   1: {_hex4('172.18.0.3', 51000)} {_hex4('140.82.112.3', 443)} 02 00000000:00000000 00:00000000 00000000  1000 0 222",
        f"   2: {_hex4('172.18.0.3', 51001)} {_hex4('8.8.8.8', 53)} 01 00000000:00000000 00:00000000 00000000  1000 0 999",
    ]
    (net / "tcp").write_text(hdr + "\n".join(rows) + "\n")
    six = "0000000000000000FFFF00000100000A"          # ::ffff:10.0.0.1, 4 little-endian words
    (net / "tcp6").write_text(hdr + f"   0: {six}:1F90 {'0' * 32}:0000 0A 0:0 0:0 0 0 0 333\n")
    conns = unplug_test.linux_inet_conns(pid, proc=tmp_path)
    assert {(c["lport"], c["status"]) for c in conns} == {(6333, "LISTEN"), (51000, "SYN_SENT")}
    out = [c for c in conns if c["rip"] and unplug_test.is_outbound(c["rip"])]
    assert out == [{"proto": "tcp", "lip": "172.18.0.3", "lport": 51000, "rip": "140.82.112.3", "rport": 443,
                    "status": "SYN_SENT"}]                # inode 999 belongs to another process: ignored
    assert unplug_test.listen_ports(conns) == {6333}
    assert unplug_test._hex_addr(f"{six}:1F90") == ("::ffff:10.0.0.1", 8080)


def test_firewall_and_conntrack_parsers():
    nft = "table inet warroom_offline {\n\tcounter dropped_egress {\n\t\tpackets 3 bytes 180\n\t}\n}"
    assert unplug_test.parse_nft_counter(nft) == {"packets": 3, "bytes": 180}
    assert unplug_test.parse_nft_counter("Error: No such file") is None
    pf = ("pass out quick to <lan> flags S/SA keep state\n"
          "  [ Evaluations: 100       Packets: 50        Bytes: 4000        States: 2     ]\n"
          "block drop out log quick all\n"
          "  [ Evaluations: 30        Packets: 7         Bytes: 420         States: 0     ]\n")
    assert unplug_test.parse_pf_block_packets(pf) == 7
    assert unplug_test.parse_pf_block_packets("") is None
    ct = ("tcp      6 117 SYN_SENT src=172.18.0.3 dst=140.82.112.3 sport=51000 dport=443 [UNREPLIED] "
          "src=140.82.112.3 dst=192.168.1.5 sport=443 dport=51000 mark=0 use=1\n"
          "udp      17 29 src=192.168.1.5 dst=192.168.1.1 sport=5353 dport=53 src=192.168.1.1 dst=192.168.1.5\n")
    assert unplug_test.parse_conntrack(ct) == {("tcp", "140.82.112.3", 443)}


def _unplug_out(applied, socks=0, dropped=None, flows=None, app=None):
    return {"block": {"applied": applied}, "sockets": {"outbound_connections": socks},
            "egress": {"firewall_dropped_packets": dropped, "conntrack_available": flows is not None,
                       "conntrack_outbound_flows": [{}] * (flows or 0)},
            "internet_reachable_during_test": not applied, "app": app or {}}


def test_unplug_verdicts_use_firewall_evidence_and_commander_card():
    v = {x["name"]: x for x in unplug_test.verdicts(_unplug_out(False), 0.5, False)}
    assert v["outbound_connections"]["passed"] is None and "internet_blocked" not in v
    v = {x["name"]: x for x in unplug_test.verdicts(_unplug_out(True, dropped=0, flows=0), 0.5, False)}
    assert v["outbound_connections"]["passed"] is True and v["internet_blocked"]["passed"] is True
    v = {x["name"]: x for x in unplug_test.verdicts(_unplug_out(True, dropped=4, flows=0), 0.5, False)}
    assert v["outbound_connections"]["passed"] is False and v["outbound_connections"]["measured"] == 4
    v = {x["name"]: x for x in unplug_test.verdicts(_unplug_out(True, dropped=None), 0.5, False)}
    assert v["outbound_connections"]["passed"] is None
    # app path: the 2 s target is judged on the commander card; placeholder kept as extra; TTFT from audit
    app = {"placeholder_after_anomaly_s": 0.017, "alert_after_anomaly_s": 10.1, "source_flag_present": True,
           "commander_call": {"mode": "llm", "ttft_ms": 3193, "total_ms": 10118}}
    v = {x["name"]: x for x in unplug_test.verdicts(_unplug_out(False, app=app), 10.1, True)}
    assert v["alert_offline"]["passed"] is False and v["alert_offline"]["extra"]["placeholder_after_anomaly_s"] == 0.017
    ct = v["commander_ttft_app_prompt"]
    assert ct["measured"] == 3.193 and ct["passed"] is None and "commander_ttft" not in v


def test_card_source():
    assert unplug_test.card_source({"source": "detector"}, 3) == "detector"
    assert unplug_test.card_source({"source": "commander"}, 0) == "commander"
    assert unplug_test.card_source({}, 0) == "detector" and unplug_test.card_source({}, 1) == "commander"


async def test_state_machine_tags_alert_card_source(tmp_path):
    """The instant detector placeholder and the commander's card are told apart on /ws (source flag)."""
    from tests.test_agent import auto_approve, make_sm, page11_anomaly
    from warroom.contracts import WsType
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path)
    sup = asyncio.create_task(auto_approve(approvals, 3))
    try:
        await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    except Exception:  # noqa: BLE001 - only the published cards matter here
        pass
    sup.cancel()
    cards = [m.data for m in sm.bus.history if m.type is WsType.ALERT_CARD]
    assert cards[0]["source"] == "detector" and cards[0]["card"]["title"].endswith("異常")
    assert any(c["source"] == "commander" for c in cards[1:])


def test_report_builds_table(env_fake, tmp_path):
    ttft.main(["--runs", "1", "--skip-long", "--out-dir", str(tmp_path)])
    memory_check.main(["--out-dir", str(tmp_path)])
    text = report.build(tmp_path)
    assert "| ttft | first_alert_card_p90 |" in text and "| memory | stack_plus_os_gb |" in text


# ---------------------------------------------------------------- cold vs warm, batching vs queuing
def test_live_anomaly_makes_every_alert_prompt_unique_but_keeps_system_prefix():
    from bench.prompts import ANOMALIES, commander_messages, live_anomaly
    from warroom.agent import plant
    from warroom.agent.commander import SYSTEM_PROMPT
    msgs = [commander_messages(live_anomaly(ANOMALIES[0])) for _ in range(5)]
    assert len({m[1]["content"] for m in msgs}) == 5                 # request differs every call
    # commander's own system text: the fixed core (cacheable prefix) + the active PLANT.md section (issue #37)
    assert {m[0]["content"] for m in msgs} == {SYSTEM_PROMPT + plant.section()}
    assert all(m[0]["content"].startswith(SYSTEM_PROMPT) for m in msgs)
    assert all(json.loads(m[1]["content"])["event_id"].startswith("EVT-") for m in msgs)
    a = live_anomaly(ANOMALIES[0], nonce="x")
    assert a == live_anomaly(ANOMALIES[0], nonce="x")                 # deterministic per nonce
    assert abs(a["value"] - ANOMALIES[0]["value"]) <= 0.4


def test_bench_and_preload_send_the_commanders_exact_call():
    """Finding: bench/preload used their own short SYSTEM, so the prefix cache was warmed for a prompt the
    commander never sends. Now every judged bench and the preload go through commander.build_messages."""
    from bench import preload
    from bench.prompts import (ANOMALIES, COMMANDER_ALLOWED_TOOLS, commander_format, commander_messages,
                               commander_request, live_anomaly)
    from warroom.agent.commander import SYSTEM_PROMPT, OpenAICommander, build_messages
    from warroom.config import Settings
    from warroom.contracts import EventState, TOOLS_BY_STATE, WRITE_TOOLS
    for a in ANOMALIES:
        la = live_anomaly(a, nonce="t")
        req = commander_request(la)
        assert commander_messages(la) == build_messages(req)
        ctx = req.context
        assert {"machine", "sensors", "sop"} <= set(ctx) and ctx["sop"] and ctx["machine"]["id"] == a["machine_id"]
    assert COMMANDER_ALLOWED_TOOLS == sorted(TOOLS_BY_STATE[EventState.SIMULATE] | WRITE_TOOLS, key=lambda t: t.value)
    pre = preload.preload_messages()
    from warroom.agent import plant
    assert pre and all(m[0]["content"] == SYSTEM_PROMPT + plant.section() for m in pre)
    import httpx
    cmd = OpenAICommander(Settings(base_url="http://127.0.0.1:9/v1", model="m"), http_client=httpx.AsyncClient())
    kw = cmd.request_kwargs(commander_request(live_anomaly(ANOMALIES[0], nonce="t")))
    assert kw["response_format"] == commander_format() and kw["max_tokens"] == 1200


def test_stream_chat_times_alert_card_inside_plan_stream(env_fake):
    import asyncio as _a
    from bench.prompts import ANOMALIES, COMMANDER_MAX_TOKENS, commander_format, commander_messages, live_anomaly
    ep = Endpoint.from_env()
    r = _a.run(stream_chat(ep, commander_messages(live_anomaly(ANOMALIES[0])), max_tokens=COMMANDER_MAX_TOKENS,
                           response_format=commander_format(), watch_alert_card=True))
    assert r.ok and r.alert_card == PLAN["alert_card"]
    assert r.ttft_s <= r.alert_card_s <= r.total_s and not r.stopped_early
    r2 = _a.run(stream_chat(ep, commander_messages(live_anomaly(ANOMALIES[0])), max_tokens=COMMANDER_MAX_TOKENS,
                            response_format=commander_format(), stop_on_alert_card=True))
    assert r2.ok and r2.stopped_early and r2.alert_card == PLAN["alert_card"]


def test_ttft_alert_card_judged_on_cold_unique_prompts(env_fake, tmp_path):
    from warroom.agent.commander import SYSTEM_PROMPT
    _, state = env_fake
    before = len(state.setdefault("bodies", []))
    out = ttft.main(["--runs", "3", "--skip-long", "--out-dir", str(tmp_path)])
    ac = out["alert_card"]
    assert ac["cold"]["card_s"]["n"] == 3 and ac["warm"]["card_s"]["n"] == 3
    ids = ac["cold_event_ids"]
    assert len(set(ids)) == 3
    bodies = state["bodies"][before:]
    users = [b["messages"][1]["content"] for b in bodies
             if len(b["messages"]) == 2 and b["messages"][0]["content"].startswith(SYSTEM_PROMPT)
             and b.get("response_format", {}).get("json_schema", {}).get("name") == "CommanderPlan"]
    for i in ids:                                                   # each cold call was the commander's, own event id
        assert sum(i in u for u in users) == 1
    v = {x["name"]: x for x in out["verdicts"]}
    assert v["first_alert_card_p90"]["measured"] == ac["cold"]["card_s"]["p90"]
    assert v["first_alert_card_warm_p90"]["passed"] is None          # warm is info only
    assert v["micro_alert_card_p90"]["passed"] is None and "micro" in ac["micro"]["label"].lower()


def test_batching_detects_queued_calls():
    def e(mid, ft, end):
        return {"machine_id": mid, "alert": {"ok": True}, "send_off_s": 0.0, "first_token_off_s": ft,
                "end_off_s": end}
    batched = concurrent.batching([e("a", 0.9, 1.5), e("b", 1.0, 1.6), e("c", 1.1, 1.7)])
    assert batched["batched"] is True and batched["serialized_pairs"] == []
    queued = concurrent.batching([e("a", 0.6, 1.2), e("b", 1.3, 1.9), e("c", 2.0, 2.6)])  # page-10 failure shape
    assert queued["batched"] is False and ("a", "b") in queued["serialized_pairs"]


def test_concurrent_runner_reports_batching(env_fake, tmp_path):
    out = concurrent.main(["--runs", "1", "--out-dir", str(tmp_path)])
    v = {x["name"]: x for x in out["verdicts"]}
    assert "concurrent_batched" in v
    ids = [e["event_id"] for r in out["rounds"] for e in r["each"]]
    assert len(set(ids)) == len(ids) == F.CONCURRENT_ANOMALIES


# ---------------------------------------------------------------- accuracy: open book headline, paired stats
def test_generated_quiz_is_large_deterministic_and_matches_file():
    from bench import quiz_gen
    a, b = quiz_gen.generate(), quiz_gen.generate()
    assert a == b and len(a) >= 200 and len({i["id"] for i in a}) == len(a)
    assert all(len(set(i["choices"])) == 4 and i["answer"] in "ABCD" for i in a)
    on_disk = accuracy.load_quiz(accuracy.GENERATED)
    assert [i["id"] for i in on_disk] == [i["id"] for i in a]          # committed file is current


def test_default_quiz_is_kb_plus_generated():
    paths = accuracy.find_quizzes()
    assert paths[-1].name == accuracy.GENERATED.name and len(paths) == 2
    items = accuracy.load_quizzes(paths)
    assert len(items) >= 240 and len({i["id"] for i in items}) == len(items)
    assert accuracy.find_quizzes(generated=False) == paths[:1]


def test_quant_labels_mxfp8_is_not_fp8():
    assert accuracy.quant_of("nemotron-3.5-lightning:30b-a3b-mxfp8") == "mxfp8"
    assert accuracy.quant_of("nemotron-3.5-lightning:30b-a3b-nvfp4") == "nvfp4"
    assert accuracy.quant_of("nemotron", "FP8") == "fp8" and accuracy.quant_of("nemotron") == "unknown"


def test_paired_gap_reproduces_the_44_item_open_book_run():
    # 2026-10-03 Mac run: NVFP4 36/44 vs MXFP8 40/44 = 9.09 points. With 4 discordant items all one way the
    # interval must be wide and the 1-item resolution 2.27 points.
    a = [{"id": str(i), "correct": i >= 8} for i in range(44)]
    b = [{"id": str(i), "correct": i >= 4} for i in range(44)]
    st = accuracy.paired_gap(a, b)
    assert st["gap_points"] == 9.09 and st["diff_points"] == -9.09 and st["points_per_item"] == 2.27
    assert (st["only_a_correct"], st["only_b_correct"]) == (0, 4)
    lo, hi = st["ci95_points"]
    assert lo < -9.09 < hi and lo < -15 and hi > 0     # wide, and includes 0: not significant at n=44
    assert st["mcnemar_exact_p"] == 0.125
    same = accuracy.paired_gap(a, a)
    assert same["gap_points"] == 0 and same["ci95_points"][0] < 0 < same["ci95_points"][1]   # never zero-width


def test_noninferiority_verdict_three_way():
    def run(model, flags):
        return {"endpoint": {"model": model}, "quant": accuracy.quant_of(model), "score_points": 0,
                "items": [{"id": str(i), "correct": f} for i, f in enumerate(flags)]}
    big_same = [True] * 240 + [False] * 10
    st, v = accuracy.compare_verdicts(run("m-nvfp4", big_same), run("m-mxfp8", big_same), "open-book (headline)")
    assert v[0]["name"] == "nvfp4_vs_mxfp8_gap" and v[0]["passed"] is True
    assert v[1]["name"] == "nvfp4_vs_mxfp8_noninferiority_ci95" and v[1]["passed"] is True
    assert "MXFP8" in v[0]["note"] and "not the page-10 FP8" in v[0]["note"]
    small_a, small_b = [i >= 8 for i in range(44)], [i >= 4 for i in range(44)]
    _, v = accuracy.compare_verdicts(run("m-nvfp4", small_a), run("m-fp8", small_b), "x")
    assert v[0]["passed"] is False and v[1]["passed"] is None and "inconclusive" in v[1]["note"]
    worse_a = [i >= 60 for i in range(250)]
    _, v = accuracy.compare_verdicts(run("m-nvfp4", worse_a), run("m-fp8", [True] * 250), "x")
    assert v[1]["passed"] is False


def test_accuracy_defaults_to_open_book_and_pairs_saved_runs(env_fake, tmp_path):
    url, state = env_fake
    one = accuracy.main(["--base-url", url, "--model", "x-nvfp4", "--limit", "6", "--out-dir", str(tmp_path / "a")])
    assert one["open_book"] is True and one["mode"].startswith("open-book")
    assert one["results"][0]["quant"] == "nvfp4"
    assert "SOP 檔案" in state["bodies"][-1]["messages"][0]["content"]           # SOP corpus in the prompt
    two = accuracy.main(["--base-url", url, "--model", "x", "--quant", "fp8", "--limit", "6",
                         "--out-dir", str(tmp_path / "b")])
    assert two["results"][0]["quant"] == "fp8"
    paired = accuracy.main(["--pair", one["saved_to"], two["saved_to"], "--out-dir", str(tmp_path)])
    assert paired["paired"]["n_paired"] == 6 and paired["verdicts"][0]["name"] == "nvfp4_vs_fp8_gap"
    closed = accuracy.main(["--base-url", url, "--model", "x", "--closed-book", "--limit", "2",
                            "--out-dir", str(tmp_path / "c")])
    assert closed["open_book"] is False and "SOP 檔案" not in state["bodies"][-1]["messages"][0]["content"]
    with pytest.raises(SystemExit):
        accuracy.main(["--pair", one["saved_to"], closed["saved_to"], "--out-dir", str(tmp_path)])


# ---------------------------------------------------------------- memory verdict (review: no PASS on a partial stack)
def _roles(ms=29.0, app=1.5, app_pids=(1,), q=0.5, q_pids=(2,)):
    r = lambda gb, pids: {"measured_gb": gb, "container_gb": 0.0, "pids": list(pids)}  # noqa: E731
    return {"model_server": r(ms, (9,)), "app": r(app, app_pids), "qdrant": r(q, q_pids)}


def test_memory_judge_needs_all_roles_full_context_and_both_band_edges():
    j = memory_check.judge
    req = ["model_server", "app", "qdrant"]
    ok = j(_roles(), 31.0, 10.0, "measured", [32768], req)
    assert ok["verdict"] == "PASS" and ok["total_gb"] == 41.0
    # app not running -> never PASS
    assert j(_roles(app=0.0, app_pids=()), 29.5, 10.0, "budget", [32768], req)["verdict"] == "UNJUDGED"
    # 16k context (the 2026-10-03 local run) or unknown context -> UNJUDGED
    short = j(_roles(), 31.0, 10.0, "budget", [16384], req)
    assert short["verdict"] == "UNJUDGED" and "16384" in short["reasons"][0]
    assert j(_roles(), 31.0, 10.0, "budget", [], req)["verdict"] == "UNJUDGED"
    # both band edges
    assert j(_roles(), 20.0, 10.0, "measured", [32768], req)["verdict"] == "FAIL"     # 30 < 36
    assert j(_roles(), 45.0, 10.7, "measured", [32768], req)["verdict"] == "FAIL"     # 55.7 > 50
    # OS basis is what the caller passed, recorded verbatim
    assert ok["os_basis"] == "measured"


def test_memory_ignores_foreign_bundled_qdrant_and_embedded_qdrant_is_not_required():
    assert memory_check.classify("/Applications/OpenWhispr.app/Contents/Resources/bin/qdrant-darwin-arm64") is None
    assert memory_check.classify("/qdrant/qdrant --config-path x") == "qdrant"
    m = memory_check.measure("budget")
    j = m["judgement"]
    assert j["os_basis"] == "budget" and j["os_gb"] == 10.0
    if not memory_check.qdrant_external():
        assert m["roles"]["qdrant"]["embedded_in"] == "app" and "qdrant" not in j["required_roles"]
    assert m["stack_within_band"] is {"PASS": True, "FAIL": False}.get(j["verdict"])


# ---------------------------------------------------------------- unplug: attempts, attributed (not box packets)
_DROP_LOG = """\
2026-10-03T02:14:01+0800 spark kernel: warroom-egress-drop host IN= OUT=enP7s7 SRC=192.168.1.20 DST=91.189.91.157 LEN=76 TOS=0x00 PREC=0x00 TTL=64 ID=1 DF PROTO=UDP SPT=51000 DPT=123 LEN=56 UID=998 GID=998
2026-10-03T02:14:02+0800 spark kernel: warroom-egress-drop host IN= OUT=enP7s7 SRC=192.168.1.20 DST=185.125.188.55 LEN=60 TOS=0x00 PREC=0x00 TTL=64 ID=2 DF PROTO=TCP SPT=40100 DPT=443 WINDOW=64240 RES=0x00 SYN URGP=0 UID=0 GID=0
2026-10-03T02:14:03+0800 spark kernel: warroom-egress-drop host IN= OUT=enP7s7 SRC=192.168.1.20 DST=185.125.188.55 LEN=60 TOS=0x00 PREC=0x00 TTL=64 ID=3 DF PROTO=TCP SPT=40100 DPT=443 WINDOW=64240 RES=0x00 SYN URGP=0 UID=0 GID=0
2026-10-03T02:14:05+0800 spark kernel: warroom-egress-drop fwd IN=br-edge OUT=enP7s7 SRC=172.19.0.4 DST=1.1.1.1 LEN=60 TOS=0x00 PREC=0x00 TTL=63 ID=4 DF PROTO=TCP SPT=33000 DPT=443 WINDOW=64240 RES=0x00 SYN URGP=0
2026-10-03T02:14:06+0800 spark kernel: warroom-egress-drop fwd IN=br-edge OUT=enP7s7 SRC=172.19.0.4 DST=1.1.1.1 LEN=60 TOS=0x00 PREC=0x00 TTL=63 ID=5 DF PROTO=TCP SPT=33000 DPT=443 WINDOW=64240 RES=0x00 SYN URGP=0
2026-10-03T02:14:07+0800 spark kernel: some unrelated line
"""


def test_drop_log_counts_attempts_not_packets_and_attributes_os_noise():
    att = unplug_test.parse_drop_log(_DROP_LOG)
    assert len(att) == 3                                     # 5 packets: SYN + retransmit share a source port
    by = {(a["chain"], a["dst"]): a for a in att}
    assert by[("containers", "1.1.1.1")]["packets"] == 2 and by[("host", "185.125.188.55")]["uid"] == 0
    assert by[("host", "91.189.91.157")]["uid"] == 998 and by[("host", "91.189.91.157")]["proto"] == "udp"
    # stack runs in containers; no stack process on the host -> timesyncd (998) and snapd (root) are OS noise
    stack, other = unplug_test.attribute_drops(att, set())
    assert [a["dst"] for a in stack] == ["1.1.1.1"] and len(other) == 2
    # a stack process on the host as uid 998 claims that uid's drop only
    stack, other = unplug_test.attribute_drops(att, {998})
    assert {a["dst"] for a in stack} == {"1.1.1.1", "91.189.91.157"}
    # the host-networked app (uid 10001) leaves through the output hook and is counted
    app = unplug_test.parse_drop_log("kernel: warroom-egress-drop host IN= OUT=eth0 SRC=10.0.0.2 DST=3.3.3.3 "
                                     "PROTO=TCP SPT=7 DPT=443 UID=10001 GID=10001")
    assert unplug_test.attribute_drops(app, {unplug_test.APP_UID})[0] == app
    # old table (no chain tag) stays conservative: counted
    old = unplug_test.parse_drop_log("kernel: warroom-egress-drop IN= OUT=eth0 SRC=10.0.0.2 DST=8.8.8.8 PROTO=UDP SPT=5 DPT=53")
    assert old[0]["chain"] == "box" and unplug_test.attribute_drops(old, set())[0] == old


def test_host_ns_uids_excludes_other_namespaces(tmp_path):
    (tmp_path / "self" / "ns").mkdir(parents=True)
    os.symlink("net:[1]", tmp_path / "self" / "ns" / "net")
    me, other = os.getpid(), os.getppid()
    for pid, ns in ((me, "net:[1]"), (other, "net:[2]")):
        (tmp_path / str(pid) / "ns").mkdir(parents=True)
        os.symlink(ns, tmp_path / str(pid) / "ns" / "net")
    assert unplug_test.host_ns_uids({me, other}, proc=tmp_path) == {os.geteuid()}
    assert unplug_test.host_ns_uids({me}, proc=tmp_path / "missing") == set()


def test_unplug_verdict_ignores_os_drops_and_dedupes_sources():
    base = {"block": {"applied": True}, "internet_reachable_during_test": False, "app": {}}
    os_only = {**base, "sockets": {"outbound_connections": 0, "outbound": []},
               "egress": {"firewall_dropped_packets": 6, "stack_drop_attempts": [],
                          "host_other_attempts": [{"dst": "91.189.91.157", "dport": 123}] * 3,
                          "conntrack_available": False, "conntrack_outbound_flows": []}}
    v = {x["name"]: x for x in unplug_test.verdicts(os_only, 0.5, False)}["outbound_connections"]
    assert v["passed"] is True and v["measured"] == 0 and v["extra"]["host_other_attempts"] == 3
    assert v["extra"]["firewall_dropped_packets_box_wide"] == 6
    # one container connect: 2 packets in the log, also seen as SYN_SENT by the socket watcher -> 1 attempt
    stack = {**base, "sockets": {"outbound_connections": 1,
                                 "outbound": [{"pid": 9, "ip": "1.1.1.1", "port": 443, "status": "SYN_SENT"}]},
             "egress": {"firewall_dropped_packets": 2, "host_other_attempts": [],
                        "stack_drop_attempts": [{"chain": "containers", "dst": "1.1.1.1", "dport": 443,
                                                 "packets": 2}],
                        "conntrack_available": True, "conntrack_outbound_flows": []}}
    v = {x["name"]: x for x in unplug_test.verdicts(stack, 0.5, False)}["outbound_connections"]
    assert v["passed"] is False and v["measured"] == 1
    # kernel log unreadable: container packet counter is the stack evidence, host counter is not
    ctr = {**base, "sockets": {"outbound_connections": 0},
           "egress": {"firewall_dropped_packets": 4, "host_dropped_packets": 4, "container_dropped_packets": 0,
                      "conntrack_available": False, "conntrack_outbound_flows": []}}
    v = {x["name"]: x for x in unplug_test.verdicts(ctr, 0.5, False)}["outbound_connections"]
    assert v["passed"] is True and v["measured"] == 0


def test_firewall_splits_host_and_container_drops():
    nft = (unplug_test.NFT).read_text()
    assert "counter egress_host" in nft and "counter egress_containers" in nft and "counter dropped_egress" in nft
    assert 'prefix "warroom-egress-drop host " level warn flags skuid' in nft
    assert 'prefix "warroom-egress-drop fwd "' in nft


def test_install_quiets_os_egress(tmp_path):
    import subprocess
    etc = tmp_path / "etc"
    (etc / "default").mkdir(parents=True)
    (etc / "default" / "motd-news").write_text("ENABLED=1\nURLS=x\n")
    root = tmp_path / "opt"
    env = os.environ | {"WARROOM_ROOT": str(root), "SYSTEMD_DIR": str(tmp_path), "NO_SYSTEMCTL": "1",
                        "NO_IMAGES": "1", "NO_MODELS": "1", "NO_BENCH_VENV": "1", "ETC_DIR": str(etc),
                        "LAN_NTP": "192.168.1.1", "WARROOM_SUPERVISOR_PIN": "test-pin-123"}
    sh = Path(unplug_test.ROOT) / "deploy" / "bin" / "install_spark.sh"
    r = subprocess.run(["bash", str(sh), "t1"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    conf = (etc / "systemd" / "timesyncd.conf.d" / "warroom.conf").read_text()
    assert "NTP=192.168.1.1" in conf and "FallbackNTP=\n" in conf
    assert (etc / "default" / "motd-news").read_text().startswith("ENABLED=0")


def test_egress_evidence_linux_reads_split_counters_and_drop_log(monkeypatch):
    monkeypatch.setattr(unplug_test.platform, "system", lambda: "Linux")
    counts = {"dropped_egress": 0, "egress_host": 0, "egress_containers": 0}
    calls = []

    def fake_run(cmd, timeout=10):
        calls.append(cmd)
        if cmd[:3] == ["nft", "reset", "counter"]:
            counts[cmd[-1]] = 0
            return True, ""
        if cmd[:3] == ["nft", "list", "counter"]:
            return True, f"counter {cmd[-1]} {{\n packets {counts[cmd[-1]]} bytes 0\n}}"
        if cmd[0] == "journalctl":
            return True, _DROP_LOG
        return False, "nope"

    monkeypatch.setattr(unplug_test, "_run", fake_run)
    with unplug_test.EgressEvidence(True) as eg:
        counts.update(dropped_egress=5, egress_host=3, egress_containers=2)   # 3 OS packets, 2 container
    r = eg.report(stack_pids=set())
    assert r["firewall_dropped_packets"] == 5 and r["host_dropped_packets"] == 3 and r["container_dropped_packets"] == 2
    assert [a["dst"] for a in r["stack_drop_attempts"]] == ["1.1.1.1"] and len(r["host_other_attempts"]) == 2
    assert unplug_test.APP_UID in r["stack_host_uids"]
    jc = next(c for c in calls if c[0] == "journalctl")
    assert jc[1] == "-k" and jc[jc.index("--since") + 1].startswith("@")
    v = {x["name"]: x for x in unplug_test.verdicts(
        {"block": {"applied": True}, "sockets": {"outbound_connections": 0, "outbound": []}, "egress": r,
         "internet_reachable_during_test": False, "app": {}}, 0.5, False)}["outbound_connections"]
    assert v["measured"] == 1 and v["passed"] is False and "1 stack attempt" in v["note"]


# ---------------------------------------------------------------- page-10 decode speed (推測解碼 95–108 tok/s)
def test_decode_tps_excludes_prefill_token_and_judges_low_edge():
    assert decode.decode_tps(101, 0.5, 1.5) == 100.0          # 100 decode tokens in 1 s after the first token
    assert decode.decode_tps(1, 0.5, 1.5) is None and decode.decode_tps(50, None, 1.0) is None
    assert decode.DECODE_TPS_BAND == (95.0, 108.0)
    assert decode.judge(101.0, 10, None).passed is True
    assert decode.judge(94.9, 10, "full").passed is False
    hi = decode.judge(120.0, 10, None)
    assert hi.passed is True and "under-states" in hi.note
    assert decode.judge(60.0, 10, "no_spec").passed is None   # ablation leg: info only
    assert decode.judge(None, 0, None).passed is None


def test_decode_runner_is_single_stream_full_plan(env_fake, tmp_path):
    url, state = env_fake
    n0 = len(state.get("bodies", []))
    out = decode.main(["--runs", "4", "--out-dir", str(tmp_path)])
    bodies = state["bodies"][n0:]
    assert len(bodies) == 5                                    # 1 prime + 4 judged, sequential
    assert all(b["response_format"]["json_schema"]["name"] == "CommanderPlan" for b in bodies)
    assert all(b["max_tokens"] == decode.COMMANDER_MAX_TOKENS for b in bodies[1:])   # never cut at the card
    assert {c["phase"] for c in out["calls"]} == {"plan", "replan"}
    assert out["batch"] == 1 and out["decode_tps"]["n"] >= 1
    v = out["verdicts"][0]
    assert v["name"] == "decode_tps_p50" and v["measured"] == out["decode_tps"]["p50"]
    assert json.loads(Path(out["saved_to"]).read_text())["bench"] == "decode"


def test_decode_ablation_reports_spec_contribution(tmp_path):
    def put(cfg, p50, tps):
        d = tmp_path / cfg
        d.mkdir()
        (d / "decode-x-20261003-000000.json").write_text(json.dumps({"decode_tps": {"p50": p50}, "n_counted": 10}))
        (d / "tokens_per_watt-x-20261003-000000.json").write_text(json.dumps({"plug": {"tokens_per_s": tps}}))
    put("full", 102.0, 240.0)
    put("no_spec", 60.0, 170.0)
    put("mtp", 90.0, 220.0)
    out = decode.main(["--ablation", str(tmp_path)])
    c = out["contributions"]
    assert c["spec_decode_dspark"] == {"with": "full", "without": "no_spec", "delta_tps": 42.0, "speedup_x": 1.7}
    assert c["spec_decode_mtp"]["delta_tps"] == 30.0
    assert out["configs"]["full"]["tokens_per_s_concurrent"] == 240.0
    assert out["verdicts"][0]["passed"] is True                # production (full) judged against ≥ 95


def test_ablation_runs_decode_and_tokens_per_watt_every_config():
    s = (Path(__file__).resolve().parents[1] / "bench" / "spark_ablation.sh").read_text()
    start = s.index('for c in $CONFIGS; do\n  read -r')
    end = s.index("\ndone\n", start)
    loop = s[start:end]
    assert '-m bench.decode --runs 10 --config "$c"' in loop
    assert "-m bench.tokens_per_watt" in loop
    assert '-m bench.decode --ablation "$OUT"' in s[end:]
    assert "bench.decode" in s.splitlines()[[i for i, l in enumerate(s.splitlines()) if "import bench.ttft" in l][0]]
