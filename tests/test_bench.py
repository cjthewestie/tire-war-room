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

from bench import accuracy, concurrent, full_event, memory_check, report, tokens_per_watt, ttft, unplug_test
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
               "fd00::5", "::ffff:192.168.0.1", "224.0.0.251"):
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
    assert "ttft_warm_32k_p50" in names
    assert out["alert_card"]["valid_card_rate"] == 1.0
    saved = json.loads(Path(out["saved_to"]).read_text())
    assert saved["bench"] == "ttft" and saved["host"]["hostname"]


def test_concurrent_runner(env_fake, tmp_path):
    out = concurrent.main(["--runs", "1", "--with-plan", "--out-dir", str(tmp_path)])
    assert out["alert_each_s"]["n"] == F.CONCURRENT_ANOMALIES
    assert out["verdicts"][0]["passed"] is True


def test_full_event_runner(env_fake, tmp_path):
    out = full_event.main(["--runs", "1", "--via-state-machine", "--out-dir", str(tmp_path)])
    run = out["runs"][0]
    assert run["llm_ok"] and out["plan_valid_rate"] == 1.0
    assert run["rounds"]["compute"]["cure_formula"] == {"total_cure_h": 36.5, "extra_h": 8.5}
    assert "state_machine" in out                         # skipped while agent is a stub, or a real run


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
    assert v["commander_ttft"]["measured"] == 3.193 and v["commander_ttft"]["passed"] is False


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
    assert "| ttft | first_alert_card_p90 |" in text and "| memory | stack_gb |" in text
