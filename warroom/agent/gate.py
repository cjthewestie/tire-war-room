"""Action Gate (page 5 動作把關, page 7 threshold 4). Deterministic, no LLM.

Checks, in order: the tool is on the allow-list, the tool is open in the state it will run in
(TOOLS_BY_STATE), the args validate against the tool's *Args model with known machine ids and no
unresolved "$S.." references, and for writes the simulation was feasible. Then a risk level:

  R0 read/compute (auto)                R1 notify maintenance/info (auto, 已自動)
  R2 set_cure_time, confirm_schedule,   R3 notify slow_feed/pause_feed/staff_move/machine_out, or
     notify overtime / dispatch            a schedule containing a cross-team transfer (approval)
     (派工, issue #17) (approval)

Anything not on the allow-list or not open in that state is REJECT with risk None; the state
machine writes it to the audit log as INTERCEPTED. There are no actuator / PLC tools to allow.

R1 is earned by content, not by the label the commander picks: a maintenance/info notify is R1
only when its text is one of the fixed notice templates (R1_TEMPLATES) for the machine it names and
it goes to maintenance/QC/supervisor or that machine's own team. Free text is R3 (supervisor).
A template that carries numbers (the cure-extension notice) is R1 only when every number is the
value of THIS event's approved set_cure_time for that press and its cure formula's extra hours
(evidence["cure_notice"]); without that evidence it is R3. With evidence["event_machine_id"], an R1
notice must be about the event's own machine.

With ``evidence`` (the state machine passes what THIS event computed) more checks apply:
confirm_schedule only for a version a feasible solve_schedule produced in this event;
set_cure_time and every solve_schedule.cure_overrides_h value only with the cure_formula's own
value for that same press (physics numbers never come from the LLM).

Writes only through the gate (G2/G3): a write the gate let through (ALLOW, or NEEDS_APPROVAL that a
supervisor approved) gets an opaque, single-use permit from issue_permit(). The tool registry runs a
write tool only with a permit it can redeem here for that step id, tool and args digest; anything
else is refused as ``ungated_write`` and logged INTERCEPTED. gated_call() is the one path for writes
outside the state machine's own EXECUTE loop (runtime rollback notices): GATE -> TOOL_CALL ->
TOOL_RESULT in the audit log, in that order.

Code-only notices (CODE_NOTICE_TEMPLATES): the staffing revocation and rollback notices the code
writes itself. They are never shown to the model (not in R1_TEMPLATES, so not in the system
prompt) and are R1 only when evidence["code_notice"] carries exactly that message and those teams.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import unicodedata
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, ValidationError

from warroom.contracts import (
    APPROVAL_REQUIRED,
    AuditKind,
    READ_COMPUTE_TOOLS,
    TOOLS_BY_STATE,
    WRITE_TOOLS,
    ConfirmScheduleArgs,
    CureFormulaArgs,
    EventState,
    GateDecision,
    GateVerdict,
    NotifyKind,
    NotifyTeamsArgs,
    PlanStep,
    PredictArgs,
    QueryOrdersShiftsArgs,
    ReadSensorsArgs,
    RiskLevel,
    SetCureTimeArgs,
    SolveScheduleArgs,
    SopSearchArgs,
    TeamId,
    ToolCall,
    ToolName,
    ToolResult,
)
from warroom.contracts import Line
from warroom.factory import CURE_PRESS_IDS, MACHINE_BY_ID, MACHINE_IDS
from warroom.agent.narrate import machine_label

ARGS_MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.READ_SENSORS: ReadSensorsArgs,
    ToolName.PREDICT_ANOMALY: PredictArgs,
    ToolName.CURE_FORMULA: CureFormulaArgs,
    ToolName.SOLVE_SCHEDULE: SolveScheduleArgs,
    ToolName.SOP_SEARCH: SopSearchArgs,
    ToolName.QUERY_ORDERS_SHIFTS: QueryOrdersShiftsArgs,
    ToolName.CONFIRM_SCHEDULE: ConfirmScheduleArgs,
    ToolName.SET_CURE_TIME: SetCureTimeArgs,
    ToolName.NOTIFY_TEAMS: NotifyTeamsArgs,
}

NOTIFY_RISK: dict[NotifyKind, RiskLevel] = {
    NotifyKind.MAINTENANCE: RiskLevel.R1_NOTIFY,
    NotifyKind.INFO: RiskLevel.R1_NOTIFY,
    NotifyKind.OVERTIME: RiskLevel.R2_CHANGE,
    NotifyKind.SLOW_FEED: RiskLevel.R3_CRITICAL,
    NotifyKind.PAUSE_FEED: RiskLevel.R3_CRITICAL,
    NotifyKind.STAFF_MOVE: RiskLevel.R3_CRITICAL,
    NotifyKind.MACHINE_OUT: RiskLevel.R3_CRITICAL,
    # issue #17: 派工 = an approved plan handed to the floor; code turns it into worker tasks. A plan change
    # within the plant's own teams, so R2 (approval), never auto
    NotifyKind.DISPATCH: RiskLevel.R2_CHANGE,
}


# R1 (auto, 已自動) is earned by a FIXED notice, never by free text: an R1 notify must be one of the
# code-owned templates below for the machine it names, sent only to maintenance / QC / supervisor or
# that machine's own team. Anything else (an instruction in free text, a homoglyph or spaced-out
# word, a notice to every team) is R3 and waits for the supervisor. Before matching, the text is
# NFKC-normalised and stripped of whitespace and invisible format characters (zero-width etc.).
R1_TEMPLATES: dict[NotifyKind, tuple[str, ...]] = {
    NotifyKind.MAINTENANCE: (
        "{m}冷卻水流偏低，請檢修冷卻水路",
        "{m}膠溫偏高，請檢查冷卻系統",
        "{m}膠溫未回落，冷卻水路列為最優先檢修",
        "{m}溫度下降，請檢查蒸汽供應與溫度計",
        "{m}疑持續洩壓或感測器漂移，請檢查蒸汽管路與溫度計",
        "{m}異常，請現場確認",
    ),
    NotifyKind.INFO: (
        "{m}延長硫化至 {n} h（+{n} h），已依新排程調整下一模",
        "{m}：系統工具失敗，請依 SOP 人工處置，排程維持原版",
        "{m}異常處置中，請留意後續通知",
    ),
}
# Notices only the CODE writes (never shown to the model: not in R1_TEMPLATES, so not in the system
# prompt or its prefix cache). Every field is filled by the code from a StaffingChange and schedule
# versions, never from an earlier round's (possibly model-written) message. Built by revoke_notice()
# and rollback_notice(); R1 only with evidence["code_notice"] naming exactly that message and teams.
CODE_NOTICE_TEMPLATES: dict[str, str] = {
    "revoke": "撤回：{date} {shift} {label}（主管改選{new_label}）",
    "rollback": "排程回復 v{from_v} → v{to_v}：{verb} {date} {shift}{label}",
}
ROLLBACK_VERBS = ("撤回", "恢復")


def _shift_date_text(d: Any) -> str:
    """"MM/DD" from a date, a datetime or an ISO date string; "" when it is none of those."""
    from datetime import date, datetime
    if isinstance(d, (date, datetime)):
        return f"{d:%m/%d}"
    try:
        return f"{datetime.fromisoformat(str(d)):%m/%d}"
    except (TypeError, ValueError):
        return ""


def _field(c: Any, name: str) -> Any:
    return c.get(name) if isinstance(c, dict) else getattr(c, name, None)


def revoke_notice(prev: Any, new: Any) -> str:
    """The withdrawal notice for an earlier round's staffing change ``prev`` that the supervisor
    replaced by ``new`` (StaffingChange or its dict). Only code-owned fields, no free text."""
    return CODE_NOTICE_TEMPLATES["revoke"].format(
        date=_shift_date_text(_field(prev, "shift_date")), shift=str(_field(prev, "shift_name") or ""),
        label=str(_field(prev, "label") or ""), new_label=str(_field(new, "label") or ""))


def rollback_notice(from_v: int, to_v: int, verb: str, change: Any) -> str:
    """The notice a schedule rollback sends for a staffing change it revokes or brings back."""
    if verb not in ROLLBACK_VERBS:
        raise ValueError(f"rollback verb {verb!r}")
    return CODE_NOTICE_TEMPLATES["rollback"].format(
        from_v=int(from_v), to_v=int(to_v), verb=verb, date=_shift_date_text(_field(change, "shift_date")),
        shift=str(_field(change, "shift_name") or ""), label=str(_field(change, "label") or ""))


def change_teams(change: Any) -> list[str]:
    """Teams a staffing change concerns: the receiving team, plus the giving team of a transfer."""
    out: list[str] = []
    for t in (_field(change, "to_team"), _field(change, "from_team") if _field(change, "kind") == "transfer" else None):
        v = getattr(t, "value", t)
        if v and v not in out:
            out.append(str(v))
    return out


def _code_notice_ok(args: NotifyTeamsArgs, evidence: dict[str, Any] | None) -> bool:
    """True when this info notice is exactly the code-built notice the evidence names."""
    want = (evidence or {}).get("code_notice")
    if not isinstance(want, dict) or NotifyKind(args.kind) is not NotifyKind.INFO:
        return False
    if _norm(str(want.get("message") or "")) != _norm(args.message) or not args.message:
        return False
    if sorted(str(t) for t in want.get("teams") or []) != sorted(TeamId(t).value for t in args.teams):
        return False
    return any(_compile_code(t).match(_norm(args.message)) for t in CODE_NOTICE_TEMPLATES.values())


R1_ALWAYS_TEAMS = frozenset({TeamId.MAINTENANCE, TeamId.QC, TeamId.SUPERVISOR})
CURE_H_TOLERANCE = 0.05            # h; the formula rounds to 0.1 h


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in t if not ch.isspace() and unicodedata.category(ch) not in ("Cf", "Cc", "Zs",
                                                                                            "Zl", "Zp"))


def _compile(tpl: str) -> re.Pattern[str]:
    """Template -> full-match regex: {m} = the machine label (checked against machine_id), {n} = a
    number, captured in order as n0, n1, ... (checked against the event's own values)."""
    parts = re.split(r"(\{m\}|\{n\})", _norm(tpl))
    out, k = [], 0
    for p in parts:
        if p == "{m}":
            out.append("(?P<m>.+?)")
        elif p == "{n}":
            out.append(rf"(?P<n{k}>\d+(?:\.\d+)?)")
            k += 1
        else:
            out.append(re.escape(p))
    return re.compile(rf"^{''.join(out)}$")


_CODE_FIELD_RX = {"from_v": r"\d+", "to_v": r"\d+", "date": r"(?:\d{2}/\d{2})?",
                  "verb": "(?:" + "|".join(ROLLBACK_VERBS) + ")"}
_CODE_TEXT_RX = r"[^，,。；;：:！!？?「」]*?"       # a label / shift name: no sentence punctuation


def _compile_code(tpl: str) -> re.Pattern[str]:
    """Code-only template -> full-match regex; a free field cannot carry a second sentence."""
    out = []
    for p in re.split(r"(\{[a-z_]+\})", _norm(tpl)):
        f = re.fullmatch(r"\{([a-z_]+)\}", p)
        out.append(_CODE_FIELD_RX.get(f.group(1), _CODE_TEXT_RX) if f else re.escape(p))
    return re.compile("^" + "".join(out) + "$")


_R1_RX: dict[NotifyKind, tuple[re.Pattern[str], ...]] = {k: tuple(_compile(t) for t in v)
                                                          for k, v in R1_TEMPLATES.items()}


def own_teams(machine_id: str | None) -> frozenset[TeamId]:
    """The team(s) that run a machine: R1 notices may go to them (plus maintenance/QC/supervisor)."""
    m = MACHINE_BY_ID.get(machine_id or "")
    if m is None:
        return frozenset()
    if m.line is Line.CURE:
        return frozenset({TeamId.CURING, TeamId.BODY})      # night body team also de-molds (page 6)
    if machine_id == "A-BIGOPEN":
        return frozenset({TeamId.BIGOPEN, TeamId.BODY})
    return frozenset({{Line.A: TeamId.BODY, Line.B: TeamId.SIDE, Line.C: TeamId.BEAD_NYLON,
                       Line.D: TeamId.BEAD_NYLON, Line.BOND: TeamId.BOND}.get(m.line, TeamId.BODY)})


def cure_notice_press(args: NotifyTeamsArgs | dict[str, Any]) -> str | None:
    """The press a cure-extension notice (an R1 template with numbers) is about, else None."""
    try:
        a = args if isinstance(args, NotifyTeamsArgs) else NotifyTeamsArgs.model_validate(args)
    except ValidationError:
        return None
    if NotifyKind(a.kind) not in _R1_RX or not a.machine_id:
        return None
    msg, label = _norm(a.message), _norm(machine_label(a.machine_id))
    for rx in _R1_RX[NotifyKind(a.kind)]:
        if rx.groups > 1 and (m := rx.match(msg)) and m.groupdict().get("m", label) == label:
            return a.machine_id
    return None


def _numbers_problem(m: re.Match, machine_id: str, evidence: dict[str, Any] | None) -> str | None:
    """A template's numbers must be this event's own: {n0} the approved set_cure_time total for that
    press, {n1} the cure formula's extra hours (evidence["cure_notice"][press])."""
    nums = [float(m.group(k)) for k in sorted(g for g in m.groupdict() if g.startswith("n"))]
    if not nums:
        return None
    ref = ((evidence or {}).get("cure_notice") or {}).get(machine_id)
    if not ref:
        return ("R1 notice carries cure hours, but this event has no approved set_cure_time for "
                f"{machine_id} (numbers in an auto notice come only from the formula and an approved write)")
    want = [ref.get("total_h"), ref.get("extra_h")]
    if len(nums) != len(want) or any(w is None or abs(n - float(w)) > CURE_H_TOLERANCE for n, w in zip(nums, want)):
        return (f"R1 notice hours {' / '.join(f'{n:g}' for n in nums)} differ from this event's approved cure time "
                f"for {machine_id} ({' / '.join('?' if w is None else f'{float(w):g}' for w in want)})")
    return None


def r1_violation(args: NotifyTeamsArgs, evidence: dict[str, Any] | None = None) -> str | None:
    """Why an R1 (auto) notify must go to the supervisor instead, or None when it is one of the
    fixed R1 notices for its machine, goes only to teams allowed to get it, is about the event's
    machine (when evidence names it) and carries no number that is not this event's own."""
    kind = NotifyKind(args.kind)
    if kind not in _R1_RX:
        return None
    if _code_notice_ok(args, evidence):
        return None
    if not args.machine_id or args.machine_id not in MACHINE_BY_ID:
        return "R1 notice must name the machine it is about"
    ev_mid = (evidence or {}).get("event_machine_id")
    if ev_mid and args.machine_id != ev_mid:
        return f"R1 notice is about {args.machine_id}, not this event's machine {ev_mid}"
    allowed = R1_ALWAYS_TEAMS | own_teams(args.machine_id)
    extra = [t.value for t in args.teams if TeamId(t) not in allowed]
    if extra:
        return f"R1 notice to team(s) outside maintenance/QC/supervisor and the machine's own team: {', '.join(extra)}"
    msg, label = _norm(args.message), _norm(machine_label(args.machine_id))
    for rx in _R1_RX[kind]:
        m = rx.match(msg)
        if m and m.groupdict().get("m", label) == label:
            return _numbers_problem(m, args.machine_id, evidence)
    return "R1 notice is not one of the fixed notice templates (free text needs approval)"


def has_unresolved_ref(v: Any) -> bool:
    if isinstance(v, str):
        return v.startswith("$S") or "${S" in v
    if isinstance(v, dict):
        return any(has_unresolved_ref(x) for x in v.values()) or any(has_unresolved_ref(k) for k in v)
    if isinstance(v, (list, tuple)):
        return any(has_unresolved_ref(x) for x in v)
    return False


def _bad_ids(tool: ToolName, a: BaseModel) -> list[str]:
    bad: list[str] = []
    if isinstance(a, ReadSensorsArgs):
        bad += [m for m in a.machine_ids if m not in MACHINE_IDS]
    elif isinstance(a, PredictArgs):
        bad += [a.machine_id] if a.machine_id not in MACHINE_IDS else []
    elif isinstance(a, SetCureTimeArgs):
        bad += [a.press_id] if a.press_id not in CURE_PRESS_IDS else []
    elif isinstance(a, NotifyTeamsArgs):
        bad += [a.machine_id] if a.machine_id and a.machine_id not in MACHINE_IDS else []
    elif isinstance(a, SolveScheduleArgs):
        bad += [p for p in [*a.unavailable_presses, *a.cure_overrides_h, *a.press_blackouts] if p not in CURE_PRESS_IDS]
    elif isinstance(a, QueryOrdersShiftsArgs):
        bad += [p for p in (a.press_ids or []) if p not in CURE_PRESS_IDS]
    return bad


def args_digest(args: dict[str, Any]) -> str:
    """Stable digest of a tool call's args (key order and enum-vs-string do not matter)."""
    blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=lambda o: getattr(o, "value", str(o)))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class UnknownToolDecision(GateDecision):
    """A REJECT for a tool name that is not a ToolName at all: it keeps the name exactly as asked
    (the contract's GateDecision.tool can only hold allow-listed names)."""
    tool: str  # type: ignore[assignment]


class PermitBook:
    """Gate-side record of what the gate let through, and the permits it signed for it.

    note() remembers the verdict of every EXECUTE-state write check by (step id, tool, args digest).
    issue() signs an opaque single-use token only for a write the gate allowed, or one that needed
    approval and carries the approval id. redeem() (called by the tool registry) consumes a token
    when it matches the call's step id, tool and args digest. Bounded, thread-safe."""

    MAX = 4096

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._verdicts: OrderedDict[tuple[str, str, str], GateVerdict] = OrderedDict()
        self._tokens: OrderedDict[str, tuple[str, str, str]] = OrderedDict()

    @staticmethod
    def _key(step_id: str | None, tool: Any, args: dict[str, Any]) -> tuple[str, str, str]:
        return (str(step_id or ""), str(getattr(tool, "value", tool)), args_digest(args))

    def note(self, step: PlanStep, decision: GateDecision) -> None:
        with self._lock:
            k = self._key(step.id, step.tool, step.args)
            self._verdicts[k] = decision.verdict
            self._verdicts.move_to_end(k)
            while len(self._verdicts) > self.MAX:
                self._verdicts.popitem(last=False)

    def issue(self, step: PlanStep, *, approval_id: str | None = None) -> str | None:
        k = self._key(step.id, step.tool, step.args)
        with self._lock:
            v = self._verdicts.get(k)
            if v is GateVerdict.ALLOW or (v is GateVerdict.NEEDS_APPROVAL and approval_id):
                tok = secrets.token_urlsafe(18)
                self._tokens[tok] = k
                while len(self._tokens) > self.MAX:
                    self._tokens.popitem(last=False)
                return tok
        return None

    def redeem(self, token: str | None, call: ToolCall) -> bool:
        if not token:
            return False
        with self._lock:
            k = self._tokens.get(token)
            if k is None or k != self._key(call.step_id, call.tool, call.args):
                return False
            del self._tokens[token]
            return True


class RuleGate:
    """Implements contracts.ActionGate.

    store: optional ScheduleStore; confirm_schedule is R3 when that version's schedule has a
    cross-team transfer. The state machine can also pass cross_team= directly (it knows the solver
    result the step refers to)."""

    def __init__(self, store=None):
        self._store = store
        self.permits = PermitBook()

    def issue_permit(self, step: PlanStep, *, approval_id: str | None = None) -> str | None:
        """A single-use permit for this exact write (step id, tool, args), or None. Only for a step
        this gate last judged ALLOW in EXECUTE, or NEEDS_APPROVAL with the approval that cleared it."""
        return self.permits.issue(step, approval_id=approval_id)

    def _schedule_has_transfer(self, version: int) -> bool:
        if self._store is None:
            return False
        try:
            sv = self._store.get(version)
        except Exception:  # noqa: BLE001
            return False
        return any(c.kind == "transfer" for c in sv.schedule.staffing_changes)

    def risk_of(self, tool: ToolName, args: BaseModel | None, cross_team: bool | None = None,
                evidence: dict[str, Any] | None = None) -> RiskLevel:
        if tool in READ_COMPUTE_TOOLS:
            return RiskLevel.R0_READ
        if tool is ToolName.NOTIFY_TEAMS:
            kind = args.kind if isinstance(args, NotifyTeamsArgs) else NotifyKind.INFO
            risk = NOTIFY_RISK[NotifyKind(kind)]
            if risk is RiskLevel.R1_NOTIFY and (not isinstance(args, NotifyTeamsArgs) or r1_violation(args, evidence)):
                return RiskLevel.R3_CRITICAL
            return risk
        if tool is ToolName.CONFIRM_SCHEDULE:
            if cross_team is None and isinstance(args, ConfirmScheduleArgs):
                cross_team = self._schedule_has_transfer(args.schedule_version)
            return RiskLevel.R3_CRITICAL if cross_team else RiskLevel.R2_CHANGE
        return RiskLevel.R2_CHANGE   # set_cure_time

    def check(self, step: PlanStep, state: EventState, simulation_feasible: bool,
              *, cross_team: bool | None = None, evidence: dict[str, Any] | None = None) -> GateDecision:
        """evidence (optional, from the state machine): {"feasible_versions": [int], "cure_formula_h":
        [float], "cure_formula_by_press": {press: [float]}, "event_machine_id": str, "cure_notice":
        {press: {"total_h", "extra_h"}}, "simulation_why": str} = schedule versions a feasible solve
        produced, cure_formula totals (per press when known), the event's machine, the approved cure
        time an auto notice may quote, and why a write has no feasible simulation, all from this
        event's own results."""
        checks = {"allow_list": False, "state_allowed": False, "format_ok": False, "simulation_feasible": False}
        reasons: list[str] = []
        try:
            tool = ToolName(step.tool)
        except ValueError:
            reasons.append(f"tool {step.tool!r} is not on the allow-list (intercepted)")
            return UnknownToolDecision(step_id=step.id, tool=str(getattr(step.tool, "value", step.tool)),
                                       verdict=GateVerdict.REJECT, risk=None, checks=checks, reasons=reasons)
        checks["allow_list"] = tool in READ_COMPUTE_TOOLS or tool in WRITE_TOOLS
        if not checks["allow_list"]:
            reasons.append(f"{tool.value} not on the allow-list")
        checks["state_allowed"] = tool in TOOLS_BY_STATE.get(state, frozenset())
        if not checks["state_allowed"]:
            reasons.append(f"{tool.value} is not open in state {state.value}")

        parsed: BaseModel | None = None
        if has_unresolved_ref(step.args):
            reasons.append("args contain an unresolved $S reference")
        else:
            try:
                parsed = ARGS_MODELS[tool].model_validate(step.args)
            except ValidationError as e:
                reasons.append(f"args invalid: {e.errors()[0].get('msg', 'validation error')}"
                               f" at {'.'.join(map(str, e.errors()[0].get('loc', ())))}")
            else:
                bad = _bad_ids(tool, parsed)
                if bad:
                    reasons.append(f"unknown machine id(s): {', '.join(bad)}")
                else:
                    checks["format_ok"] = True

        is_write = tool in WRITE_TOOLS
        checks["simulation_feasible"] = simulation_feasible or not is_write
        if not checks["simulation_feasible"]:
            why = (evidence or {}).get("simulation_why")
            reasons.append("simulation not feasible: writes are blocked" + (f" ({why})" if why else ""))
        if evidence is not None and parsed is not None:
            why = self._evidence_problem(parsed, evidence)
            if why:
                checks["evidence_ok"] = False
                reasons.append(why)
            elif tool in (ToolName.CONFIRM_SCHEDULE, ToolName.SET_CURE_TIME) or \
                    (isinstance(parsed, SolveScheduleArgs) and parsed.cure_overrides_h):
                checks["evidence_ok"] = True
        if isinstance(parsed, NotifyTeamsArgs) and NOTIFY_RISK[parsed.kind] is RiskLevel.R1_NOTIFY:
            why = r1_violation(parsed, evidence)
            if why:
                reasons.append(f"{why} -> R3")

        out_of_scope = not (checks["allow_list"] and checks["state_allowed"])
        risk = None if out_of_scope else self.risk_of(tool, parsed, cross_team, evidence)
        if not all(checks.values()):
            verdict = GateVerdict.REJECT
        elif risk in APPROVAL_REQUIRED:
            verdict = GateVerdict.NEEDS_APPROVAL
            reasons.append(f"{risk.value}: supervisor approval required")
        else:
            verdict = GateVerdict.ALLOW
        decision = GateDecision(step_id=step.id, tool=tool, verdict=verdict, risk=risk, checks=checks,
                                reasons=reasons)
        if is_write and state is EventState.EXECUTE:
            self.permits.note(step, decision)
        return decision

    @staticmethod
    def _formula_for(evidence: dict[str, Any], press: str) -> list[float] | None:
        """Cure-formula totals this event computed for that press. None = the evidence does not say
        which press a formula result belongs to (old callers): any formula total of the event."""
        by_press = evidence.get("cure_formula_by_press")
        if isinstance(by_press, dict):
            v = by_press.get(press)
            return [float(x) for x in (v if isinstance(v, list) else [v]) if x is not None] if v is not None else []
        return None

    @classmethod
    def _cure_problem(cls, press: str, hours: float, evidence: dict[str, Any], what: str) -> str | None:
        anywhere = [float(x) for x in evidence.get("cure_formula_h") or [] if x is not None]
        formula = cls._formula_for(evidence, press)
        if formula is None:
            formula = anywhere
        if not formula:
            if anywhere:
                return (f"{what} for {press} has no cure_formula result for that press in this event "
                        "(the formula was run for another press)")
            return f"{what} needs a cure_formula result in this event (never an AI guess)"
        if not any(abs(hours - f) <= CURE_H_TOLERANCE for f in formula):
            return f"{what} {hours:g} h for {press} differs from the cure formula ({' / '.join(f'{f:g}' for f in formula)} h)"
        return None

    @classmethod
    def _evidence_problem(cls, a: BaseModel, evidence: dict[str, Any]) -> str | None:
        if isinstance(a, ConfirmScheduleArgs):
            ok = {int(v) for v in evidence.get("feasible_versions") or [] if v is not None}
            if a.schedule_version not in ok:
                return (f"schedule v{a.schedule_version} was not produced by a feasible solve_schedule in this "
                        "event (no simulation for it)")
        elif isinstance(a, SetCureTimeArgs):
            return cls._cure_problem(a.press_id, a.total_cure_h, evidence, "total_cure_h")
        elif isinstance(a, SolveScheduleArgs):
            for press, hours in a.cure_overrides_h.items():
                why = cls._cure_problem(press, hours, evidence, "cure_overrides_h")
                if why:
                    return why
        return None


def gated_call(gate: Any, tools: Any, audit: Any, step: PlanStep, *, actor: str, event_id: str | None = None,
               simulation_feasible: bool = True, evidence: dict[str, Any] | None = None,
               call_id: str | None = None) -> tuple[GateDecision, ToolResult | None]:
    """Run one write outside the state machine's EXECUTE loop, through the gate: check it (EXECUTE),
    audit GATE (INTERCEPTED when out of scope), and only on ALLOW sign a permit, audit TOOL_CALL, call
    the registry with the permit and audit TOOL_RESULT, in that order. NEEDS_APPROVAL / REJECT: no
    call, the result is None (the caller decides who must approve). Never raises on a tool error."""
    kw: dict[str, Any] = {} if evidence is None else {"evidence": evidence}
    gd = gate.check(step, EventState.EXECUTE, simulation_feasible, **kw)
    kind = AuditKind.INTERCEPTED if gd.risk is None else AuditKind.GATE
    audit.append(kind, "gate", {"step_id": step.id, "tool": str(getattr(step.tool, "value", step.tool)),
                                "args": step.args, "by": actor, "decision": gd.model_dump(mode="json")},
                 event_id=event_id)
    if gd.verdict is not GateVerdict.ALLOW or tools is None:
        return gd, None
    issue = getattr(gate, "issue_permit", None)
    permit = issue(step) if callable(issue) else None
    call = ToolCall(id=call_id or step.id, event_id=event_id, step_id=step.id, tool=step.tool, args=step.args)
    audit.append(AuditKind.TOOL_CALL, f"tool:{step.tool.value}",
                 {"call_id": call.id, "step_id": step.id, "args": step.args, "attempt": 1,
                  "state": EventState.EXECUTE.value, "by": actor}, event_id=event_id)
    try:
        res = tools.call(call, permit=permit)
    except Exception as e:  # noqa: BLE001 - the registry never raises; be safe anyway
        res = ToolResult(call_id=call.id, tool=step.tool, ok=False, error=f"{type(e).__name__}: {e}")
    audit.append(AuditKind.TOOL_RESULT, f"tool:{step.tool.value}",
                 {"call_id": call.id, "step_id": step.id, "ok": res.ok, "error": res.error,
                  "duration_ms": res.duration_ms, "attempt": 1}, event_id=event_id)
    return gd, res
