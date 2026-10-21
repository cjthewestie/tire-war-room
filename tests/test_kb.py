"""kb module: SOP corpus + hybrid retrieval, orders/shifts DB + Excel import, tool functions, quiz."""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime

import pytest
from pydantic import ValidationError

from warroom import factory as F
from warroom.contracts import QueryOrdersShiftsArgs, QueryOrdersShiftsData, SopHit, SopKB, TeamId, TireSize
from warroom.kb import OrdersDB, QdrantSopKB, load_quiz, parse_choice, score
from warroom.kb.store import DEFAULT_SOP_DIR, load_corpus, parse_sop
from warroom.kb.tools import query_orders_shifts_tool, sop_search_tool

REQUIRED_TITLES = ["硫化中斷處置", "硫化機溫度下降", "蒸汽壓不足", "冷卻水路異常", "開煉機過熱", "密煉機異常",
                   "感測器漂移判定", "預熱過熱報廢判定", "換模作業", "夜班人力調度", "停機與復機", "品質抽驗"]


class FixedClock:
    def __init__(self, t: datetime):
        self.t = t

    def now(self) -> datetime:
        return self.t


DEMO_NOW = datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ)


@pytest.fixture(scope="module")
def kb() -> QdrantSopKB:
    k = QdrantSopKB(path=":memory:", collection="sop_test")
    assert k.ingest() > 0
    return k


@pytest.fixture()
def db() -> OrdersDB:
    return OrdersDB(":memory:", clock=FixedClock(DEMO_NOW))


# ------------------------------------------------------------------ corpus
def test_corpus_has_required_sops():
    titles = {c.title for c in load_corpus(DEFAULT_SOP_DIR)}
    assert len(titles) >= 12
    for want in REQUIRED_TITLES:
        assert any(want in t for t in titles), want
    assert "硫化中斷處置" in titles   # exact title, page 11


def test_every_sop_has_id_steps_thresholds_and_owners():
    for p in sorted(DEFAULT_SOP_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        assert text.startswith("# SOP-"), p.name
        assert "權責" in text, p.name
        chunks = parse_sop(p)
        assert chunks and all(c.sop_id.startswith("SOP-") for c in chunks)
        assert len({c.sop_id for c in chunks}) == 1


def test_thresholds_match_proposal():
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in DEFAULT_SOP_DIR.glob("*.md"))
    assert "120～130°C" in corpus and "20 kgf/cm²" in corpus
    assert "≥120°C" in corpus and "≥110°C" in corpus
    assert "28.5" in corpus and "4.5" in corpus and "85 kJ/mol" in corpus
    assert "06:00–15:00" in corpus and "14:00–23:00" in corpus and "20:00–05:00" in corpus
    assert "T−1 時 30 分" in corpus and "8 小時" in corpus
    ids = [parse_sop(p)[0].sop_id for p in DEFAULT_SOP_DIR.glob("*.md")]
    assert len(ids) == len(set(ids)), "duplicate SOP ids"


# ------------------------------------------------------------------ retrieval
def test_protocol(kb):
    assert isinstance(kb, SopKB)
    assert kb.mode == "vector"


@pytest.mark.parametrize("query,expected", [
    ("溫度下降 蒸汽", {"SOP-CP-02", "SOP-ST-01"}),
    ("硫化中斷處置", {"SOP-CP-01"}),
    ("7號機溫度持續下降到119.5 洩壓", {"SOP-CP-01", "SOP-CP-02"}),
    ("蒸汽壓不足 同管線多台降溫", {"SOP-ST-01"}),
    ("大開煉機 膠溫 112 水流偏低", {"SOP-OM-01", "SOP-CW-01"}),
    ("進出水溫差小於 1 度 冷卻水", {"SOP-CW-01", "SOP-OM-01"}),
    ("感測器 漂移 對照同管線", {"SOP-SN-01"}),
    ("夜班缺人 加班 跨組調人", {"SOP-HR-01"}),
    ("換模 停機 8 小時", {"SOP-MC-01"}),
    ("預熱過熱 報廢", {"SOP-QC-02"}),
    ("密煉機 排膠溫度 轉子電流", {"SOP-MX-01"}),
    ("停機後 復機條件", {"SOP-SD-01"}),
    ("硬度 破壞抽驗", {"SOP-QC-01"}),
    ("週末不斷火 出模時刻", {"SOP-CP-03"}),
    ("貼合 投料 倒推", {"SOP-BD-01"}),
    ("風險分級 核准 白名單", {"SOP-EV-01"}),
])
def test_top1_hits_right_sop(kb, query, expected):
    hits = kb.search(query, k=3)
    assert hits and isinstance(hits[0], SopHit)
    assert hits[0].sop_id in expected, [(h.sop_id, h.score) for h in hits]


def test_hits_are_distinct_sops_with_citation(kb):
    hits = kb.search("硫化機 溫度", k=5)
    assert len(hits) == 5
    assert len({h.sop_id for h in hits}) == 5
    for h in hits:
        assert h.text.startswith(f"〔{h.sop_id} {h.title}｜")
        assert h.source.endswith(".md")
    assert [h.score for h in hits] == sorted([h.score for h in hits], reverse=True)


def test_empty_query(kb):
    assert kb.search("   ") == []


def test_lexical_fallback_without_model(tmp_path, monkeypatch):
    from warroom.kb import store

    def boom(*a, **k):
        raise RuntimeError("no model offline")
    monkeypatch.setattr(store, "_get_embedder", boom)
    k = QdrantSopKB(path=":memory:", collection="sop_nomodel")
    k.ingest()
    assert k.mode == "lexical"
    assert k.search("硫化中斷處置")[0].sop_id == "SOP-CP-01"
    assert k.search("溫度下降 蒸汽")[0].sop_id in {"SOP-CP-02", "SOP-ST-01"}


def test_embedded_path_index_reused(tmp_path):
    a = QdrantSopKB(path=tmp_path / "q", collection="sop")
    n = a.ingest()
    b = QdrantSopKB(path=tmp_path / "q", collection="sop")   # same process: shared client, reuse index
    assert b.search("硫化中斷處置")[0].sop_id == "SOP-CP-01"
    assert b.mode == "vector" and len(b._chunks) == n


def test_kb_does_not_import_llm_clients():
    for p in pathlib.Path(DEFAULT_SOP_DIR).parent.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        names = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not any(x and x.startswith(("openai", "warroom.agent")) for x in names), p


def test_sop_get(kb):
    assert "硫化中斷處置" in kb.get("SOP-CP-01")
    assert kb.get("SOP-NOPE-99") is None


# ------------------------------------------------------------------ orders DB
def test_order_progress_matches_page2(db):
    prog = {p.size: p for p in db.order_progress()}
    for size, (done, total, rem) in F.DEMO_ORDER_PROGRESS.items():
        assert (prog[size].done, prog[size].total, prog[size].remaining_h) == (done, total, rem)
    assert [prog[s].label for s in TireSize] == ["小胎", "中胎", "大胎"]


def test_page11_query_press7(db):
    data = db.query(QueryOrdersShiftsArgs(press_ids=[F.press_id(7)]))
    assert isinstance(data, QueryOrdersShiftsData)
    nxt = [j for j in data.next_jobs if j.order_id == "#A-311"]
    assert nxt and nxt[0].press_id == "CURE-07" and nxt[0].size is TireSize.S
    a311 = next(o for o in data.orders if o.id == "#A-311")
    assert (a311.due - DEMO_NOW.date()).days == 3          # 交期 3 天後
    assert data.gaps == ["10/03 夜班缺 1 人"]               # 夜班缺 1 人
    running = next(j for j in data.next_jobs if j.order_id == "#S-101")
    assert (DEMO_NOW - running.mold_in).total_seconds() / 3600 == pytest.approx(14.4)   # 14.4 h in
    assert running.cure_h == 24.0


def test_backprop_page6(db):
    jobs = {j.id: j for j in db.jobs()}
    for job_id, size in (("J-11-01", TireSize.M), ("J-10-07", TireSize.L), ("J-01-02", TireSize.S)):
        j = jobs[job_id]
        bp = F.BACKPROP[size]
        assert (j.mold_in - j.bond_start).total_seconds() / 60 == bp["bond_start_min"]
        assert (j.mold_in - j.a_feed_start).total_seconds() / 60 == bp["a_feed_min"]
        assert j.side_pieces == bp["side_pieces"] and j.bead_pieces == bp["bead_pieces"]
        assert (j.demold - j.mold_in).total_seconds() / 3600 == F.CURE_HOURS[size]


def test_presses_respect_size_rules(db):
    for j in db.jobs():
        assert j.press_id in F.CURE_PRESS_IDS
        if j.size is not TireSize.S:
            assert j.press_id in F.ML_PRESSES


def test_big_tire_07_page2(db):
    sf = db.semi_finished("大胎 #07")
    assert [p.status_text for p in sf.parts] == ["預熱中", "成形中", "包布中", "已完成"]
    assert sf.planned_mold_in_at.hour == 17 and sf.planned_mold_in_at.tzinfo is not None
    assert all(p.machine_id in F.MACHINE_IDS for p in sf.parts)


def test_shifts_follow_page6_premise(db):
    shifts = db.shifts(DEMO_NOW.replace(hour=0, minute=0), DEMO_NOW.replace(hour=23, minute=59))
    curing = sorted((s.start.hour, s.end.hour) for s in shifts if s.team is TeamId.CURING)
    assert curing == [(6, 15), (14, 23)]
    night = [s for s in shifts if s.team is TeamId.BODY and s.start.hour == 20 and s.start.day == 3]
    assert night and night[0].end.hour == 5 and night[0].headcount == 2 and night[0].required == 3
    assert all(s.start.tzinfo is not None for s in shifts)


def test_current_shift_plan_page4():
    d = OrdersDB(":memory:", shift_plan="current")
    curing = {(s.start.hour, s.end.hour) for s in d.shifts() if s.team is TeamId.CURING}
    assert curing == {(8, 17)}


def test_operation_records(db):
    recs = db.operation_records(machine_id="CURE-07")
    assert recs and recs[0]["kind"] == "入模"
    assert all(r["machine_id"] in F.MACHINE_IDS for r in db.operation_records(limit=100) if r["machine_id"])


def test_excel_roundtrip(db, tmp_path):
    xlsx = db.export_excel(tmp_path / "plant.xlsx")
    other = OrdersDB(tmp_path / "o.db", seed=False)
    counts = other.import_excel(xlsx)
    assert counts["訂單"] == len(db.orders()) and counts["班表"] == len(db.shifts())
    other.clock = FixedClock(DEMO_NOW)
    a = db.query(QueryOrdersShiftsArgs(press_ids=["CURE-07"])).model_dump(mode="json")
    b = other.query(QueryOrdersShiftsArgs(press_ids=["CURE-07"])).model_dump(mode="json")
    assert a == b


def test_excel_import_chinese_headers_and_naive_datetimes(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "訂單"
    ws.append(["訂單號", "尺寸", "數量", "已完成", "交期", "客戶", "模號", "優先"])
    ws.append(["#X-1", "L", 5, 1, datetime(2026, 10, 20), "某客戶", "L-1", 1])  # noqa: DTZ001 - Excel cells are naive
    ws2 = wb.create_sheet("班表")
    ws2.append(["班次編號", "組別", "名稱", "開始", "結束", "人數", "應到"])
    ws2.append(["s1", "body", "夜班", datetime(2026, 10, 3, 20), datetime(2026, 10, 4, 5), 1, 3])  # noqa: DTZ001
    wb.save(tmp_path / "in.xlsx")
    d = OrdersDB(":memory:", seed=False, xlsx=tmp_path / "in.xlsx")
    o = d.orders()[0]
    assert (o.id, o.size, o.qty, o.due.isoformat()) == ("#X-1", TireSize.L, 5, "2026-10-20")
    s = d.shifts()[0]
    assert s.start.utcoffset().total_seconds() == 8 * 3600
    assert d.gaps(d.shifts()) == ["10/03 夜班缺 2 人"]


def test_order_id_matching_tolerant(db):
    assert [o.id for o in db.orders(["A-311"])] == ["#A-311"]
    assert [o.id for o in db.orders(["#a-311"])] == ["#A-311"]


def test_seed_anchors_on_clock_date():
    d = OrdersDB(":memory:", clock=FixedClock(datetime(2026, 11, 16, 2, 0, tzinfo=F.TZ)))   # a Monday
    data = d.query(QueryOrdersShiftsArgs(order_ids=["#A-311"]))
    assert data.orders[0].due.isoformat() == "2026-11-19"
    assert data.gaps == ["11/16 夜班缺 1 人"]


# ------------------------------------------------------------------ tool functions
class Ctx:
    def __init__(self, kb=None, orders_db=None, clock=None):
        self.kb, self.orders_db, self.clock = kb, orders_db, clock


def test_sop_search_tool_json(kb):
    out = sop_search_tool(Ctx(kb=kb), {"query": "硫化中斷處置", "k": 2})
    assert out["hits"][0]["title"] == "硫化中斷處置" and len(out["hits"]) == 2
    assert set(out["hits"][0]) == {"sop_id", "title", "source", "score", "text"}
    import json
    json.dumps(out, ensure_ascii=False)


def test_query_tool_json(db):
    out = query_orders_shifts_tool(Ctx(orders_db=db), {"press_ids": ["CURE-07"]})
    QueryOrdersShiftsData.model_validate({k: v for k, v in out.items() if k != "gap_details"})
    assert out["gaps"] == ["10/03 夜班缺 1 人"]
    assert out["gap_details"][0]["team"] == "body" and out["gap_details"][0]["short"] == 1
    import json
    json.dumps(out, ensure_ascii=False)


def test_query_tool_rejects_bad_args(db):
    with pytest.raises(ValidationError):
        query_orders_shifts_tool(Ctx(orders_db=db), {"press_ids": "CURE-07", "bogus": 1})


# ------------------------------------------------------------------ quiz
def test_quiz_set():
    q = load_quiz()
    assert len(q) >= 30
    assert len({i["id"] for i in q}) == len(q)
    for i in q:
        assert set(i["choices"]) == {"A", "B", "C", "D"} and i["answer"] in i["choices"]
        assert i["question"] and i["explanation"] and i["source"]
    assert score({i["id"]: i["answer"] for i in q})["accuracy_pts"] == 100.0
    assert score({})["accuracy_pts"] == 0.0


def test_quiz_numbers_agree_with_factory():
    q = {i["question"]: i for i in load_quiz()}
    item = next(v for k, v in q.items() if "總硫化時間" in k)
    assert item["choices"][item["answer"]].startswith("28.5")
    item = next(v for k, v in q.items() if "每週可做幾顆大胎" in k)
    assert float(item["choices"][item["answer"]]) == F.CAPACITY_TARGETS[2].large


@pytest.mark.parametrize("text,want", [("C", "C"), ("答案：B", "B"), ("(D) 因為…", "D"), ("Ａ", "A"),
                                       ("<think>maybe A</think>答案是 C", "C"), ("不知道", None)])
def test_parse_choice(text, want):
    assert parse_choice(text) == want
