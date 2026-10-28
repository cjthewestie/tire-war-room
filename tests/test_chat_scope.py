"""Issue #195, live runs 10-22: a task session's chat sees only its own task, and 「怎改」 is answered from its card."""
from warroom.agent.main_agent import _THIRD_RX, edit_q, speak_to_you
from warroom.api.task_sessions import TASK_LIST_TOPICS, scope_context, session_label

RUSH = "EV-20261021-000356-RUSH"
STAFF = "EV-20261021-000425-STAFF"


def _ctx() -> dict:
    return {"factory_time": "2026-10-21T08:00:00+08:00", "abnormal": [],
            "open_events": [{"id": RUSH, "state": "await_approval"}, {"id": STAFF, "state": "await_approval"}],
            "today": {"presses": {"運轉": 5}}, "overview": {"tasks": []}}


def test_task_session_context_has_only_its_task() -> None:
    out = scope_context(_ctx(), RUSH)
    assert [e["id"] for e in out["open_events"]] == [RUSH]
    assert STAFF not in str(out)
    assert "today" not in out and "overview" not in out           # the plant overview is a read away, not read back


def test_task_session_refuses_task_list_topics() -> None:
    assert {"open_events", "task_search", "questionable"} <= TASK_LIST_TOPICS
    assert "machines" not in TASK_LIST_TOPICS


def test_how_to_change_comes_from_the_card() -> None:
    one = "急單 #R-101 插入（v1）。要做的事：排程 v2 生效（#R-101 排入 8 號 ×2）。方案：方案 A：核准全部（只有這一個）"
    q = edit_q(one)
    assert "直接跟我說" in q and "「8 號機今晚不能用」" in q and "B 方案" not in q
    two = ("貼合組缺 1 人（v1）。要做的事：通知。人力方案（2 選 1，不是加在一起）："
           "方案 A：夜班加班 1 人（系統建議）；方案 B：B 線調 1 人")
    q2 = edit_q(two)
    assert "二選一" in q2 and "A 是夜班加班 1 人" in q2 and "B 是B 線調 1 人" in q2 and "A 加上 B" not in q2


def test_reply_speaks_to_you() -> None:
    assert speak_to_you("主管好，這個排程是 S3 排程 v2 生效。") == "這個排程是 排程 v2 生效。"
    assert "您" not in speak_to_you("請您核准。")
    assert speak_to_you("今天的工廠概況：5 台運轉。8 號機今晚可以不用。有什麼我能幫忙的嗎？") == "8 號機今晚可以不用。"
    for leak in ("主管是想調整 7 號機。", "可以問主管要改哪一項。", "而 waiting_plan 裡是胎體組的方案。"):
        assert _THIRD_RX.search(leak)
    assert not _THIRD_RX.search("7 號機現在 128°C。")


def test_session_label() -> None:
    assert session_label("2026-10-22", 1) == {"session": "Nemo-1022-#01", "session_short": "Nemo#01 · 10/22"}
    assert session_label("2026-10-22", 0)["session"] == "Nemo-1022-#00"
    assert session_label(None, 3)["session"] is None
