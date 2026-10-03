"""Event Commander — the ONLY LLM in the system (page 5).

OpenAICommander speaks the OpenAI chat-completions API, so one code path runs on Ollama (Mac),
vLLM (DGX Spark, Nemotron 3.5 Lightning NVFP4) or NVIDIA cloud via BASE_URL / MODEL:

- Constrained output: the CommanderPlan JSON schema. On vLLM it goes in extra_body guided_json
  (xgrammar); elsewhere (Ollama, cloud) as response_format json_schema.
- The schema lists ``alert_card`` first, so the ≤100-token alert card is the first thing the model
  streams; ``on_alert`` fires as soon as that object is complete (page 9: first card ≤ 2 s).
- The system prompt is byte-identical on every call (tool catalogue, rules, factory facts), and all
  per-event data goes in the user message, so vLLM prefix caching reuses the whole prefix.
- Streaming measures TTFT. Invalid JSON, timeout or unreachable server falls back to the mock plan
  when settings.llm_fallback_to_mock (logged as fell_back_to_mock / valid_json=False).

MockCommander is a deterministic rule commander that reproduces page 11 (H1 steam pressure →
H2 leak / sensor drift) and page 2 (cooling insufficient) with no model at all.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from warroom.agent.narrate import machine_label
from warroom.config import Settings
from warroom.contracts import (
    AlertCard,
    AnomalyKind,
    Channel,
    CommanderClient,
    CommanderPlan,
    CommanderRequest,
    CompoundStage,
    Expectation,
    Hypothesis,
    HypothesisStatus,
    Line,
    LLMCallInfo,
    LLMMode,
    NotifyKind,
    PlanStep,
    TeamId,
    TireSize,
    ToolName,
)
from warroom.factory import (
    ACTIVATION_ENERGY_KJ_MOL,
    ALERT_CARD_MAX_TOKENS,
    COMPOUND_STOP_C,
    CURATIVE_STOP_C,
    CURE_HOURS,
    CURE_TEMP_MAX_C,
    CURE_TEMP_MIN_C,
    CURE_TEMP_NOMINAL_C,
    MACHINE_BY_ID,
)

AlertCallback = Callable[[AlertCard], Any]


# =====================================================================================
# Alert card budget (≤ 100 tokens)
# =====================================================================================
_CJK = re.compile(r"[　-鿿＀-￯]")


def approx_tokens(s: str) -> int:
    """Rough token count: one per CJK char, ~1.3 per latin word / number."""
    cjk = len(_CJK.findall(s))
    rest = _CJK.sub(" ", s)
    words = len(re.findall(r"[A-Za-z0-9.+\-→°%#]+", rest))
    return cjk + int(words * 1.3 + 0.999)


def clamp_alert_card(card: AlertCard, max_tokens: int = ALERT_CARD_MAX_TOKENS) -> AlertCard:
    title, summary = card.title, card.summary
    while approx_tokens(title) > max_tokens // 3 and len(title) > 4:
        title = title[:-1]
    while approx_tokens(title) + approx_tokens(summary) > max_tokens and summary:
        summary = summary[:-1]
    if summary != card.summary:
        summary = summary.rstrip("，、；。 ") + "…"
    return AlertCard(title=title, summary=summary)


# =====================================================================================
# Prompt (prefix-stable) and schema
# =====================================================================================
def plan_schema() -> dict[str, Any]:
    """CommanderPlan JSON schema with alert_card first (xgrammar emits properties in order)."""
    s = CommanderPlan.model_json_schema()
    props = s["properties"]
    order = ["alert_card", "goal", "hypothesis", "rejected_hypothesis_ids", "steps", "safe_stop", "rationale"]
    s["properties"] = {k: props[k] for k in order if k in props} | {k: v for k, v in props.items() if k not in order}
    s["required"] = ["alert_card", "goal", "hypothesis", "steps"]
    return s


def _tool_catalogue() -> str:
    # Static on purpose: identical text every call (prefix cache). Allowed tools per call are in
    # the user message.
    return "\n".join([
        "- read_sensors {machine_ids: [id], minutes: float=30} 讀感測器（讀，自動）",
        "- predict_anomaly {machine_id, channel='temp_c', horizon_min=30} 時序異常預測（讀，自動）",
        "- cure_formula {size:'S'|'M'|'L', ref_temp_c=128, actual_temp_c, elapsed_h, ref_cure_h?, "
        "activation_energy_kj_mol=85} Arrhenius 等效硫化 → total_cure_h, extra_h（算，自動）",
        "- query_orders_shifts {order_ids?, press_ids?, from_ts?, to_ts?} 訂單／班表 → orders, shifts, gaps, "
        "next_jobs（讀，自動）",
        "- solve_schedule {unavailable_presses: [id], cure_overrides_h: {press_id: h}, horizon_days=7, "
        "time_limit_s=10, allow_overtime=true, allow_cross_team=true} OR-Tools 排程 → feasible, "
        "deliveries_met, moves, staffing_options, gaps, schedule_version（算，自動；逾時自動換備援求解器）",
        "- sop_search {query, k=3} SOP 知識庫（讀，自動）",
        "- confirm_schedule {schedule_version: int, reason} 確認新排程（寫，需主管核准）",
        "- set_cure_time {press_id, total_cure_h, reason} 設定硫化時間建議值（寫，需主管核准）",
        "- notify_teams {teams: [curing|body|side|bead_nylon|bigopen|bond|maintenance|qc|supervisor], "
        "kind: maintenance|info|slow_feed|pause_feed|staff_move|overtime|machine_out, message, machine_id?} "
        "通知各組（寫；maintenance/info 自動，其餘需核准）",
    ])


SYSTEM_PROMPT = f"""你是實心胎工廠戰情室的「事件指揮官」，系統中唯一的 LLM。你只輸出一個 JSON 物件（CommanderPlan），不輸出其他文字。

工廠事實：4 條產線、28 台機台、11 台硫化機（每台一顆胎；CURE-07..10 可做中／大胎，其餘只做小胎）。硫化 S 24 h / M 28 h / L 34 h，{CURE_TEMP_MIN_C:.0f}–{CURE_TEMP_MAX_C:.0f}°C，標稱 {CURE_TEMP_NOMINAL_C:.0f}°C。蒸汽管線：H1=CURE-01..05、H2=CURE-06..08、H3=CURE-09..11。活化能 {ACTIVATION_ENERGY_KJ_MOL:.0f} kJ/mol（假設值）。
安全核心（確定性 PLC 規則，你不能呼叫也不能覆寫）：煉膠 ≥{COMPOUND_STOP_C:.0f}°C 停機；已加硫 ≥{CURATIVE_STOP_C:.0f}°C 停機；硫化機 <{CURE_TEMP_MIN_C:.0f}°C 扣模。

工具（只能用使用者訊息 allowed_tools 列出的；沒有任何機台致動或 PLC 寫入工具）：
{_tool_catalogue()}

規則：
1. 先輸出 alert_card：title ≤ 20 字、summary ≤ 60 字，總計 ≤ 100 tokens。
2. 提出一個可驗證的假設 hypothesis（id 依序 H1、H2…），附 expectation（機台、通道、op、數值、within_min），驗證由程式比對，不由你判斷。
3. steps 為 S1..Sn，用 depends_on 表示依賴；讀／算工具先做，寫入工具最後。數值（硫化時間、排程）一律由工具計算，不可自行推測；後續步驟用 "$S1.total_cure_h"、"$S3.schedule_version" 引用前一步結果，字串中可寫 "${{S1.total_cure_h}}"。
4. phase=replan 時：rejected 內的假設已被程式驗證推翻，必須換假設（不可重提），rejected_hypothesis_ids 列出它們。
5. 跨組調人、暫停／放慢投料、機台停用屬 R3，必須由主管核准；你只能提出，不能執行。
6. 無法安全處理時，設 safe_stop=true（維持原排程）。
7. goal 一句話；purpose 與 rationale 用繁體中文、簡短。"""


def build_user_message(req: CommanderRequest) -> str:
    payload = {
        "phase": req.phase,
        "event_id": req.event_id,
        "anomaly": req.anomaly.model_dump(mode="json", exclude={"prediction": {"model"}}),
        "allowed_tools": [t.value for t in req.allowed_tools],
        "rejected": [h.model_dump(mode="json") for h in req.rejected],
        "context": req.context,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def build_messages(req: CommanderRequest) -> list[dict[str, str]]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": build_user_message(req)}]


# =====================================================================================
# Parsing helpers
# =====================================================================================
_ALERT_KEY = re.compile(r'"alert_card"\s*:\s*')


def extract_alert_card(partial: str) -> AlertCard | None:
    """Return the alert card from a partially streamed JSON document once its object is complete."""
    m = _ALERT_KEY.search(partial)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(partial[m.end():])
        return AlertCard.model_validate(obj)
    except Exception:  # noqa: BLE001 - incomplete yet
        return None


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    a, b = text.find("{"), text.rfind("}")
    return text[a:b + 1] if a >= 0 and b > a else text


def parse_plan(text: str) -> tuple[CommanderPlan, list[dict[str, Any]]]:
    """Validate model output. Steps naming a tool outside the allow-list are removed and returned
    as `intercepted` (the state machine writes them to the audit log). Raises on anything else."""
    raw = json.loads(_strip_fences(text))
    intercepted: list[dict[str, Any]] = []
    if isinstance(raw, dict) and isinstance(raw.get("steps"), list):
        allowed = {t.value for t in ToolName}
        keep = []
        for s in raw["steps"]:
            if isinstance(s, dict) and s.get("tool") not in allowed:
                intercepted.append(s)
            else:
                keep.append(s)
        raw["steps"] = keep
    plan = CommanderPlan.model_validate(raw)
    return plan.model_copy(update={"alert_card": clamp_alert_card(plan.alert_card)}), intercepted


# =====================================================================================
# OpenAI-compatible commander
# =====================================================================================
def _looks_like_vllm(settings: Settings) -> bool:
    url = settings.base_url.lower()
    return settings.guided_json and ":11434" not in url and "ollama" not in url and "nvidia.com" not in url


class OpenAICommander:
    """OpenAI-compatible chat client (Ollama / vLLM / NVIDIA cloud via BASE_URL, MODEL).
    Constrained output = CommanderPlan JSON schema (alert_card first): vLLM
    extra_body={"guided_json": schema} (xgrammar), else response_format={"type":"json_schema",...}.
    Validates with pydantic; on invalid JSON / timeout falls back to MockCommander when
    settings.llm_fallback_to_mock. ``last_intercepted`` holds steps dropped for naming a tool off
    the allow-list on the latest call."""
    mode = LLMMode.LLM

    def __init__(self, settings: Settings, http_client=None, max_tokens: int = 1200):
        from openai import AsyncOpenAI
        self.settings = settings
        self.model = settings.model
        self.max_tokens = max_tokens
        self.client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key_effective,
                                  timeout=settings.llm_timeout_s, max_retries=0, http_client=http_client)
        self._fallback = MockCommander()
        self.schema = plan_schema()
        self.use_guided_json = _looks_like_vllm(settings)
        self.last_intercepted: list[dict[str, Any]] = []
        self.last_raw: str = ""

    def request_kwargs(self, req: CommanderRequest) -> dict[str, Any]:
        kw: dict[str, Any] = dict(model=self.model, messages=build_messages(req), temperature=0.0,
                                  max_tokens=self.max_tokens, stream=True,
                                  stream_options={"include_usage": True})
        if self.use_guided_json:
            kw["extra_body"] = {"guided_json": self.schema}
        else:
            kw["response_format"] = {"type": "json_schema",
                                     "json_schema": {"name": "CommanderPlan", "schema": self.schema}}
        return kw

    async def plan(self, req: CommanderRequest, on_alert: AlertCallback | None = None
                   ) -> tuple[CommanderPlan, LLMCallInfo]:
        t0 = time.perf_counter()
        ttft: float | None = None
        text, usage = "", None
        alert_sent = False
        self.last_intercepted = []
        try:
            stream = await self.client.chat.completions.create(**self.request_kwargs(req))
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                for ch in chunk.choices or []:
                    delta = getattr(ch.delta, "content", None) or ""
                    if delta and ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000
                    text += delta
                if on_alert and not alert_sent:
                    card = extract_alert_card(text)
                    if card is not None:
                        alert_sent = True
                        on_alert(clamp_alert_card(card))
            self.last_raw = text
            plan, self.last_intercepted = parse_plan(text)
            info = LLMCallInfo(mode=LLMMode.LLM, model=self.model, base_url=self.settings.base_url, ttft_ms=ttft,
                               total_ms=(time.perf_counter() - t0) * 1000,
                               prompt_tokens=getattr(usage, "prompt_tokens", None),
                               completion_tokens=getattr(usage, "completion_tokens", None), valid_json=True)
            if on_alert and not alert_sent:
                on_alert(plan.alert_card)
            return plan, info
        except Exception as e:
            if not self.settings.llm_fallback_to_mock:
                raise
            plan, _ = await self._fallback.plan(req, on_alert=None if alert_sent else on_alert)
            info = LLMCallInfo(mode=LLMMode.LLM, model=self.model, base_url=self.settings.base_url, ttft_ms=ttft,
                               total_ms=(time.perf_counter() - t0) * 1000,
                               prompt_tokens=getattr(usage, "prompt_tokens", None),
                               completion_tokens=getattr(usage, "completion_tokens", None),
                               valid_json=False, fell_back_to_mock=True)
            self.last_error = f"{type(e).__name__}: {e}"[:500]
            return plan, info

    async def ping(self) -> bool:
        try:
            await self.client.models.list()
            return True
        except Exception:  # noqa: BLE001
            return False


# =====================================================================================
# Deterministic mock commander
# =====================================================================================
def _round_half(x: float) -> float:
    return round(x * 2) / 2


class MockCommander:
    """Deterministic rule commander. Reproduces page 11: plan phase -> H1 steam_pressure_low,
    expectation <press> temp_c >= 121 within 30 min, steps S1 cure_formula, S2 query_orders_shifts,
    S3 solve_schedule(dep S1,S2), S4 confirm_schedule, S5 notify_teams; replan with H1 rejected ->
    H2 steam_leak_or_sensor_drift (header neighbours compared, SOP 「硫化中斷處置」 in context),
    press out of service, reschedule. Page 2: cooling_insufficient, notify maintenance (R1) +
    slow_feed (R3). Third miss -> safe_stop (keep the original schedule)."""
    mode = LLMMode.MOCK
    model = "mock-rules"

    async def plan(self, req: CommanderRequest, on_alert: AlertCallback | None = None
                   ) -> tuple[CommanderPlan, LLMCallInfo]:
        t0 = time.perf_counter()
        plan = self.build(req)
        plan = plan.model_copy(update={"alert_card": clamp_alert_card(plan.alert_card)})
        if on_alert:
            on_alert(plan.alert_card)
        ms = (time.perf_counter() - t0) * 1000
        return plan, LLMCallInfo(mode=LLMMode.MOCK, model=self.model, base_url=None, ttft_ms=ms, total_ms=ms,
                                 prompt_tokens=None, completion_tokens=None)

    # ---- rule book ----
    def build(self, req: CommanderRequest) -> CommanderPlan:
        a = req.anomaly
        mdef = MACHINE_BY_ID.get(a.machine_id)
        rejected_codes = {h.cause_code for h in req.rejected}
        rejected_ids = [h.id for h in req.rejected]
        hid = f"H{len(req.rejected) + 1}"
        ctx = req.context or {}
        if ctx.get("sop_path"):
            return self._sop_path(req, hid, rejected_ids)
        if len(req.rejected) >= 2:
            return self._safe_stop(req, hid, rejected_ids, "連續兩個假設不成立")
        if mdef is not None and mdef.line is Line.CURE:
            if "steam_pressure_low" not in rejected_codes and req.phase == "plan":
                return self._press_h1(req, hid)
            return self._press_h2(req, hid, rejected_ids)
        if mdef is not None and (mdef.handles_compound or a.kind in (AnomalyKind.OVERHEAT,
                                                                      AnomalyKind.WATER_FLOW_LOW)):
            if "cooling_insufficient" not in rejected_codes and req.phase == "plan":
                return self._mill_h1(req, hid)
            return self._mill_h2(req, hid, rejected_ids)
        if req.phase == "replan":
            return self._safe_stop(req, hid, rejected_ids, "無可用替代假設")
        return self._generic(req, hid)

    # page 11, first round
    def _press_h1(self, req: CommanderRequest, hid: str) -> CommanderPlan:
        a, ctx = req.anomaly, req.context or {}
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        job = ctx.get("cure_job") or {}
        size = TireSize(job.get("size", TireSize.M.value))
        ref = float(job.get("setpoint_c") or CURE_TEMP_NOMINAL_C)
        elapsed = float(job.get("cure_elapsed_h", 10.0))
        stable = a.prediction.predicted_value if a.prediction else a.value
        actual = _round_half(stable)
        band = (a.prediction.band_hi - a.prediction.predicted_value) if a.prediction else 1.0
        exp_val = round(actual - max(band, 1.0))
        steps = [
            PlanStep(id="S1", tool=ToolName.CURE_FORMULA, purpose="以 Arrhenius 計算降溫後所需總硫化時間",
                     args={"size": size.value, "ref_temp_c": ref, "actual_temp_c": actual, "elapsed_h": elapsed,
                           "ref_cure_h": CURE_HOURS[size], "activation_energy_kj_mol": ACTIVATION_ENERGY_KJ_MOL}),
            PlanStep(id="S2", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="查下一模訂單交期與班表缺口",
                     args={"press_ids": [mid]}),
            PlanStep(id="S3", tool=ToolName.SOLVE_SCHEDULE, depends_on=["S1", "S2"],
                     purpose="延長硫化後重排，保大胎交期",
                     args={"cure_overrides_h": {mid: "$S1.total_cure_h"}, "time_limit_s": 10.0}),
            PlanStep(id="S4", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S3"], purpose="確認新排程（需主管核准）",
                     args={"schedule_version": "$S3.schedule_version",
                           "reason": f"{lbl}降溫，延長硫化至 ${{S1.total_cure_h}} h 後重排"}),
            PlanStep(id="S5", tool=ToolName.NOTIFY_TEAMS, depends_on=["S4"], purpose="通知硫化組與夜班新排程",
                     args={"teams": [TeamId.CURING.value, TeamId.BODY.value], "kind": NotifyKind.INFO.value,
                           "machine_id": mid,
                           "message": f"{lbl}延長硫化至 ${{S1.total_cure_h}} h（+${{S1.extra_h}} h），已依新排程調整下一模"}),
        ]
        stable_txt = f"{_fmt(stable)}±{_fmt(band)}°C"
        return CommanderPlan(
            goal=f"{machine_label(mid, short=True)}降溫，大胎不能延誤",
            hypothesis=Hypothesis(
                id=hid, statement="蒸汽壓不足，溫度會穩定", cause_code="steam_pressure_low",
                status=HypothesisStatus.ACTIVE,
                expectation=Expectation(machine_id=mid, channel=Channel.TEMP, op=">=", value=float(exp_val),
                                        within_min=30.0),
                evidence=[f"30 分鐘溫度 {_fmt(a.baseline)}→{_fmt(a.value)}°C，預測穩定在 {stable_txt}",
                          f"仍在 {CURE_TEMP_MIN_C:.0f}–{CURE_TEMP_MAX_C:.0f}°C，安全核心未觸發"]),
            steps=steps,
            alert_card=AlertCard(title=f"{lbl}溫度下降 {_fmt(a.baseline)}→{_fmt(a.value)}°C",
                                 summary=f"預測穩定在 {stable_txt}；假設蒸汽壓不足，延長硫化並重排，保大胎交期。"),
            rationale="溫度降後趨穩，符合蒸汽壓不足；先以公式延長硫化，再由求解器重排。",
        )

    # page 11, second round
    def _press_h2(self, req: CommanderRequest, hid: str, rejected_ids: list[str]) -> CommanderPlan:
        a, ctx = req.anomaly, req.context or {}
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        failed = ctx.get("failed_expectation") or {}
        observed = failed.get("observed")
        neigh = ctx.get("neighbours") or {}
        n_ok = [m for m, s in neigh.items() if m != mid and (s.get("temp_c_now") or 0) >= CURE_TEMP_MIN_C]
        evidence = []
        if observed is not None:
            evidence.append(f"{rejected_ids[-1] if rejected_ids else 'H1'} 不成立：溫度續降至 {_fmt(observed)}°C")
        if n_ok:
            nums = "、".join(m.split("-")[-1].lstrip("0") for m in n_ok)
            evidence.append(f"同管線 {nums} 號溫度正常 → 問題侷限 {machine_label(mid, short=True)}"
                            f"（排除總管蒸汽不足）")
        sop = (ctx.get("sop") or [{}])[0].get("title")
        if sop:
            evidence.append(f"依 SOP「{sop}」：停用並重排")
        steps = [
            PlanStep(id="S1", tool=ToolName.SOLVE_SCHEDULE, purpose=f"{lbl}停用，重排其模次",
                     args={"unavailable_presses": [mid], "time_limit_s": 10.0}),
            PlanStep(id="S2", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S1"], purpose="確認重排（需主管核准）",
                     args={"schedule_version": "$S1.schedule_version",
                           "reason": f"{lbl}持續降溫，停用重排"}),
            PlanStep(id="S3", tool=ToolName.NOTIFY_TEAMS, depends_on=["S2"], purpose=f"通知{lbl}停用",
                     args={"teams": [TeamId.CURING.value, TeamId.MAINTENANCE.value, TeamId.QC.value],
                           "kind": NotifyKind.MACHINE_OUT.value, "machine_id": mid,
                           "message": f"{lbl}停用：疑持續洩壓或感測器漂移，請維修檢查蒸汽管路與溫度計；在模胎交品管"}),
        ]
        reason = f"溫度續降至 {_fmt(observed)}°C" if observed is not None else "驗證未達預期"
        return CommanderPlan(
            goal=f"{machine_label(mid, short=True)}降溫，大胎不能延誤",
            hypothesis=Hypothesis(id=hid, statement="持續洩壓或感測器漂移", cause_code="steam_leak_or_sensor_drift",
                                  status=HypothesisStatus.ACTIVE, expectation=None, evidence=evidence),
            rejected_hypothesis_ids=rejected_ids,
            steps=steps,
            alert_card=AlertCard(title=f"{lbl}停用，重排中",
                                 summary=f"{rejected_ids[-1] if rejected_ids else 'H1'} 不成立：{reason}；"
                                         f"改判持續洩壓或感測器漂移，停用重排。"),
            rationale="同管線其他機台正常，排除總管蒸汽不足；依 SOP 停用該機並重排。",
        )

    # page 2
    def _mill_h1(self, req: CommanderRequest, hid: str) -> CommanderPlan:
        a = req.anomaly
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        mdef = MACHINE_BY_ID[mid]
        limit = CURATIVE_STOP_C if (req.context or {}).get("machine", {}).get("compound_stage") == \
            CompoundStage.CURATIVE_ADDED.value else COMPOUND_STOP_C
        team = TeamId.BIGOPEN if mid == "A-BIGOPEN" else {Line.A: TeamId.BODY, Line.B: TeamId.SIDE,
                                                          Line.C: TeamId.BEAD_NYLON}.get(mdef.line, TeamId.BODY)
        steps = [
            PlanStep(id="S1", tool=ToolName.READ_SENSORS, purpose="讀膠溫與冷卻水流 30 分鐘趨勢",
                     args={"machine_ids": [mid], "minutes": 30.0}),
            PlanStep(id="S2", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="確認對大胎 #07 入硫化時間的影響", args={}),
            PlanStep(id="S3", tool=ToolName.NOTIFY_TEAMS, depends_on=["S1"], purpose="通知維修冷卻水路（自動）",
                     args={"teams": [TeamId.MAINTENANCE.value], "kind": NotifyKind.MAINTENANCE.value,
                           "machine_id": mid, "message": f"{lbl}冷卻水流偏低，請檢修冷卻水路"}),
            PlanStep(id="S4", tool=ToolName.NOTIFY_TEAMS, depends_on=["S1", "S2"],
                     purpose="放慢生料投入（需主管核准）",
                     args={"teams": [team.value], "kind": NotifyKind.SLOW_FEED.value, "machine_id": mid,
                           "message": f"{lbl}膠溫偏高，請放慢生料投入直到冷卻恢復"}),
        ]
        return CommanderPlan(
            goal=f"{lbl}降溫，避免整批膠料提早硬化",
            hypothesis=Hypothesis(
                id=hid, statement="冷卻能力不足（水流偏低）", cause_code="cooling_insufficient",
                status=HypothesisStatus.ACTIVE,
                expectation=Expectation(machine_id=mid, channel=Channel.TEMP, op="<=", value=limit - 2,
                                        within_min=20.0),
                evidence=[f"膠溫 {_fmt(a.value)}°C 且持續上升（上限 {limit:.0f}°C）", "冷卻水流偏低"]),
            steps=steps,
            alert_card=AlertCard(title=f"異常：{lbl} 溫度過高",
                                 summary=f"膠溫 {_fmt(a.value)}°C 且持續上升（上限 {limit:.0f}°C）；原因：冷卻能力不足。"
                                         f"已通知維修，建議放慢生料投入。"),
            rationale="水流偏低與升溫同步，判定冷卻不足；通知維修為 R1 自動，放慢投料為 R3 需核准。",
        )

    def _mill_h2(self, req: CommanderRequest, hid: str, rejected_ids: list[str]) -> CommanderPlan:
        a = req.anomaly
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        steps = [
            PlanStep(id="S1", tool=ToolName.NOTIFY_TEAMS, purpose="暫停投料（需主管核准）",
                     args={"teams": [TeamId.BIGOPEN.value, TeamId.BODY.value], "kind": NotifyKind.PAUSE_FEED.value,
                           "machine_id": mid, "message": f"{lbl}冷卻水路疑似故障，請暫停投料並出料降溫"}),
            PlanStep(id="S2", tool=ToolName.NOTIFY_TEAMS, purpose="維修優先處理冷卻水路",
                     args={"teams": [TeamId.MAINTENANCE.value], "kind": NotifyKind.MAINTENANCE.value,
                           "machine_id": mid, "message": f"{lbl}放慢投料無效，冷卻水路列為最優先檢修"}),
        ]
        return CommanderPlan(
            goal=f"{lbl}降溫，避免整批膠料提早硬化",
            hypothesis=Hypothesis(id=hid, statement="冷卻水路故障，放慢投料不足以降溫", cause_code="cooling_failure",
                                  status=HypothesisStatus.ACTIVE, expectation=None,
                                  evidence=["放慢投料後膠溫仍未回落"]),
            rejected_hypothesis_ids=rejected_ids, steps=steps,
            alert_card=AlertCard(title=f"{lbl}降溫無效，建議暫停投料",
                                 summary="放慢投料後膠溫仍上升；疑冷卻水路故障，建議暫停投料（需核准）。"),
            rationale="H1 處置後未達預期，升級為暫停投料。",
        )

    def _generic(self, req: CommanderRequest, hid: str) -> CommanderPlan:
        a = req.anomaly
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        return CommanderPlan(
            goal=f"確認{lbl}異常原因",
            hypothesis=Hypothesis(id=hid, statement=f"{a.kind.value} 原因待查", cause_code="unknown",
                                  status=HypothesisStatus.ACTIVE, evidence=[a.message]),
            steps=[PlanStep(id="S1", tool=ToolName.READ_SENSORS, purpose="讀 30 分鐘趨勢",
                            args={"machine_ids": [mid], "minutes": 30.0}),
                   PlanStep(id="S2", tool=ToolName.NOTIFY_TEAMS, depends_on=["S1"], purpose="通知維修巡檢",
                            args={"teams": [TeamId.MAINTENANCE.value], "kind": NotifyKind.MAINTENANCE.value,
                                  "machine_id": mid, "message": f"{lbl}：{a.message}，請現場確認"})],
            alert_card=AlertCard(title=f"{lbl}異常", summary=a.message),
            rationale="無對應規則，先通知維修確認。",
        )

    def _sop_path(self, req: CommanderRequest, hid: str, rejected_ids: list[str]) -> CommanderPlan:
        a, ctx = req.anomaly, req.context or {}
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        sop = (ctx.get("sop") or [{}])[0]
        fail = ctx.get("failure") or {}
        title = sop.get("title") or "現場標準作業"
        return CommanderPlan(
            goal=f"{lbl}異常：工具失敗，改走 SOP",
            hypothesis=Hypothesis(id=hid, statement=f"工具 {fail.get('tool', '?')} 失敗，依 SOP「{title}」人工處置",
                                  cause_code="sop_path", status=HypothesisStatus.ACTIVE, expectation=None,
                                  evidence=[str(fail.get("error", ""))[:120]]),
            rejected_hypothesis_ids=rejected_ids,
            steps=[PlanStep(id="S1", tool=ToolName.NOTIFY_TEAMS, purpose="依 SOP 通知現場人工處置",
                            args={"teams": [TeamId.MAINTENANCE.value, TeamId.SUPERVISOR.value],
                                  "kind": NotifyKind.INFO.value, "machine_id": mid,
                                  "message": f"{lbl}：系統工具失敗，請依 SOP「{title}」處置，排程維持原版"})],
            alert_card=AlertCard(title=f"{lbl}：改走 SOP", summary=f"工具重試仍失敗，依 SOP「{title}」人工處置，排程不變。"),
            rationale="工具重試一次仍失敗（page 7）：改走 SOP。",
        )

    def _safe_stop(self, req: CommanderRequest, hid: str, rejected_ids: list[str], why: str) -> CommanderPlan:
        a = req.anomaly
        lbl = machine_label(a.machine_id)
        return CommanderPlan(
            goal=f"{lbl}異常：安全中止",
            hypothesis=Hypothesis(id=hid, statement=f"{why}，交由人工判斷", cause_code="safe_stop",
                                  status=HypothesisStatus.PROPOSED),
            rejected_hypothesis_ids=rejected_ids, steps=[], safe_stop=True,
            alert_card=AlertCard(title=f"{lbl}：安全中止", summary=f"{why}，維持原排程，交由主管判斷。"),
            rationale=why,
        )


def _fmt(v: Any) -> str:
    from warroom.agent.narrate import fmt_num
    return fmt_num(v)


def make_commander(settings: Settings) -> CommanderClient:
    """LLM_MODE=mock -> MockCommander; LLM_MODE=llm -> OpenAICommander (falls back to mock per call
    when settings.llm_fallback_to_mock)."""
    if settings.llm_mode is LLMMode.LLM:
        return OpenAICommander(settings)
    return MockCommander()
