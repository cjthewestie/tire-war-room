"""Team dataset V2 (data/team_dataset): id map, files, Arrhenius cross-check, replay, SOP chunks,
past incidents. CJ, 2026-10-20: 「這份資料是隊友做的，當作團隊正式資料集」."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from warroom import factory as F
from warroom.config import Settings
from warroom.contracts import AuditKind, CureFormulaArgs, SafetyActionKind, ScenarioName, TireSize
from warroom.data import team_dataset as TD
from warroom.data.team_ids import DATASET_MACHINE_ID, MACHINE_ID, UNMAPPED, machine_id
from warroom.sim import Factory, ReplaySource
from warroom.sim.physics import STEAM_NOMINAL_KGF_CM2, steam_for_temp
from warroom.sim.team_replay import REPLAY_MODES, TeamReplayFactory, replay_start
from warroom.tools.cure import arrhenius_cure, rate_ratio

SIZE = {"小胎": TireSize.S, "中胎": TireSize.M, "大胎": TireSize.L}


# ------------------------------------------------------------------ id map
def test_id_map_covers_all_28_machines_one_to_one():
    assert len(MACHINE_ID) == 28 and not UNMAPPED
    assert len(set(MACHINE_ID.values())) == 28
    for team, ours in MACHINE_ID.items():
        assert DATASET_MACHINE_ID[ours] == team


@pytest.mark.parametrize("label, want", [
    ("A03", "A-BIGOPEN"), ("A04", "A-SMALLOPEN"), ("A06", "A-COOL"), ("V06", "CURE-06"),
    ("V07", "CURE-07"), ("V08", "CURE-08"), ("V07.T", "CURE-07"), ("A03 大開煉機", "A-BIGOPEN"),
    ("L01 油壓機", "BOND-01"), ("輸送帶", None), ("人工", None), ("X99", None),
])
def test_machine_id(label, want):
    assert machine_id(label) == want


def test_every_dataset_equipment_label_maps_to_the_same_process():
    """tire_step names each machine with its process; it must be the process our factory.py gives it."""
    seen = {}
    for r in TD.load_table("tire_step"):
        if r["equipment"].endswith("模具"):           # "V07 模具" = the press's mold (入模/出模), not a machine
            continue
        mid = machine_id(r["equipment"])
        if mid is None:
            assert r["equipment"] in ("輸送帶", "人工", "靜置"), r["equipment"]
            continue
        seen[mid] = r["step"].replace("（", "").replace("）", "")
    assert set(seen) == set(F.MACHINE_IDS)
    for mid, step in seen.items():
        assert F.MACHINE_BY_ID[mid].process in step, (mid, step)   # 預熱保溫儲料 ⊇ 保溫儲料


# ------------------------------------------------------------------ files
def test_dataset_files_present_and_validated():
    d = TD.DATASET_DIR
    for name in ("orders", "staff", "roster", "tire_unit", "tire_step", "events", "ev1_affected", "audit_cure",
                 "history_daily", "history_incident", "mold_change", "sensor_meta"):
        assert TD.load_table(name), name
    lines = [ln for ln in (d / "validation.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 25 and all(ln.startswith("PASS") for ln in lines)
    assert (d / "README.md").is_file() and (d / "SOURCE.txt").is_file()
    assert {r["sensor_id"].split(".")[0] for r in TD.load_table("sensor_meta")} == {"A03", "A04", "A06", "V06",
                                                                                     "V07", "V08"}


def test_sop_chunks_60_from_12_sops():
    chunks = TD.load_sop_chunks()
    assert len(chunks) == 60 and len({c.chunk_id for c in chunks}) == 60
    assert len({c.sop_id for c in chunks}) == 12


# ------------------------------------------------------------------ Arrhenius cross-check
def test_audit_cure_matches_our_arrhenius_formula():
    """audit_cure.csv (dataset) vs warroom/tools/cure.py: same Ea 85 kJ/mol, Tref 128 °C."""
    rows = TD.load_table("audit_cure")
    assert len(rows) == 2
    for r in rows:
        t, elapsed, cure = float(r["at_temp_c"]), float(r["elapsed_min"]), float(r["cure_min"])
        assert 1.0 / rate_ratio(t, 128.0, 85.0) == pytest.approx(float(r["factor"]), abs=5e-5)
        unit = next(u for u in TD.load_table("tire_unit") if u["unit_id"] == r["unit_id"])
        data = arrhenius_cure(CureFormulaArgs(size=SIZE[unit["size"]], actual_temp_c=t, elapsed_h=elapsed / 60.0,
                                              ref_cure_h=cure / 60.0))
        assert data.total_cure_h * 60.0 == pytest.approx(float(r["total_equiv_min"]), abs=1.0)
        assert data.extra_h * 60.0 == pytest.approx(float(r["extra_min"]), abs=1.0)
    assert 1.0 / rate_ratio(122.0) == pytest.approx(1.4725, abs=5e-5)          # validation.txt check


# ------------------------------------------------------------------ replay data
def test_steam_scale_maps_dataset_steady_states():
    assert TD.steam_from_bar_abs(2.5) == pytest.approx(STEAM_NOMINAL_KGF_CM2, abs=1e-3)
    assert TD.steam_from_bar_abs(1.8) == pytest.approx(steam_for_temp(119.5), abs=1e-3)


def test_ev2_window_is_the_page11_curve_on_our_ids():
    ticks = TD.replay_window("ev2")
    assert len(ticks) == 49 * 60 + 1                           # 02:11:00 .. 03:00:00 at 1 Hz
    assert ticks[0][0].ts == TD.WINDOWS["ev2"].start
    assert {r.machine_id for r in ticks[0]} == {"CURE-06", "CURE-07", "CURE-08"}

    def temp(mid, hms):
        ts = TD.WINDOWS["ev2"].start.replace(hour=int(hms[:2]), minute=int(hms[3:5]), second=int(hms[6:]))
        return next(r.temp_c for t in ticks if t[0].ts == ts for r in t if r.machine_id == mid)
    assert temp("CURE-07", "02:12:00") == pytest.approx(128.0, abs=1.0)
    assert temp("CURE-07", "02:31:00") == pytest.approx(119.5, abs=0.8)
    assert temp("CURE-06", "02:31:00") == pytest.approx(128.0, abs=1.0)
    assert temp("CURE-08", "02:31:00") == pytest.approx(128.0, abs=1.0)


def test_replay_jsonl_round_trips_through_replay_source(tmp_path):
    start = datetime(2026, 10, 3, 14, 9, tzinfo=F.TZ)
    n = TD.write_replay_jsonl("ev1", tmp_path / "ev1.jsonl", start_at=start)
    ticks = list(ReplaySource(tmp_path / "ev1.jsonl"))
    assert sum(len(t) for t in ticks) == n
    assert ticks[0][0].ts == start                              # time-shifted onto the sim clock
    assert {r.machine_id for r in ticks[0]} == {"A-BIGOPEN", "A-SMALLOPEN", "A-COOL"}
    peak = max(r.temp_c for t in ticks for r in t if r.machine_id == "A-BIGOPEN")
    assert 112.0 < peak < 120.0                                 # dataset: 112 °C at 14:26, never 120


# ------------------------------------------------------------------ replay through the plant
def test_replay_start_keeps_page11_date_and_window_time_of_day():
    assert replay_start("team_ev2") == datetime(2026, 10, 3, 2, 11, tzinfo=F.TZ)
    assert replay_start("team_ev1") == datetime(2026, 10, 3, 14, 9, tzinfo=F.TZ)
    assert replay_start("team_both") == datetime(2026, 10, 3, 2, 11, tzinfo=F.TZ)
    with pytest.raises(ValueError):
        TeamReplayFactory("nope")


def test_overlay_is_off_outside_normal(tmp_path):
    a = TeamReplayFactory("team_ev2", cache_dir=tmp_path)
    a.load_scenario(ScenarioName.PRESS7_TEMP_DROP)
    b = Factory()
    b.load_scenario(ScenarioName.PRESS7_TEMP_DROP)
    assert not a.replay_active
    for _ in range(120):
        assert a.step(1.0) == b.step(1.0)


def _run_plant(mode: str, seconds: int, tmp_path):
    from warroom.detect import StreamDetector
    from warroom.safety import RuleSafetyCore
    f = TeamReplayFactory(mode, cache_dir=tmp_path)
    f.load_scenario(ScenarioName.NORMAL)
    det, saf = StreamDetector(clock=f.clock), RuleSafetyCore(clock=f.clock)
    anomalies, actions = [], []
    for _ in range(seconds):
        rs = f.step(1.0)
        for a in saf.check(rs, f.states()):
            f.apply_safety_action(a)
            actions.append(a)
        anomalies += det.ingest(rs)
    return f, anomalies, actions


def test_ev2_replay_opens_cure07_event_and_safety_holds(tmp_path):
    f, anomalies, actions = _run_plant("team_ev2", 20 * 60, tmp_path)
    t0 = replay_start("team_ev2")
    cure7 = [a for a in anomalies if a.machine_id == "CURE-07"]
    assert cure7 and cure7[0].kind.value == "temp_drop"
    assert cure7[0].ts < t0 + timedelta(minutes=6)              # dataset fault 02:14 = t0 + 3 min
    assert not [a for a in anomalies if a.machine_id in ("CURE-06", "CURE-08")]
    holds = [a for a in actions if a.machine_id == "CURE-07" and a.action is SafetyActionKind.CURE_HOLD]
    assert holds                                                 # 119.x °C < 120 °C, no AI involved
    assert f.history("CURE-07", 30)[0].ts < t0                   # history comes from the dataset too


def test_ev1_replay_opens_open_mill_event_without_a_stop(tmp_path):
    _, anomalies, actions = _run_plant("team_ev1", 15 * 60, tmp_path)
    big = [a for a in anomalies if a.machine_id == "A-BIGOPEN"]
    assert big and big[0].kind.value == "overheat"
    assert not [a for a in actions if a.machine_id == "A-BIGOPEN"]   # < 120 °C unvulcanised: no stop


async def test_runtime_mock_replay_opens_both_events(tmp_path):
    """End to end, mock commander: detector -> state machine -> audit for EV-1 and EV-2."""
    from warroom.api import Runtime
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                 qdrant_path=tmp_path / "qdrant", scenario="normal", llm_mode="mock", replay="team_both")
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    assert rt.degraded == {}
    await rt.start()
    try:
        assert rt.now() == replay_start("team_both")
        for _ in range(8 * 60):
            await rt.tick(1.0, publish=False)
            for req in rt.approvals.pending():
                from warroom.contracts import ApprovalDecision, DecisionKind
                opt = next((o for o in req.options if o.recommended), req.options[0] if req.options else None)
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                     option_id=opt.id if opt else None, reason="test",
                                                     via="auto", decided_at=rt.now()))
        await rt.wait_events(5)
        machines = {ev.machine_id for ev in rt.events()}
        assert {"CURE-07", "A-BIGOPEN"} <= machines
    finally:
        await rt.stop()
    lines = [json.loads(ln) for ln in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    opened = {ln["payload"]["machine_id"] for ln in lines if ln["kind"] == AuditKind.ANOMALY.value}
    assert {"CURE-07", "A-BIGOPEN"} <= opened


# ------------------------------------------------------------------ SOP chunks in the knowledge base
@pytest.fixture(scope="module")
def kb():
    from warroom.kb import QdrantSopKB
    k = QdrantSopKB(path=":memory:", collection="sop_team_test")
    k.ingest()
    return k


def test_team_chunks_indexed_alongside_house_sops(kb):
    assert len(kb._team_chunks) == 60 and kb.team_mode in ("vector", "lexical")
    assert len(kb.titles()) == 16                                # house SOPs (15 + SOP-MT-01 原料短缺)
    assert len(kb.team_titles()) == 12
    assert kb.search("硫化中斷處置")[0].sop_id == "SOP-CP-01"    # page 11 unchanged


def test_sop_search_returns_team_dataset_chunks(kb):
    from warroom.kb.tools import sop_search_tool

    class Ctx:
        pass
    ctx = Ctx()
    ctx.kb = kb
    out = sop_search_tool(ctx, {"query": "疏水閥 卡滯 蒸汽壓力", "k": 3})
    # hits = house and team SOPs merged on one score; team_hits = the team ones among them
    assert {h["source_label"] for h in out["hits"]} == {"工廠 SOP", "團隊資料集"}
    assert out["team_hits"] and all(h["source"] == "team_dataset" for h in out["team_hits"])
    assert all(h in out["hits"] for h in out["team_hits"])
    assert out["team_hits"][0]["sop_id"] == "SOP-06"            # 蒸汽壓力異常與疏水閥檢查
    assert "SOP-06-C" in out["team_hits"][0]["text"]            # chunk id cited
    assert "蒸汽壓力異常與疏水閥檢查" in kb.get("SOP-06")


def test_team_chunks_off_and_lexical(monkeypatch, tmp_path):
    from warroom.kb import QdrantSopKB, store
    off = QdrantSopKB(path=":memory:", collection="sop_team_off", team=False)
    off._ensure_ready = lambda: None
    off._ready, off.mode = True, "lexical"
    assert off.search_team("蒸汽") == [] and off.team_mode == "off"

    def boom(*a, **k):
        raise RuntimeError("no model offline")
    monkeypatch.setattr(store, "_get_embedder", boom)
    lex = QdrantSopKB(path=":memory:", collection="sop_team_lex")
    lex.ingest()
    assert lex.team_mode == "lexical"
    assert lex.search_team("等效硫化 119.5")[0].sop_id == "SOP-05"


# ------------------------------------------------------------------ past incidents (read-only)
def test_incident_store_loads_8_and_maps_ids():
    store = TD.IncidentStore()
    assert len(store) == 8
    assert all(i.machine_id in F.MACHINE_IDS for i in store.all())
    assert [i.incident_id for i in store.for_machine("CURE-09")] == ["INC-004"]
    assert store.get("INC-001").machine_id == "A-BIGOPEN"
    assert {i.incident_id for i in store.search("疏水閥")} == {"INC-007"}
    assert not any(n.startswith(("add", "set", "write", "update", "delete")) for n in dir(store))


def test_replay_modes_listed():
    assert set(REPLAY_MODES) == {"team_ev1", "team_ev2", "team_both"}
