"""Resolution paths for the two dead ends of the 主控台 (CJ, 2026-10-22: 「安全核心要設計可以被解決 安全停止也要有可以走向解決的按鈕」).

(A) Safety Core → resolvable. Every stop / hold / alarm the core applies opens a SafetyCase for the machine:
  ① 派現場檢查    worker tasks from the rule's SOP (fixed per rule, below), on the world's task board (/worker).
  ② 讀值回到安全  code counts valid samples in a row that no rule of the case would trip on, the same count the
                  core needs to re-arm (RuleSafetyCore.rearm_samples). It only READS the readings and the core's rule
                  list; the core is never called, reset or changed here.
  ③ 確認恢復運轉  the supervisor's release: allowed only when ② passed AND ① is done (or 「已現場確認」 with a reason).
                  SR-CURE-LOW-120 releases by QC decision instead: 「QC 合格，放行」 or 「報廢」. Both judge the tyre, so
                  they need ① only, not ② (CJ 10-22, option B). 報廢: the tyre leaves the press. QC 合格 with ② passed:
                  as 確認恢復運轉. QC 合格 with the press still out of range: the tyre is de-molded as made (counts for
                  its order) and the press goes OUT_OF_SERVICE 「QC 放行，機台待維修」 (no cure job left, so the hold
                  does not fire again; back via 恢復運轉 after 檢修完成). Otherwise lifts STOPPED / ALARM back to
                  RUNNING, audited (AuditKind.SAFETY_RELEASE, actor = the supervisor session, with the branch taken).

(B) SAFE_STOP → follow-up: replan (a new event, warroom/agent/state_machine.py open_replan), hand to the floor (worker
    tasks, 「人工處理中」) and close (「確認結案」). Driven by Runtime; the fixed SOP tasks for B are here too.

HUMAN ONLY. These writes are reachable from the supervisor-session API routes in warroom/api/app.py and nowhere else:
nothing in warroom.agent or warroom.tools imports this module, it is not a tool, and neither main_agent nor the commander
can reach it (tests/test_resolve.py checks). The model never lifts a Safety Core stop.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from warroom.contracts import (
    AuditKind,
    MachineStatus,
    SafetyAction,
    SafetyActionKind,
    SafetyCase,
    SafetyRelease,
    SensorReading,
    TeamId,
)
from warroom.agent.narrate import machine_label
from warroom.factory import MACHINE_BY_ID, MachineKind
from warroom.sim.work import _spec

PHYSICAL_TEMP_RANGE = (-20.0, 300.0)     # same plausibility window as the core (warroom/safety/core.py)
KEEP_RESOLVED = 5


def _name(mid: str) -> str:
    return machine_label(mid)


def sop_for_rules(rule_ids: list[str], machine_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    """(SOP ids, task specs) for ① 派現場檢查. Fixed per rule, from warroom/kb/sop: the model never writes a task."""
    n = _name(machine_id)
    rules = set(rule_ids)
    sops: list[str] = []
    specs: list[dict[str, Any]] = []
    if rules & {"SR-COMPOUND-120", "SR-CURATIVE-110"}:
        sops += ["SOP-SD-01", "SOP-OM-01", "SOP-QC-02"]
        specs.append(_spec("檢修", f"{n}：檢查冷卻水閥與水路，排除過熱原因（SOP-OM-01）", TeamId.MAINTENANCE, machine_id))
        specs.append(_spec("品管", f"{n}：扣留停機批次送品管判定（SOP-QC-02）", TeamId.QC, machine_id))
    if "SR-CURE-LOW-120" in rules:
        sops += ["SOP-CP-01", "SOP-SD-01"]
        specs.append(_spec("檢修", f"{n}：檢查蒸汽閥、加熱板、疏水閥（SOP-CP-01）", TeamId.MAINTENANCE, machine_id))
        specs.append(_spec("品管", f"{n}：在模胎硫化度判定，放行或報廢（SOP-CP-01）", TeamId.QC, machine_id))
    if "SR-CURE-HIGH-130" in rules:
        sops += ["SOP-CP-02"]
        specs.append(_spec("檢修", f"{n}：檢查蒸汽壓調節閥，溫度回到 120–130°C（SOP-CP-02）", TeamId.MAINTENANCE,
                           machine_id))
    if "SR-DATA-LOSS" in rules:
        sops += ["SOP-SN-01"]
        specs.append(_spec("檢修", f"{n}：檢查溫度計與訊號線（SOP-SN-01）", TeamId.MAINTENANCE, machine_id))
    return list(dict.fromkeys(sops)), specs


def sop_for_stop(machine_id: str, title: str) -> tuple[list[str], list[dict[str, Any]]]:
    """(SOP ids, task specs) for a SAFE_STOP handed to the floor (「交給現場處理」)."""
    d = MACHINE_BY_ID.get(machine_id)
    if d is None:                           # a plant event or a supervisor command about the whole plant
        return ["SOP-EV-01"], [_spec("宣達", f"現場主管依 SOP-EV-01 人工處理：{title[:40]}", TeamId.SUPERVISOR, None,
                                     zone="staff")]
    n = machine_label(machine_id)
    if d.kind is MachineKind.CURING_PRESS:
        return ["SOP-CP-01"], [
            _spec("檢修", f"{n}：檢查蒸汽閥、加熱板、疏水閥（SOP-CP-01）", TeamId.MAINTENANCE, machine_id),
            _spec("品管", f"{n}：在模胎標示待判，交品管（SOP-CP-01）", TeamId.QC, machine_id)]
    if d.handles_compound:
        return ["SOP-OM-01"], [
            _spec("檢修", f"{n}：檢查冷卻水閥與水路（SOP-OM-01）", TeamId.MAINTENANCE, machine_id)]
    return ["SOP-SD-01"], [_spec("檢修", f"{n}：依 SOP-SD-01 現場檢查並排除原因", TeamId.MAINTENANCE, machine_id)]


class SafetyCases:
    """The open and recently resolved SafetyCases of one run (rebuilt with the components on a scenario load)."""

    def __init__(self, core: Any, audit: Any):
        self.core = core
        self.audit = audit
        self.safe_needed = int(getattr(core, "rearm_samples", 30) or 30)
        self._rules = {r.id: r for r in (getattr(core, "rules", None) or [])}
        self._cases: dict[str, SafetyCase] = {}

    # ---- red path hooks (never raise into the plant loop: the runtime wraps them)
    def on_action(self, a: SafetyAction, event_id: str | None) -> SafetyCase:
        c = self.open_for(a.machine_id)
        if c is not None:
            if a.id not in c.actions:
                c.actions.append(a.id)
                if a.rule_id not in c.rule_ids:
                    c.rule_ids.append(a.rule_id)
                c.decisions = self._decisions(c)
                c.safe_samples, c.reading_ok = 0, False
            return c
        c = SafetyCase(id=a.id, machine_id=a.machine_id, rule_id=a.rule_id, rule_ids=[a.rule_id], actions=[a.id],
                       action=a.action, message=a.message, value=a.value, threshold=a.threshold, ts=a.ts,
                       latency_ms=a.latency_ms, event_id=event_id, safe_needed=self.safe_needed)
        c.sop_ids = sop_for_rules(c.rule_ids, c.machine_id)[0]
        c.decisions = self._decisions(c)
        self._cases[c.id] = c
        return c

    def observe(self, readings: list[SensorReading]) -> bool:
        """Count valid in-range samples in a row per open case (skip invalid ones, like the core: no count, no reset).
        True when any case's ② flipped (the runtime republishes the snapshot)."""
        flipped = False
        open_ = {c.machine_id: c for c in self._cases.values() if c.status == "open"}
        if not open_:
            return False
        for r in readings:
            c = open_.get(r.machine_id)
            if c is None:
                continue
            v = r.temp_c
            if v is None or not math.isfinite(v) or not (PHYSICAL_TEMP_RANGE[0] <= v <= PHYSICAL_TEMP_RANGE[1]):
                continue
            c.reading = round(v, 2)
            if self._violates(c, v):
                c.safe_samples = 0
            else:
                c.safe_samples = min(c.safe_samples + 1, c.safe_needed)
            ok = c.safe_samples >= c.safe_needed
            if ok != c.reading_ok:
                c.reading_ok, flipped = ok, True
        return flipped

    def _violates(self, c: SafetyCase, v: float) -> bool:
        for rid in c.rule_ids:
            rule = self._rules.get(rid)
            if rule is None:                    # SR-DATA-LOSS: the stopping rule near whose limit the signal was lost
                rule = next((ru for ru in self._rules.values() if c.machine_id in ru.machine_ids
                             and ru.threshold == c.threshold), None)
            if rule is None:
                continue
            if (v >= rule.threshold) if rule.op == ">=" else (v < rule.threshold):
                return True
        return False

    @staticmethod
    def _decisions(c: SafetyCase) -> list:
        return ["qc_pass", "scrap"] if "SR-CURE-LOW-120" in c.rule_ids else ["resume"]

    # ---- reads
    def get(self, case_id: str) -> SafetyCase:
        return self._cases[case_id]

    def open_for(self, machine_id: str) -> SafetyCase | None:
        return next((c for c in self._cases.values() if c.machine_id == machine_id and c.status == "open"), None)

    def view(self, board: Any = None) -> list[SafetyCase]:
        """Open cases (newest first) and the last few resolved ones, with ① counted from the task board."""
        out = []
        for c in self._cases.values():
            self._refresh(c, board)
            out.append(c)
        open_ = sorted((c for c in out if c.status == "open"), key=lambda c: c.ts, reverse=True)
        done_ = sorted((c for c in out if c.status != "open"), key=lambda c: c.resolved_at or c.ts, reverse=True)
        return open_ + done_[:KEEP_RESOLVED]

    @staticmethod
    def _refresh(c: SafetyCase, board: Any) -> None:
        if board is None or not c.task_ids:
            return
        live = {t.id: t.status for t in board.for_event(c.id)}
        # a task no longer on the board was trimmed, and the board trims only finished ones
        c.tasks_done = sum(1 for t in c.task_ids if live.get(t, "完成") == "完成")

    # ---- supervisor writes (called only from the supervisor-session routes)
    def dispatch(self, case_id: str, actor: str, board: Any, now: datetime) -> list[Any]:
        c = self._cases[case_id]
        if c.status != "open":
            raise ValueError("這件安全核心處置已經解除")
        if c.task_ids:
            raise ValueError("已派過現場檢查")
        if board is None or not callable(getattr(board, "add_specs", None)):
            raise ValueError("現場任務板不可用（模擬工廠沒有人員）")
        sops, specs = sop_for_rules(c.rule_ids, c.machine_id)
        tasks = board.add_specs(c.id, specs, now)
        c.task_ids, c.sop_ids, c.dispatched_at, c.tasks_done = [t.id for t in tasks], sops, now, 0
        self.audit.append(AuditKind.TASK, actor,
                          {"created": [t.model_dump(mode="json", include={"id", "kind", "title", "team", "machine_id",
                                                                          "zone", "status", "worker_id", "eta"})
                                       for t in tasks],
                           "safety_case": c.id, "rule_ids": c.rule_ids, "sop_ids": sops, "generated_by": "code"},
                          c.event_id or c.id)
        return tasks

    def check_release(self, c: SafetyCase, body: SafetyRelease, board: Any) -> None:
        """ValueError (→ 409) with the reason the release is not allowed yet."""
        if c.status != "open":
            raise ValueError("這件安全核心處置已經解除")
        if body.decision not in c.decisions:
            raise ValueError(f"這條規則的解除方式是：{'、'.join(c.decisions)}")
        reason = body.reason.strip()
        if body.field_confirmed and not reason:
            raise ValueError("勾選「已現場確認」時請寫理由")
        # CJ 10-22 (option B): 「QC 合格放行」 judges the tyre, not the press: field check only. 確認恢復運轉 still needs ②.
        if body.decision == "resume" and not c.reading_ok:
            raise ValueError(f"讀值還沒回到安全範圍（連續 {c.safe_samples}/{c.safe_needed} 筆）")
        self._refresh(c, board)
        done = c.tasks_done
        if not body.field_confirmed:
            if not c.task_ids:
                raise ValueError("請先派現場檢查，或勾選「已現場確認」並寫理由")
            if done < len(c.task_ids):
                raise ValueError(f"現場檢查還沒完成（{done}/{len(c.task_ids)}）")

    def release(self, case_id: str, body: SafetyRelease, actor: str, sim: Any, board: Any, now: datetime) -> SafetyCase:
        c = self._cases[case_id]
        self.check_release(c, body, board)
        st = sim.machine(c.machine_id)
        before = st.status
        scrapped = passed = None
        if body.decision == "scrap":
            branch = "scrap"
            fn = getattr(sim, "scrap_held_job", None)
            scrapped = fn(c.machine_id) if callable(fn) else None
            if scrapped is None and before is MachineStatus.STOPPED:
                sim.set_machine_status(c.machine_id, MachineStatus.IDLE, "QC 報廢，換模入模中")
        elif body.decision == "qc_pass" and not c.reading_ok:
            # the tyre is accepted as made; the press is still out of range, so it must NOT go back to RUNNING:
            # de-mold as a completed tyre and take the press out of service until it is repaired
            branch = "qc_pass_press_out_of_service"
            fn = getattr(sim, "pass_held_job", None)
            passed = fn(c.machine_id) if callable(fn) else None
            if passed is None:
                sim.set_machine_status(c.machine_id, MachineStatus.OUT_OF_SERVICE, "QC 放行，機台待維修／讀值未回到安全範圍")
        elif before in (MachineStatus.STOPPED, MachineStatus.ALARM):
            branch = f"{body.decision}_running"
            # STOPPED / ALARM only: an OUT_OF_SERVICE decision is not the core's and stays
            sim.set_machine_status(c.machine_id, MachineStatus.RUNNING, "主管確認恢復運轉")
        else:
            branch = f"{body.decision}_status_kept"
        after = sim.machine(c.machine_id).status
        c.status, c.resolved_at, c.resolved_by = "resolved", now, actor
        c.resolution, c.reason, c.field_confirmed = body.decision, body.reason.strip() or None, body.field_confirmed
        self.audit.append(AuditKind.SAFETY_RELEASE, actor,
                          {"case_id": c.id, "machine_id": c.machine_id, "rule_ids": c.rule_ids, "decision": body.decision,
                           "reason": c.reason, "field_confirmed": body.field_confirmed, "reading": c.reading,
                           "safe_samples": c.safe_samples, "safe_needed": c.safe_needed, "task_ids": c.task_ids,
                           "tasks_done": c.tasks_done, "status_before": before.value, "status_after": after.value,
                           "scrapped_job": scrapped, "passed_job": passed, "branch": branch,
                           "safety_action_ids": c.actions},
                          c.event_id or c.id)
        return c
