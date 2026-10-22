"""Web module checks: the TS factory mirror matches warroom/factory.py, the mock reproduces the
proposal's page-2 / page-11 numbers, and (when pnpm is available) the production build succeeds."""
import pathlib
import re
import shutil
import subprocess

import pytest

from warroom import contracts as C
from warroom import factory as F

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SRC = WEB / "src"
FACTORY_TS = (SRC / "factory.ts").read_text(encoding="utf-8")
SCRIPTS_TS = (SRC / "mock" / "scripts.ts").read_text(encoding="utf-8")
ENGINE_TS = (SRC / "mock" / "engine.ts").read_text(encoding="utf-8")


def test_machine_names_match_python():
    for m in F.MACHINES:
        if m.line is C.Line.CURE:
            continue
        pat = rf"id: '{re.escape(m.id)}', line: '{m.line.value}', name: '{m.name}', short: '{m.short_name}'"
        assert re.search(pat, FACTORY_TS), m.id
    assert "ML_PRESSES = ['CURE-08', 'CURE-09', 'CURE-10', 'CURE-11']" in FACTORY_TS
    assert list(F.ML_PRESSES) == ["CURE-08", "CURE-09", "CURE-10", "CURE-11"]
    for h in ("H1", "H2", "H3"):
        ids = F.presses_on_header(h)
        assert f"{h}: [{', '.join(repr(i) for i in ids)}]" in FACTORY_TS, h


def test_no_foreign_machine_ids_in_web():
    """Every machine-id-looking token in web/src must be one of the 28 ids from warroom.factory."""
    pat = re.compile(r"\b(?:[ABCD]-[A-Z]+|BOND-\d\d|CURE-\d\d)\b")
    ok = set(F.MACHINE_IDS)
    for f in SRC.rglob("*.ts*"):
        for tok in pat.findall(f.read_text(encoding="utf-8")):
            assert tok in ok, f"{f.name}: {tok}"


def test_shifts_capacity_orders_match_python():
    def shift_rows(name):
        block = FACTORY_TS.split(f"export const {name}")[1].split("= [", 1)[1].split("\n]")[0]
        return re.findall(r"team: '(\w+)', name: '(.+?)', start: (\d+), end: (\d+), headcount: (\d+)", block)

    for name, py in (("CURRENT_SHIFTS", F.CURRENT_SHIFTS), ("OPTIMISED_SHIFTS", F.OPTIMISED_SHIFTS)):
        ts = shift_rows(name)
        assert ts == [(s.team.value, s.name, str(s.start.hour), str(s.end.hour), str(s.headcount)) for s in py], name
    for row in F.CAPACITY_TARGETS:
        assert f"{{ label: '{row.label}', small: {row.small:g}, medium: {row.medium:g}, large: {row.large:g} }}" in FACTORY_TS
    for size, (done, total, hours) in F.DEMO_ORDER_PROGRESS.items():
        assert f"{size.value}: [{done}, {total}, {hours}]" in FACTORY_TS
    ml, small = F.LEAD_TIME_TARGET_WEEKS["ML_100_each"], F.LEAD_TIME_TARGET_WEEKS["small"]
    assert f"ml: [{ml[0]:g}, {ml[1]:.1f}]" in FACTORY_TS and f"small: [{small[0]:g}, {small[1]:.1f}]" in FACTORY_TS
    for k, v in F.CURE_HOURS.items():
        assert f"{k.value}: {v:g}" in FACTORY_TS
    assert f"COMPOUND_STOP_C = {F.COMPOUND_STOP_C:g}" in FACTORY_TS
    assert f"CURATIVE_STOP_C = {F.CURATIVE_STOP_C:g}" in FACTORY_TS


def test_mock_page11_numbers():
    for s in ("28.5 h（+4.5 h）", "#A-311 仍排 7 號機", "119.5°C → H1 不成立", "夜班加班", "從 2 線調 1 人",
              "硫化中斷處置", "對照 6、8 號同管線", "夜班缺 1"):
        assert s in SCRIPTS_TS, s
    for t in ("2026-10-03T02:14:00+08:00",):
        assert t in ENGINE_TS
    # timeline offsets: 02:14:01, :03, :09, :11, :25, 02:16:40, 02:31:00, :04, :30
    # :03 / :09 / :11 / :25 / :26 live in the normal-run K table (#194 adds a compressed single-event table beside it)
    assert "{ hyp: 3, plan: 9, sim: 11, gate: 25, card: 26 }" in SCRIPTS_TS
    for off in ("at(1)", "at(160)", "at(1020)", "at(1024)", "at(1050)"):
        assert off in SCRIPTS_TS, off


def test_mock_order_panel_follows_the_dataset():
    """The mock's 訂單進度 shows the dataset's de-molded tyres at 02:12 (S 21 / M 9 / L 7), same as Factory."""
    assert "{ S: 21, M: 9, L: 7 }" in ENGINE_TS


def test_mock_page2_text():
    for s in ("異常：A 線大開煉機　溫度過高", "未加硫上限 120°C；已加硫另設 110°C 防焦燒", "原因：冷卻能力不足",
              "通知維修冷卻水路", "放慢生料投入", "無，大胎 #07 仍 17:00 入硫化機"):
        assert s in SCRIPTS_TS, s


def test_ts_contract_imports_only_from_contracts():
    """Views must use the shared contract types, never redefine API shapes."""
    for f in (SRC / "components").glob("*.tsx"):
        txt = f.read_text(encoding="utf-8")
        assert not re.search(r"^export (interface|type) (DashboardSnapshot|Event|ApprovalRequest)\b", txt, re.M), f.name


@pytest.mark.skipif(shutil.which("pnpm") is None or not (WEB / "node_modules").exists(), reason="pnpm/node_modules not available")
@pytest.mark.timeout(180)
def test_pnpm_build():
    r = subprocess.run(["pnpm", "build"], cwd=WEB, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert all((WEB / "dist" / p).exists() for p in ("index.html", "phone.html", "factory.html", "worker.html", "handover.html", "report.html"))
