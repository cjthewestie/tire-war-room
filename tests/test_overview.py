"""Agent Nemo 總覽 (CJ, 2026-10-22: 「我希望這裡是獨立的對話agent 可以統合整理目前進行中的任務…他還可以列出我覺得有疑問的agent
並且列連結給我讓我可以直接連到那個agent的任務版」): the overview conversation has no selected task, the questionable
rules come from real state, and links are made by code from event ids that exist."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from tests.test_plan_talk import Plant, _rt
from warroom.agent.main_agent import MainAgent, topic_for
from warroom.api import overview, plant_query
from warroom.contracts import HypothesisStatus

T0 = datetime(2026, 10, 22, 8, 0, tzinfo=timezone(timedelta(hours=8)))


def _ev(eid, state="plan", minutes=1.0, **kw):
    base = {"id": eid, "opened_at": T0, "closed_at": None, "state": NS(value=state), "follow_up": None,
            "stop_reason_text": None, "outcome": None, "replans": 0, "hypotheses": [], "consecutive_failures": 0, "steps": [],
            "source": "detector", "timeline": [NS(ts=T0 + timedelta(minutes=minutes))], "command": None, "alert_card": None,
            "anomaly": NS(message="溫度偏低"), "machine_id": "CURE-07"}
    return NS(**{**base, **kw})


class _Board:
    def __init__(self, tasks):
        self.tasks = tasks

    def for_event(self, eid):
        return [t for t in self.tasks if t.event_id == eid]


def _runtime(events, now_min=5.0, pending=(), tasks=()):
    return NS(clock=NS(now=lambda: T0 + timedelta(minutes=now_min)), events=lambda: list(events),
              approvals=NS(pending=lambda: list(pending)), audit=None,
              orch=NS(_board=lambda: _Board(list(tasks))))


def test_questionable_rules_from_state():
    card = NS(event_id="EV-WAIT", version=1, created_at=T0, expires_at=T0 + timedelta(minutes=10))
    late = NS(event_id="EV-WORK", status="執行中", eta=T0 - timedelta(minutes=2), title="檢修 7 號機")
    evs = [
        _ev("EV-OK", minutes=4.5),                                                   # planning, fresh: fine
        _ev("EV-WAIT", state="await_approval", minutes=4.5),                         # 6 of 10 min: over half
        _ev("EV-STOP", state="safe_stop", closed_at=T0, stop_reason_text="10 分鐘內沒核准"),
        _ev("EV-STOP-DONE", state="safe_stop", closed_at=T0, follow_up=NS(status="closed")),
        _ev("EV-REPLAN", state="execute", minutes=4.5, replans=2),
        _ev("EV-H1", state="execute", minutes=4.5,
            hypotheses=[NS(id="H1", statement="蒸汽壓不足", status=HypothesisStatus.REJECTED)]),
        _ev("EV-VERIFY", state="verify", minutes=4.5, consecutive_failures=2),
        _ev("EV-STUCK", state="plan", minutes=-10),                                  # 15 min with no step
        _ev("EV-WORK", state="execute", minutes=4.5),
        _ev("EV-CLOSED", state="closed", closed_at=T0, replans=5),                   # closed: never listed
    ]
    q = {x["id"]: x["reasons"] for x in overview.questionable(_runtime(evs, now_min=6.0, pending=[card], tasks=[late]))}
    assert set(q) == {"EV-WAIT", "EV-STOP", "EV-REPLAN", "EV-H1", "EV-VERIFY", "EV-STUCK", "EV-WORK"}
    assert "超過時限一半" in q["EV-WAIT"][0]
    assert "安全停止" in q["EV-STOP"][0] and "10 分鐘內沒核准" in q["EV-STOP"][0]
    assert q["EV-REPLAN"] == ["重新規劃了 2 次"]
    assert "H1" in q["EV-H1"][0]
    assert "連續失敗 2 次" in q["EV-VERIFY"][0]
    assert "規劃卡住" in q["EV-STUCK"][0]
    assert "現場任務逾時" in q["EV-WORK"][0]


def test_links_only_from_real_ids():
    evs = [_ev("EV-1", state="execute", minutes=4.5, command="#A-311 可能晚交，請重排保交期", source="supervisor"),
           _ev("EV-2", minutes=4.5, machine_id="CURE-09")]
    r = _runtime(evs)
    # an id the model made up, one read result naming a missing event: neither becomes a link
    got = overview.links(r, "#A-311 之前怎麼了", "EV-FAKE-01 和 EV-1 都處理過；#A-311 已重排。",
                         [("order_lookup", {"events": [{"id": "EV-1"}, {"id": "EV-GONE"}]})])
    assert [x["event_id"] for x in got] == ["EV-1"]
    assert got[0]["label"].startswith("主管指令")
    # the reads named two tasks, the reply only one (9 號機): only that one
    got = overview.links(r, "現在所有任務狀況", "9 號機在擬計劃。", [("tasks", {"tasks": [{"id": "EV-1"}, {"id": "EV-2"}]})])
    assert [x["event_id"] for x in got] == ["EV-2"]
    # 有問題: the code's whole questionable list, each with its reason, whatever the reply says
    evs.append(_ev("EV-3", state="execute", minutes=4.5, replans=3))
    got = overview.links(_runtime(evs), "哪些任務有問題", "都還好。", [])
    assert [(x["event_id"], x["reason"]) for x in got] == [("EV-3", "重新規劃了 3 次")]


def test_questionable_topic():
    assert topic_for("哪些任務有問題") == "questionable"
    assert topic_for("現在所有任務狀況") == "tasks"
    assert "questionable" in plant_query.TOPICS


async def test_overview_mode_has_no_selected_task(tmp_path, monkeypatch):
    seen = []

    async def fake_turn(self, text, **kw):
        seen.append(kw)
        return {"id": f"MAIN-{len(seen):04d}", "reply": "好", "steps": [], "event_id": None, "by": "rules",
                "fell_back": False, "error": None, "intent": None, "ms": 1.0}

    monkeypatch.setattr(MainAgent, "turn", fake_turn)
    async with Plant(_rt(tmp_path, "normal")) as p:
        ev = p.rt.start_command("#A-311 可能晚交，請重排保交期", "supervisor")
        task = (await p.http.post("/api/main/message", json={"text": "這件怎樣", "event_id": ev.id, "conversation_id": "c1"})).json()
        over = (await p.http.post("/api/main/message", json={"text": "現在所有任務狀況", "event_id": ev.id,
                                                             "conversation_id": "c1", "mode": "overview"})).json()
        t, o = seen
        assert t["context"].get("selected_task", {}).get("id") == ev.id and t["mode"] == "task"
        assert "selected_task" not in o["context"] and o["waiting_plan"] is None and o["discuss"] is None
        assert o["mode"] == "overview" and "overview" in o["context"]
        assert {"tasks", "questionable"} <= set(o["context"]["overview"])
        # its own conversation memory: never the per-task one
        assert o["conversation"].startswith("overview|") and o["conversation"] != t["conversation"]
        assert over["mode"] == "overview" and isinstance(over["links"], list) and "links" not in task
        rows = [e.payload for e in p.rt.audit.tail(0, 1_000_000) if e.actor == "main_agent" and e.payload.get("orch") == "chat"]
        assert rows[-1]["mode"] == "overview" and rows[-1]["selected"] is None and "mode" not in rows[-2]


def test_links_name_the_task_a_control_ran_on():
    r = _runtime([_ev("EV-1", state="safe_stop", closed_at=T0, follow_up=NS(status="closed"))])
    assert [x["event_id"] for x in overview.links(r, "好", "主管中斷 10 號機", [], ["EV-1", "EV-NOPE"])] == ["EV-1"]
