"""One Agent Nemo session per task, and the task list behind it (issue #189).

CJ, 2026-10-22: 「agent nemo 主控台的是一個任務一個 agent context，不要共用」「進行中任務也要可以搜尋的到 今日已結案也要 然後
隔日已結案的任務會儲存到歷史任務區分每天」「總攬區的對話要可以存取這些事件點了會到主控台」."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from tests.test_plan_talk import Plant, _rt
from tests.test_tasklog import _one_task
from warroom.agent.audit import JsonlAuditLog
from warroom.api import overview, task_sessions
from warroom.api.tasklog import TaskLog
from warroom.contracts import AuditKind


def _main(p):
    return p.http._transport.app.state.holder["main"]


# ---------------------------------------------------------------- sessions do not leak
async def test_each_task_has_its_own_session(tmp_path):
    async with Plant(_rt(tmp_path, "normal")) as p:
        say = lambda text, **kw: p.http.post("/api/main/message", json={"text": text, "conversation_id": "page-1", **kw})  # noqa: E731
        assert (await say("記住：9 號機這週不要排", event_id="EV-A")).status_code == 200
        assert (await say("你好", event_id="EV-B")).status_code == 200
        assert (await say("嗨")).status_code == 200
        mems = _main(p)._memories
        a = next(k for k in mems if k.startswith("task:EV-A|"))
        b = next(k for k in mems if k.startswith("task:EV-B|"))
        general = next(k for k in mems if k.startswith("task|") and k.endswith("|page-1"))
        assert len({a, b, general}) == 3
        said = lambda k: " ".join(t for pair in mems[k] for t in pair)  # noqa: E731
        assert "9 號機" in said(a) and "9 號機" not in said(b) and "9 號機" not in said(general)
        # the same task from another page (the Agent page) is the same session
        assert (await p.http.post("/api/main/message", json={"text": "那就這樣", "event_id": "EV-A",
                                                              "conversation_id": "page-2"})).status_code == 200
        assert not [k for k in mems if k.startswith("task:EV-A|") and k != a]
        # the session's turns on disk, by task
        s = (await p.http.get("/api/tasks/EV-A/session")).json()
        texts = [t["text"] for t in s["turns"] if t["role"] == "user"]
        assert texts == ["記住：9 號機這週不要排", "那就這樣"]
        assert [t["text"] for t in (await p.http.get("/api/tasks/EV-B/session")).json()["turns"] if t["role"] == "user"] == ["你好"]


# ---------------------------------------------------------------- the task list and its search
async def test_search_finds_running_and_closed_today(tmp_path):
    async with Plant(_rt(tmp_path, "normal")) as p:
        done = await _one_task(p)                                       # #A-311, approved and closed
        r = await p.http.post("/api/command", json={"text": "SO-004 可能晚交，請評估重排"})
        open_id = r.json()["event_id"]
        await p.until(lambda: p.pending(open_id) or p.rt.state_machine.get(open_id).state.value != "detected")
        lst = (await p.http.get("/api/tasks")).json()
        running = {t["id"] for t in lst["running"]}
        today = {t["id"] for t in lst["closed_today"]}
        if p.rt.state_machine.get(open_id).state.value not in ("closed", "safe_stop"):
            assert open_id in running
        assert done in today and done not in running and not lst["history"]
        hit = (await p.http.get("/api/tasks", params={"q": "A-311"})).json()
        assert [t["id"] for t in hit["closed_today"]] == [done] and open_id not in {t["id"] for t in hit["running"]}
        so = (await p.http.get("/api/tasks", params={"q": "so-004"})).json()
        assert open_id in {t["id"] for t in [*so["running"], *so["closed_today"]]}
        assert done not in {t["id"] for t in [*so["running"], *so["closed_today"]]}
        kind = (await p.http.get("/api/tasks", params={"q": "主管指令"})).json()
        assert done in {t["id"] for t in kind["closed_today"]}
        none = (await p.http.get("/api/tasks", params={"q": "沒有這種任務"})).json()
        assert not none["running"] and not none["closed_today"] and not none["history"]


# ---------------------------------------------------------------- day rollover -> 歷史任務 by day
class _Clock:
    def __init__(self, t: datetime):
        self.t = t

    def now(self) -> datetime:
        return self.t


def _stub(tmp_path, clock, loop=None):
    return SimpleNamespace(clock=clock, events=lambda: [], orch=None, tasklog=TaskLog(tmp_path / "audit.jsonl"),
                           day_loop_info=lambda: {"loop": loop} if loop else None)


def _task(log, eid, machine="CURE-07", final="closed"):
    log.append(AuditKind.ANOMALY, "detector", {"message": f"{machine} 溫度下降", "machine_id": machine}, event_id=eid)
    log.append(AuditKind.STATE_TRANSITION, "state_machine", {"to": final}, event_id=eid)


def test_day_rollover_moves_closed_tasks_to_history_by_day(tmp_path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.append(AuditKind.SYSTEM, "supervisor", {"msg": "scenario loaded", "scenario": "day_loop"})
    _task(log, "EV-1")
    clock = _Clock(datetime.fromisoformat(log.tail(0, 10)[-1].ts if isinstance(log.tail(0, 10)[-1].ts, str)
                                          else log.tail(0, 10)[-1].ts.isoformat()))
    r = _stub(tmp_path, clock, loop=1)
    lst = task_sessions.listing(r)
    assert [t["id"] for t in lst["closed_today"]] == ["EV-1"] and not lst["history"]
    # 24:00 -> 00:00: 循環 2
    log.append(AuditKind.SYSTEM, "day_loop", {"msg": "新的一天（循環 2）", "loop": 2})
    _task(log, "EV-2", machine="CURE-08")
    lst = task_sessions.listing(r)
    assert [t["id"] for t in lst["closed_today"]] == ["EV-2"]
    assert [(g["label"].split("（")[1], [t["id"] for t in g["tasks"]]) for g in lst["history"]] == [("循環 1）", ["EV-1"])]
    # another day: two history groups, newest first; search reaches them
    log.append(AuditKind.SYSTEM, "day_loop", {"msg": "新的一天（循環 3）", "loop": 3})
    lst = task_sessions.listing(r)
    assert not lst["closed_today"]
    assert [[t["id"] for t in g["tasks"]] for g in lst["history"]] == [["EV-2"], ["EV-1"]]
    assert ["循環 2" in lst["history"][0]["label"], "循環 1" in lst["history"][1]["label"]] == [True, True]
    hit = task_sessions.listing(r, "8 號機")
    assert [[t["id"] for t in g["tasks"]] for g in hit["history"]] == [["EV-2"]]
    # a restart on the same file: the history is still there, nothing is 今日
    log.append(AuditKind.SYSTEM, "system", {"msg": "runtime start", "scenario": "day_loop"})
    r2 = _stub(tmp_path, clock, loop=1)
    lst = task_sessions.listing(r2)
    assert not lst["closed_today"] and {t["id"] for g in lst["history"] for t in g["tasks"]} == {"EV-1", "EV-2"}


# ---------------------------------------------------------------- the 總覽 reaches past tasks
def test_overview_links_a_past_task_from_task_search():
    r = SimpleNamespace(events=lambda: [], audit=None)
    found = {"history": [{"day": "10/21（循環 1）", "tasks": [{"id": "EV-20261021-031500-CURE-07", "what": "7 號機・溫度下降",
                                                           "state": "結案"}]}], "running": [], "closed_today": []}
    links = overview.links(r, "昨天 7 號機怎麼了", "昨天 7 號機溫度下降，已結案。", [("task_search", found)])
    assert links == [{"event_id": "EV-20261021-031500-CURE-07", "label": "7 號機・溫度下降", "state": "結案", "reason": None}]
    # an id that is neither live nor found is never linked
    assert overview.links(r, "x", "EV-NOPE", []) == []
