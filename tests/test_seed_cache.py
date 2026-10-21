"""Scenario switches reuse the seed schedule instead of re-solving it (the 8-10 s freeze on 展示 clicks)."""
from __future__ import annotations

from warroom.api.runtime import ComponentBuilder
from warroom.config import Settings
from warroom.contracts import ScenarioName
from warroom.schedule import demo


def _settings(tmp_path) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", llm_mode="mock")


def _count_solves(monkeypatch) -> list[int]:
    calls = [0]
    real = demo.seed_schedule

    def counted(*a, **kw):
        calls[0] += 1
        return real(*a, **kw)
    monkeypatch.setattr("warroom.schedule.seed_schedule", counted)
    return calls


def _jobs(comps):
    return [j.model_dump(mode="json") for j in comps.store.current().schedule.jobs]


def test_second_load_of_a_scenario_reuses_the_seed_schedule(tmp_path, monkeypatch):
    calls = _count_solves(monkeypatch)
    b = ComponentBuilder(_settings(tmp_path))
    first = b.build(ScenarioName.PRESS7_TEMP_DROP, fresh_store=True)
    assert calls[0] == 1
    again = b.build(ScenarioName.PRESS7_TEMP_DROP, fresh_store=True)
    assert calls[0] == 1
    assert _jobs(again) == _jobs(first)


def test_cache_survives_a_restart_and_differs_per_scenario(tmp_path, monkeypatch):
    calls = _count_solves(monkeypatch)
    s = _settings(tmp_path)
    ComponentBuilder(s).build(ScenarioName.PRESS7_TEMP_DROP, fresh_store=True)
    ComponentBuilder(s).build(ScenarioName.PRESS7_TEMP_DROP, fresh_store=True)
    assert calls[0] == 1
    assert len(list((s.schedule_dir / "seed_cache").glob("*.json"))) == 1
    ComponentBuilder(s).build(ScenarioName.BAD_DATA, fresh_store=True)
    assert len(list((s.schedule_dir / "seed_cache").glob("*.json"))) in (1, 2)   # same inputs may share a key


def test_unreadable_cache_file_solves_again(tmp_path, monkeypatch):
    calls = _count_solves(monkeypatch)
    s = _settings(tmp_path)
    ComponentBuilder(s).build(ScenarioName.PRESS7_TEMP_DROP, fresh_store=True)
    for f in (s.schedule_dir / "seed_cache").glob("*.json"):
        f.write_text("{broken", encoding="utf-8")
    ComponentBuilder(s).build(ScenarioName.PRESS7_TEMP_DROP, fresh_store=True)
    assert calls[0] == 2
