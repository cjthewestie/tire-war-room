"""Event Commander — the ONLY LLM in the system (page 5).

OpenAICommander speaks the OpenAI chat-completions API, so one code path runs on Ollama (Mac),
vLLM (DGX Spark, Nemotron 3.5 Lightning NVFP4) or NVIDIA cloud via BASE_URL / MODEL:

- Constrained output: the CommanderPlan JSON schema as response_format json_schema on every
  server (vLLM enforces it with xgrammar). Reasoning is switched off per server kind.
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
from warroom.agent.strict import TruncatedOutput, failure_cause
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
    CURE_PRESS_IDS,
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
        "- notify_teams {teams: [curing|body|side|bead_nylon|bigopen|bond|maintenance|qc|trim|supervisor], "
        "kind: maintenance|info|slow_feed|pause_feed|staff_move|overtime|machine_out|dispatch, message, machine_id?} "
        "通知各組（寫；maintenance/info 自動，其餘需核准；dispatch=派工，核准後由程式轉成人員任務）",
    ])


def _r1_templates_text() -> str:
    from warroom.agent.gate import R1_TEMPLATES
    return "；".join(f"{k.value}「{t}」" for k, v in R1_TEMPLATES.items() for t in v)


SYSTEM_PROMPT = f"""你是實心胎工廠戰情室的「事件指揮官」，系統中唯一的 LLM。你只輸出一個 JSON 物件（CommanderPlan），不輸出其他文字。

工廠事實：4 條產線、28 台機台、11 台硫化機（每台一顆胎；CURE-08..11 可做中／大胎，CURE-01..07 只做小胎）。硫化 S 24 h / M 28 h / L 34 h，{CURE_TEMP_MIN_C:.0f}–{CURE_TEMP_MAX_C:.0f}°C，標稱 {CURE_TEMP_NOMINAL_C:.0f}°C。蒸汽管線：H1=CURE-01..05、H2=CURE-06..08、H3=CURE-09..11。活化能 {ACTIVATION_ENERGY_KJ_MOL:.0f} kJ/mol（假設值）。
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
7. goal 一句話；purpose 與 rationale 用繁體中文、簡短。
8. maintenance／info 通知（自動 R1）只能用固定句型，{{m}} 為該機台名稱（如「7 號機」「A 線大開煉機」），必須帶 machine_id，只發給 maintenance／qc／supervisor 或該機台所屬組：{_r1_templates_text()}；其他任何文字都會送主管核准。
9. cure_formula 的輸入與 solve_schedule 的 cure_overrides_h 由程式依該硫化機現場模次與公式結果填入；你只需引用 "$S1.total_cure_h"。
10. 若方案讓事件機台停用（machine_out 或 unavailable_presses 含該機），程式以「停用已完成」驗證，expectation 可為 null；其餘方案的 expectation 一律由程式依異常通道定界。
11. phase=replan 且 context 有 supervisor_rejected（主管否決）、supervisor_discussion（主管討論）、write_failed（寫入失敗，如排程已過期）或 infeasible 時，假設並未被推翻：沿用同一假設（同 cause_code），重新求解或改提其他方案。
12. context.source=supervisor 時，這不是異常，是主管下的指令（context.command）：hypothesis.cause_code 填 supervisor_command、statement 寫要完成的事、expectation 為 null；只用工具做得到的部分（通知某組用 notify_teams，message 寫清楚要做什麼、何時、哪台機；排程變更用 solve_schedule 再 confirm_schedule；只想知道狀況用讀取工具）。工具做不到的事（搬動機台、改機台設定、開關閥、任何 PLC 動作）不可假裝能做：steps=[]，rationale 說明做不到的原因與可行的替代做法。
13. context 有 supervisor_discussion 時：主管在核准前提出的條件（constraints，例如某台機某時段保養、不加班、不跨組調人）由程式在每次 solve_schedule 強制套用並檢查結果；你只需保留原計畫的步驟（含 solve_schedule、confirm_schedule 與同一輪的寫入步驟），重新求解。
14. context.source=plant 時，這不是機台異常，是工廠事件（context.plant_event.kind：shift_start 早班開工、rush_order 急單插單、absence 有人請假）：hypothesis.cause_code 填 shift_start／order_rush／staffing_short，expectation 為 null（程式等人員任務完成來驗證）。步驟用 query_orders_shifts 查、solve_schedule 重排、confirm_schedule 確認；早班開工與插單最後加 notify_teams kind=dispatch（派工，需核准）；人力缺口不必寫加班或調人步驟，A/B 方案由求解器的 staffing_options 產生。confirm_schedule 的 schedule_version 一律寫 "$S2.schedule_version"（引用求解步驟），不寫數字；不要用 cure_formula、set_cure_time、read_sensors（沒有機台故障）；machine_id 不填。人員任務一律由程式產生，你不寫任務。"""


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


def build_messages(req: CommanderRequest, plant_text: str | None = None) -> list[dict[str, str]]:
    """System = the fixed core prompt, then the plant section (PLANT.md, warroom/agent/plant.py) after it, so the
    core prefix stays cacheable. plant_text overrides the active version (the 後台 test run of a draft)."""
    from warroom.agent import plant
    extra = plant.section_of(plant_text) if plant_text is not None else plant.section()
    return [{"role": "system", "content": SYSTEM_PROMPT + extra}, {"role": "user", "content": build_user_message(req)}]


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


def split_intercepted(raw: Any) -> list[dict[str, Any]]:
    """Remove (in place) and return the steps of a decoded plan that name a tool outside the
    allow-list. Runs BEFORE schema validation, so an out-of-scope step is caught and audited even
    when the rest of the plan is malformed and the call falls back to the mock (page 7)."""
    intercepted: list[dict[str, Any]] = []
    if isinstance(raw, dict) and isinstance(raw.get("steps"), list):
        allowed = {t.value for t in ToolName}
        keep = []
        for s in raw["steps"]:
            if isinstance(s, dict) and s.get("tool") not in allowed:
                intercepted.append(s)
            else:
                # step-level keys a model sometimes nests inside args: lift them out, never invent them
                if isinstance(s, dict) and isinstance(s.get("args"), dict):
                    for k in ("depends_on", "purpose"):
                        if k in s["args"]:
                            v = s["args"].pop(k)
                            s.setdefault(k, v)
                keep.append(s)
        raw["steps"] = keep
    return intercepted


_TOOL_FIELD = re.compile(r'"tool"\s*:\s*"([^"\\]{1,80})"')


def scan_intercepted(text: str) -> list[dict[str, Any]]:
    """Best effort for output that is not even JSON (truncated, prose around it): every "tool"
    field naming something off the allow-list, so it still reaches the audit log."""
    allowed = {t.value for t in ToolName}
    return [{"tool": name, "partial": True} for name in dict.fromkeys(_TOOL_FIELD.findall(text or ""))
            if name not in allowed]


def parse_plan(text: str, intercepted_out: list[dict[str, Any]] | None = None
               ) -> tuple[CommanderPlan, list[dict[str, Any]]]:
    """Validate model output. Steps naming a tool outside the allow-list are removed and returned
    as `intercepted` (the state machine writes them to the audit log). Raises on anything else;
    pass ``intercepted_out`` to receive the intercepted steps even when validation then raises."""
    raw = json.loads(_strip_fences(text))
    intercepted = split_intercepted(raw)
    if intercepted_out is not None:
        intercepted_out.extend(intercepted)
    plan = CommanderPlan.model_validate(raw)
    return plan.model_copy(update={"alert_card": clamp_alert_card(plan.alert_card)}), intercepted


# =====================================================================================
# OpenAI-compatible commander
# =====================================================================================
def detect_server_kind(base_url: str, probe: bool = True) -> str:
    """'ollama' | 'vllm' | 'openai'. LLM_SERVER_KIND overrides; else ask the server (Ollama answers
    /api/version, vLLM /version); else guess from the URL. A port guess alone is wrong for a
    second Ollama on :11435, so the probe comes first."""
    import os
    forced = os.environ.get("LLM_SERVER_KIND")
    if forced:
        return forced
    url = base_url.rstrip("/")
    root = url[:-3] if url.endswith("/v1") else url
    if "nvidia.com" in url:
        return "openai"
    if probe:
        try:
            import httpx
            with httpx.Client(timeout=1.0) as c:
                if c.get(root + "/api/version").status_code == 200:
                    return "ollama"
                r = c.get(root + "/version")
                if r.status_code == 200 and "version" in r.text:
                    return "vllm"
        except Exception:  # noqa: BLE001 - server down: fall through to the URL guess
            pass
    low = url.lower()
    if "ollama" in low or ":11434" in low or ":11435" in low:
        return "ollama"
    return "vllm"


def thinking_off(kind: str) -> dict[str, Any]:
    """extra_body that turns Nemotron reasoning off (reasoning on: ~39 s and 0% valid plans, bench/results).
    vLLM: chat_template_kwargs.enable_thinking=false (model card). Ollama: think=false alone does not stop
    it when streaming; reasoning_effort='none' does (measured on Ollama 0.33.2)."""
    if kind == "vllm":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    if kind == "ollama":
        return {"think": False, "reasoning_effort": "none"}
    return {}


class OpenAICommander:
    """OpenAI-compatible chat client (Ollama / vLLM / NVIDIA cloud via BASE_URL, MODEL).
    Constrained output = CommanderPlan JSON schema (alert_card first) as response_format json_schema;
    reasoning off via extra_body for the detected server kind (vLLM / Ollama).
    Validates with pydantic; on invalid JSON / timeout falls back to MockCommander when
    settings.llm_fallback_to_mock. ``last_intercepted`` holds steps dropped for naming a tool off
    the allow-list on the latest call."""
    mode = LLMMode.LLM

    def __init__(self, settings: Settings, http_client=None, max_tokens: int | None = None):
        from openai import AsyncOpenAI
        self.settings = settings
        self.model = settings.model
        self.max_tokens = max_tokens or settings.commander_max_tokens
        self.client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key_effective,
                                  timeout=settings.llm_timeout_s, max_retries=0, http_client=http_client)
        self._fallback = MockCommander()
        self.schema = plan_schema()
        self.server_kind = detect_server_kind(settings.base_url, probe=http_client is None)
        self.last_intercepted: list[dict[str, Any]] = []
        self.last_raw: str = ""
        self.last_error: str | None = None     # why the latest call fell back (None = it did not)
        self.plant_override: str | None = None  # 後台 test run: a draft PLANT.md instead of the active one
        self.last_plant_sha: str | None = None  # sha256 of the PLANT.md text in the latest request
        self.last_cause: str | None = None     # failure_cause of that: timeout | truncated | invalid_output | error

    def request_kwargs(self, req: CommanderRequest) -> dict[str, Any]:
        from warroom.agent import plant
        self.last_plant_sha = plant.sha256(self.plant_override) if self.plant_override is not None else plant.active_sha()
        kw: dict[str, Any] = dict(model=self.model, messages=build_messages(req, self.plant_override), temperature=0.0,
                                  max_tokens=self.max_tokens, stream=True,
                                  stream_options={"include_usage": True})
        # response_format json_schema works on vLLM (xgrammar; guided_json was removed in vLLM 0.12),
        # Ollama and NVIDIA cloud alike.
        kw["response_format"] = {"type": "json_schema",
                                 "json_schema": {"name": "CommanderPlan", "schema": self.schema}}
        extra = thinking_off(self.server_kind)
        if extra:
            kw["extra_body"] = extra
        return kw

    async def plan(self, req: CommanderRequest, on_alert: AlertCallback | None = None
                   ) -> tuple[CommanderPlan, LLMCallInfo]:
        t0 = time.perf_counter()
        ttft: float | None = None
        text, usage, finish = "", None, None
        alert_sent = False
        self.last_intercepted, self.last_raw, self.last_error, self.last_cause = [], "", None, None
        caught: list[dict[str, Any]] = []
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
                    finish = getattr(ch, "finish_reason", None) or finish
                if on_alert and not alert_sent:
                    card = extract_alert_card(text)
                    if card is not None:
                        alert_sent = True
                        on_alert(clamp_alert_card(card))
            self.last_raw = text
            if finish == "length":
                raise TruncatedOutput(f"output cut at max_tokens={self.max_tokens} "
                                      f"(completion_tokens={getattr(usage, 'completion_tokens', None)})")
            plan, self.last_intercepted = parse_plan(text, caught)
            info = LLMCallInfo(mode=LLMMode.LLM, model=self.model, base_url=self.settings.base_url, ttft_ms=ttft,
                               total_ms=(time.perf_counter() - t0) * 1000,
                               prompt_tokens=getattr(usage, "prompt_tokens", None),
                               completion_tokens=getattr(usage, "completion_tokens", None), valid_json=True)
            if on_alert and not alert_sent:
                on_alert(plan.alert_card)
            return plan, info
        except Exception as e:
            self.last_raw = text
            self.last_error = f"{type(e).__name__}: {e}"[:500]
            self.last_cause = failure_cause(e)
            # out-of-scope steps in a plan that was then rejected as a whole are still intercepted
            # and logged (page 7); with no parseable JSON, scan the raw text for tool names
            self.last_intercepted = caught or scan_intercepted(text)
            if not self.settings.llm_fallback_to_mock:
                raise
            plan, _ = await self._fallback.plan(req, on_alert=None if alert_sent else on_alert)
            info = LLMCallInfo(mode=LLMMode.LLM, model=self.model, base_url=self.settings.base_url, ttft_ms=ttft,
                               total_ms=(time.perf_counter() - t0) * 1000,
                               prompt_tokens=getattr(usage, "prompt_tokens", None),
                               completion_tokens=getattr(usage, "completion_tokens", None),
                               valid_json=False, fell_back_to_mock=True)
            return plan, info

    async def ping(self) -> bool:
        try:
            await self.client.models.list()
            return True
        except Exception:  # noqa: BLE001
            return False


# =====================================================================================
# Stable cure temperature (page 11 physics input)
# =====================================================================================
def stable_cure_temp(a: Any) -> tuple[float, str | None]:
    """The temperature a press is expected to cure at from now on, for the Arrhenius formula: the detector's
    prediction (page 11: 128 -> 122 °C, 預測穩定在 122±1), never below the cure band's floor.

    Why the floor (team dataset EV-2 replay, issue #17): the detector can fire while the curve is still falling
    (7 號 128 -> 126 °C at -0.89 °C/min); its 30-min forecast is then a straight-line extrapolation (99.8 °C), not a
    level the press settles at. Curing at 99.8 °C would need 131.6 h, past the 96 h cap, and the event would
    safe-stop instead of following page 11. Below CURE_TEMP_MIN_C (120 °C) the safety core holds the press anyway
    (扣模), so no cure plan runs under it: the floor is the coldest temperature a cure plan can mean. Returns
    (value, why it was floored or None). A prediction at or above the floor is used unchanged (page 11 identical)."""
    pred = getattr(a, "prediction", None)
    v = float(pred.predicted_value) if pred is not None else float(a.value)
    if v < CURE_TEMP_MIN_C:
        return float(CURE_TEMP_MIN_C), (f"預測 30 分後 {_fmt(v)}°C 為仍在下降的直線外推；"
                                        f"取硫化下限 {CURE_TEMP_MIN_C:.0f}°C（低於下限安全核心直接扣模）")
    return v, None


# =====================================================================================
# Deterministic mock commander
# =====================================================================================
def _round_half(x: float) -> float:
    return round(x * 2) / 2


class MockCommander:
    """Deterministic rule commander. Reproduces page 11: plan phase -> H1 steam_pressure_low,
    expectation <press> temp_c >= 121 within 30 min, steps S1 cure_formula, S2 query_orders_shifts,
    S3 solve_schedule(dep S1,S2), S4 confirm_schedule with S4b set_cure_time ($S1.total_cure_h) under
    the same approval, S5 notify_teams (pages 7/11 decomposition S1..S5); replan with H1 rejected ->
    H2 steam_leak_or_sensor_drift (header neighbours compared, SOP 「硫化中斷處置」 in context),
    press out of service, reschedule. Page 2: cooling_insufficient when the water flow really is low
    (else overheat_unknown), notify maintenance (R1, fixed template) + slow_feed (R3). A replan after a
    supervisor reject, a failed write or an infeasible solve keeps the current hypothesis (re-solve;
    after a reject without cross-team moves, or without the feed change on the mill); only a failed
    expectation swaps it. Third miss -> safe_stop (keep the original schedule)."""
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
        if a.kind is AnomalyKind.SUPERVISOR_COMMAND:          # issue #10: a task the supervisor typed
            return self._command(req, hid)
        if a.kind in (AnomalyKind.SHIFT_START, AnomalyKind.ORDER_RUSH, AnomalyKind.STAFFING_SHORT):   # issue #17
            return self._plant(req, hid)
        if ctx.get("sop_path"):
            return self._sop_path(req, hid, rejected_ids)
        if len(req.rejected) >= 2:
            return self._safe_stop(req, hid, rejected_ids, "連續兩個假設不成立")
        if a.kind is AnomalyKind.SCHEDULE_SLIP:      # page 6 box 4: plan vs actual
            if req.phase == "replan":
                return self._safe_stop(req, hid, rejected_ids, "重排未成立，維持原排程")
            return self._schedule_slip(req, hid)
        # A replan only swaps the hypothesis when VERIFY rejected it (failed_expectation). A supervisor
        # reject, a failed write (stale schedule) or an infeasible solve leaves the current hypothesis
        # standing: re-plan it (re-solve against the schedule now active, or offer another option).
        current = (ctx.get("current_hypothesis") or {}).get("cause_code")
        redo = req.phase == "replan" and not ctx.get("failed_expectation") and current and current not in rejected_codes
        rejected_by_sup = bool(ctx.get("supervisor_rejected"))
        if mdef is not None and mdef.line is Line.CURE:
            if req.phase == "plan" and "steam_pressure_low" not in rejected_codes:
                return self._press_h1(req, hid)
            if redo and current == "steam_pressure_low":
                return self._press_h1(req, hid, no_cross_team=rejected_by_sup)
            if redo and current == "steam_leak_or_sensor_drift":
                return self._press_h2(req, hid, rejected_ids, no_cross_team=rejected_by_sup)
            if "steam_leak_or_sensor_drift" not in rejected_codes:
                return self._press_h2(req, hid, rejected_ids)
            return self._safe_stop(req, hid, rejected_ids, "無可用替代假設")
        if mdef is not None and (mdef.handles_compound or a.kind in (AnomalyKind.OVERHEAT,
                                                                      AnomalyKind.WATER_FLOW_LOW)):
            first = self._mill_h1(req, hid)
            if req.phase == "plan" and first.hypothesis.cause_code not in rejected_codes:
                return first
            if redo and current == first.hypothesis.cause_code:
                return self._mill_h1(req, hid, observe_only=rejected_by_sup)
            second = self._mill_h2(req, hid, rejected_ids)
            if redo and current == second.hypothesis.cause_code:
                return self._mill_h2(req, hid, rejected_ids, observe_only=rejected_by_sup)
            if second.hypothesis.cause_code not in rejected_codes:
                return second
            return self._safe_stop(req, hid, rejected_ids, "無可用替代假設")
        if req.phase == "replan":
            if redo and current == "unknown" and not rejected_by_sup:
                return self._generic(req, hid)
            return self._safe_stop(req, hid, rejected_ids, "無可用替代假設")
        return self._generic(req, hid)

    # page 11, first round
    def _press_h1(self, req: CommanderRequest, hid: str, no_cross_team: bool = False) -> CommanderPlan:
        a, ctx = req.anomaly, req.context or {}
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        job = ctx.get("cure_job") or {}
        size = TireSize(job.get("size", TireSize.M.value))
        ref = float(job.get("setpoint_c") or CURE_TEMP_NOMINAL_C)
        elapsed = float(job.get("cure_elapsed_h", 10.0))
        stable, floored = stable_cure_temp(a)
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
                     args={"cure_overrides_h": {mid: "$S1.total_cure_h"}, "time_limit_s": 10.0,
                           **({"allow_cross_team": False} if no_cross_team else {})}),
            PlanStep(id="S4", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S3"], purpose="確認新排程（需主管核准）",
                     args={"schedule_version": "$S3.schedule_version",
                           "reason": f"{lbl}降溫，延長硫化至 ${{S1.total_cure_h}} h 後重排"}),
            # page 5 write tool 設定硫化時間（建議值）: the formula's value, gated R2. It is part of S4's
            # approval (pages 7/11: S4 確認（需核准）→ S5 通知), hence the sub-step id S4b
            PlanStep(id="S4b", tool=ToolName.SET_CURE_TIME, depends_on=["S1", "S3"],
                     purpose=f"設定{lbl}硫化時間為公式值（需主管核准）",
                     args={"press_id": mid, "total_cure_h": "$S1.total_cure_h",
                           "reason": f"{lbl}降溫至 {_fmt(actual)}°C，Arrhenius 等效硫化"}),
            PlanStep(id="S5", tool=ToolName.NOTIFY_TEAMS, depends_on=["S4", "S4b"], purpose="通知硫化組與胎體組新排程",
                     args={"teams": [TeamId.CURING.value, TeamId.BODY.value], "kind": NotifyKind.INFO.value,
                           "machine_id": mid,
                           "message": f"{lbl}延長硫化至 ${{S1.total_cure_h}} h（+${{S1.extra_h}} h），已依新排程調整下一模"}),
        ]
        stable_txt = f"{_fmt(stable)}±{_fmt(band)}°C"
        if floored:
            stable_txt = f"{_fmt(stable)}°C（下限）"
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
    def _press_h2(self, req: CommanderRequest, hid: str, rejected_ids: list[str],
                  no_cross_team: bool = False) -> CommanderPlan:
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
                     args={"unavailable_presses": [mid], "time_limit_s": 10.0,
                           **({"allow_cross_team": False} if no_cross_team else {})}),
            PlanStep(id="S2", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S1"], purpose="確認重排（需主管核准）",
                     args={"schedule_version": "$S1.schedule_version",
                           "reason": f"{lbl}持續降溫，停用重排"}),
            PlanStep(id="S3", tool=ToolName.NOTIFY_TEAMS, depends_on=["S2"], purpose=f"通知{lbl}停用",
                     args={"teams": [TeamId.CURING.value, TeamId.MAINTENANCE.value, TeamId.QC.value],
                           "kind": NotifyKind.MACHINE_OUT.value, "machine_id": mid,
                           "message": f"{lbl}停用：疑持續洩壓或感測器漂移，請維修檢查蒸汽管路與溫度計；在模胎交品管"}),
        ]
        last = rejected_ids[-1] if rejected_ids else "H1"
        summary = (f"{last} 不成立：溫度續降至 {_fmt(observed)}°C；改判持續洩壓或感測器漂移，停用重排。"
                   if observed is not None else "持續洩壓或感測器漂移：停用並重排。")
        return CommanderPlan(
            goal=f"{machine_label(mid, short=True)}降溫，大胎不能延誤",
            hypothesis=Hypothesis(id=hid, statement="持續洩壓或感測器漂移", cause_code="steam_leak_or_sensor_drift",
                                  status=HypothesisStatus.ACTIVE, expectation=None, evidence=evidence),
            rejected_hypothesis_ids=rejected_ids,
            steps=steps,
            alert_card=AlertCard(title=f"{lbl}停用，重排中", summary=summary),
            rationale="同管線其他機台正常，排除總管蒸汽不足；依 SOP 停用該機並重排。",
        )

    @staticmethod
    def _flow_low(req: CommanderRequest) -> bool:
        """Is the cooling water flow actually low? Only from the anomaly or the readings (now vs the start
        of the 30-min window, the same 85 % line the dashboard tile uses), never assumed."""
        a, ctx = req.anomaly, req.context or {}
        if a.kind is AnomalyKind.WATER_FLOW_LOW or a.channel is Channel.WATER_FLOW:
            return True
        now = ((ctx.get("sensors") or {}).get(a.machine_id) or {}).get("water_flow_lpm_now")
        base = ((ctx.get("sensor_baseline") or {}).get(a.machine_id) or {}).get(Channel.WATER_FLOW.value)
        try:
            return now is not None and base is not None and float(base) > 0 and float(now) < 0.85 * float(base)
        except (TypeError, ValueError):
            return False

    # page 2
    def _mill_h1(self, req: CommanderRequest, hid: str, observe_only: bool = False) -> CommanderPlan:
        a = req.anomaly
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        mdef = MACHINE_BY_ID[mid]
        limit = CURATIVE_STOP_C if (req.context or {}).get("machine", {}).get("compound_stage") == \
            CompoundStage.CURATIVE_ADDED.value else COMPOUND_STOP_C
        team = TeamId.BIGOPEN if mid == "A-BIGOPEN" else {Line.A: TeamId.BODY, Line.B: TeamId.SIDE,
                                                          Line.C: TeamId.BEAD_NYLON}.get(mdef.line, TeamId.BODY)
        flow_low = self._flow_low(req)
        if flow_low:
            statement, code, cause = "冷卻能力不足（水流偏低）", "cooling_insufficient", "冷卻能力不足"
            notice, why = f"{lbl}冷卻水流偏低，請檢修冷卻水路", "冷卻水流偏低"
        else:
            statement, code, cause = "膠溫過高，冷卻水流正常，原因待查", "overheat_unknown", "原因待查（水流正常）"
            notice, why = f"{lbl}膠溫偏高，請檢查冷卻系統", "冷卻水流正常"
        steps = [
            PlanStep(id="S1", tool=ToolName.READ_SENSORS, purpose="讀膠溫與冷卻水流 30 分鐘趨勢",
                     args={"machine_ids": [mid], "minutes": 30.0}),
        ]
        if not observe_only:
            steps += [
                PlanStep(id="S2", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="確認對大胎 #07 入硫化時間的影響", args={}),
                PlanStep(id="S3", tool=ToolName.NOTIFY_TEAMS, depends_on=["S1"],
                         purpose="通知維修冷卻水路（自動）" if flow_low else "通知維修檢查冷卻系統（自動）",
                         args={"teams": [TeamId.MAINTENANCE.value], "kind": NotifyKind.MAINTENANCE.value,
                               "machine_id": mid, "message": notice}),
                PlanStep(id="S4", tool=ToolName.NOTIFY_TEAMS, depends_on=["S1", "S2"],
                         purpose="放慢生料投入（需主管核准）",
                         args={"teams": [team.value], "kind": NotifyKind.SLOW_FEED.value, "machine_id": mid,
                               "message": f"{lbl}膠溫偏高，請放慢生料投入直到冷卻恢復"}),
            ]
        summary = (f"膠溫 {_fmt(a.value)}°C 且持續上升（上限 {limit:.0f}°C）；原因：{cause}。"
                   + ("主管否決放慢投料，持續監看膠溫。" if observe_only else "已通知維修，建議放慢生料投入。"))
        return CommanderPlan(
            goal=f"{lbl}降溫，避免整批膠料提早硬化",
            hypothesis=Hypothesis(
                id=hid, statement=statement, cause_code=code, status=HypothesisStatus.ACTIVE,
                expectation=Expectation(machine_id=mid, channel=Channel.TEMP, op="<=", value=limit - 2,
                                        within_min=20.0),
                evidence=[f"膠溫 {_fmt(a.value)}°C 且持續上升（上限 {limit:.0f}°C）", why]),
            steps=steps,
            alert_card=AlertCard(title=f"異常：{lbl} 溫度過高", summary=summary),
            rationale=("主管否決放慢投料：不再變更投料，只持續監看。" if observe_only else
                       ("水流偏低與升溫同步，判定冷卻不足" if flow_low else "水流正常，冷卻水路不是已知原因")
                       + "；通知維修為 R1 自動，放慢投料為 R3 需核准。"),
        )

    def _mill_h2(self, req: CommanderRequest, hid: str, rejected_ids: list[str],
                 observe_only: bool = False) -> CommanderPlan:
        a = req.anomaly
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        flow_low = self._flow_low(req)
        steps = [] if observe_only else [
            PlanStep(id="S1", tool=ToolName.NOTIFY_TEAMS, purpose="暫停投料（需主管核准）",
                     args={"teams": [TeamId.BIGOPEN.value, TeamId.BODY.value], "kind": NotifyKind.PAUSE_FEED.value,
                           "machine_id": mid, "message": f"{lbl}膠溫未回落，請暫停投料並出料降溫"}),
            PlanStep(id="S2", tool=ToolName.NOTIFY_TEAMS, purpose="維修優先處理冷卻水路",
                     args={"teams": [TeamId.MAINTENANCE.value], "kind": NotifyKind.MAINTENANCE.value,
                           "machine_id": mid, "message": f"{lbl}膠溫未回落，冷卻水路列為最優先檢修"}),
        ]
        statement, code = (("冷卻水路故障，放慢投料不足以降溫", "cooling_failure") if flow_low else
                           ("持續過熱，放慢投料不足以降溫", "overheat_persistent"))
        return CommanderPlan(
            goal=f"{lbl}降溫，避免整批膠料提早硬化",
            hypothesis=Hypothesis(id=hid, statement=statement, cause_code=code,
                                  status=HypothesisStatus.ACTIVE, expectation=None,
                                  evidence=["放慢投料後膠溫仍未回落"]),
            rejected_hypothesis_ids=rejected_ids, steps=steps,
            alert_card=AlertCard(title=f"{lbl}降溫無效，建議暫停投料",
                                 summary=("放慢投料後膠溫仍上升；" + ("疑冷卻水路故障，" if flow_low else "")
                                          + ("主管否決暫停投料，持續監看。" if observe_only else "建議暫停投料（需核准）。"))),
            rationale="H1 處置後未達預期，升級為暫停投料。",
        )

    # page 6 box 4: a late de-mold / mold-in or WIP behind T -> re-solve from the actual state
    def _schedule_slip(self, req: CommanderRequest, hid: str) -> CommanderPlan:
        a = req.anomaly
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        status = ((req.context or {}).get("machine") or {}).get("status")
        if mid in CURE_PRESS_IDS and status in ("stopped", "out_of_service"):
            return self._held_press_slip(req, hid, status)
        steps = [
            PlanStep(id="S1", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="查受影響訂單交期與班表缺口",
                     args={"press_ids": [mid]} if mid in CURE_PRESS_IDS else {}),
            PlanStep(id="S2", tool=ToolName.SOLVE_SCHEDULE, depends_on=["S1"],
                     purpose="以實際進度重排，保交期", args={"time_limit_s": 10.0}),
            PlanStep(id="S3", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S2"], purpose="確認新排程（需主管核准）",
                     args={"schedule_version": "$S2.schedule_version",
                           "reason": f"{lbl}排程延誤 {_fmt(a.value)} 分，依實際進度重排"}),
            PlanStep(id="S4", tool=ToolName.NOTIFY_TEAMS, depends_on=["S3"], purpose="通知相關組別新排程",
                     args={"teams": [TeamId.CURING.value, TeamId.BOND.value, TeamId.BODY.value],
                           "kind": NotifyKind.INFO.value, "machine_id": mid,
                           "message": f"{lbl}排程延誤，已依實際進度重排，請依新排程作業"}),
        ]
        return CommanderPlan(
            goal=f"{lbl}排程延誤 {_fmt(a.value)} 分，交期不能延誤",
            hypothesis=Hypothesis(id=hid, statement="作業落後計畫，依實際進度重排可保交期",
                                  cause_code="schedule_slip", status=HypothesisStatus.ACTIVE, evidence=[a.message]),
            steps=steps,
            alert_card=AlertCard(title=f"{lbl}排程延誤 {_fmt(a.value)} 分", summary=a.message[:80]),
            rationale="計畫與實際不符（每分鐘比對）；排程數字由求解器產生，新排程需核准。",
        )

    def _held_press_slip(self, req: CommanderRequest, hid: str, status: str) -> CommanderPlan:
        """The plan-vs-actual slip is on a press the safety core holds or a decision took out: the
        cause is that hold, not people running late. Re-solve without the press so nothing new is
        loaded on it."""
        a = req.anomaly
        mid, lbl = a.machine_id, machine_label(a.machine_id)
        held = "已停用" if status == "out_of_service" else "安全核心扣模／停機中"
        steps = [
            PlanStep(id="S1", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="查受影響訂單交期與班表缺口",
                     args={"press_ids": [mid]}),
            PlanStep(id="S2", tool=ToolName.SOLVE_SCHEDULE, depends_on=["S1"],
                     purpose=f"{lbl}{held}，排除該機重排", args={"unavailable_presses": [mid], "time_limit_s": 10.0}),
            PlanStep(id="S3", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S2"], purpose="確認新排程（需主管核准）",
                     args={"schedule_version": "$S2.schedule_version",
                           "reason": f"{lbl}{held}，排除該機重排"}),
            PlanStep(id="S4", tool=ToolName.NOTIFY_TEAMS, depends_on=["S3"], purpose="通知相關組別新排程",
                     args={"teams": [TeamId.CURING.value, TeamId.BOND.value, TeamId.BODY.value],
                           "kind": NotifyKind.INFO.value, "machine_id": mid,
                           "message": f"{lbl}{held}，已排除該機重排，請依新排程作業"}),
        ]
        return CommanderPlan(
            goal=f"{lbl}{held}，交期不能延誤",
            hypothesis=Hypothesis(id=hid, statement=f"{lbl}{held}（非作業落後），排除該機重排",
                                  cause_code="safety_hold", status=HypothesisStatus.ACTIVE, evidence=[a.message]),
            steps=steps,
            alert_card=AlertCard(title=f"{lbl}{held}，排程需調整", summary=a.message[:80]),
            rationale="計畫與實際不符的原因是機台被扣模／停用，不是作業落後；排除該機由求解器重排。",
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
                                  "machine_id": mid, "message": f"{lbl}異常，請現場確認"})],
            alert_card=AlertCard(title=f"{lbl}異常", summary=a.message),
            rationale="無對應規則，先通知維修確認。",
        )

    def _command(self, req: CommanderRequest, hid: str) -> CommanderPlan:
        """Issue #10, rule mode: map the supervisor's command onto the tools (notify / re-solve /
        read). Free-text notices are not R1 templates, so the gate sends them to the supervisor.
        Anything else gets no step and says why (never an invented action)."""
        from warroom.agent.talk import command_kind, command_teams
        a, ctx = req.anomaly, req.context or {}
        text = str(ctx.get("command") or a.message)
        mid = a.machine_id if a.machine_id in MACHINE_BY_ID else None
        lbl = machine_label(mid) if mid else "全廠"
        kind = command_kind(text)
        why = ""
        if req.phase == "replan" and (ctx.get("supervisor_rejected") or ctx.get("sop_path")):
            kind, why = "stop", ("主管否決，不執行這個指令" if ctx.get("supervisor_rejected") else "工具失敗，不執行，請人工處理")
        steps: list[PlanStep] = []
        if kind == "notify":
            teams = command_teams(text) or [TeamId.MAINTENANCE]
            if mid:
                steps.append(PlanStep(id="S1", tool=ToolName.READ_SENSORS, purpose=f"先讀{lbl}現況",
                                      args={"machine_ids": [mid], "minutes": 30.0}))
            nk = NotifyKind.MAINTENANCE if teams == [TeamId.MAINTENANCE] else NotifyKind.INFO
            steps.append(PlanStep(id=f"S{len(steps) + 1}", tool=ToolName.NOTIFY_TEAMS,
                                  depends_on=[s.id for s in steps], purpose="依主管指令通知（需主管核准）",
                                  args={"teams": [t.value for t in teams], "kind": nk.value,
                                        **({"machine_id": mid} if mid else {}), "message": f"主管指示：{text}"}))
            why = "主管指令：通知相關組別；自由文字通知需主管核准"
        elif kind == "schedule":
            steps = [
                PlanStep(id="S1", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="查受影響訂單與班表",
                         args={"press_ids": [mid]} if mid in CURE_PRESS_IDS else {}),
                PlanStep(id="S2", tool=ToolName.SOLVE_SCHEDULE, depends_on=["S1"], purpose="依主管條件重排",
                         args={"time_limit_s": 10.0}),
                PlanStep(id="S3", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S2"], purpose="確認新排程（需主管核准）",
                         args={"schedule_version": "$S2.schedule_version", "reason": f"主管指令：{text}"[:120]}),
            ]
            why = "主管指令：排程條件由程式套用，求解器重排，新排程需核准"
        elif kind == "query":
            steps = [PlanStep(id="S1", tool=ToolName.READ_SENSORS, purpose=f"讀{lbl}現況",
                              args={"machine_ids": [mid], "minutes": 30.0})] if mid else \
                [PlanStep(id="S1", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="查訂單與班表", args={})]
            why = "主管指令：只需查詢，不變更"
        elif kind != "stop":
            why = "規則模式無法把這個指令對應到工具（通知某組、依條件重排、查詢）；請改用這些說法，或交由現場處理"
        return CommanderPlan(
            goal=f"完成主管指令：{text}"[:80],
            hypothesis=Hypothesis(id=hid, statement=f"主管指令：{text}"[:120], cause_code="supervisor_command",
                                  status=HypothesisStatus.ACTIVE, expectation=None, evidence=[]),
            steps=steps,
            alert_card=AlertCard(title=f"主管指令：{text}"[:20], summary=why[:60] or text[:60]),
            rationale=why,
        )

    def _plant(self, req: CommanderRequest, hid: str) -> CommanderPlan:
        """Issue #17 plant events, rule mode. The schedule numbers come from the solver; the A/B staffing options
        from the solver's staffing_options; the worker tasks from code after approval (派工). A supervisor reject or
        a tool failure ends with no step (the plan in force stays)."""
        a, ctx = req.anomaly, req.context or {}
        pe = ctx.get("plant_event") or {}
        all_teams = [TeamId.BODY.value, TeamId.BIGOPEN.value, TeamId.SIDE.value, TeamId.BEAD_NYLON.value,
                     TeamId.BOND.value, TeamId.CURING.value]
        if req.phase == "replan" and (ctx.get("supervisor_rejected") or ctx.get("sop_path")):
            why = "主管否決：維持現行排程，不派工" if ctx.get("supervisor_rejected") else "工具失敗：維持現行排程，交主管人工處理"
            return CommanderPlan(goal=f"{a.message}"[:80],
                                 hypothesis=Hypothesis(id=hid, statement=why, cause_code=a.kind.value,
                                                       status=HypothesisStatus.ACTIVE),
                                 steps=[], alert_card=AlertCard(title=why[:20], summary=a.message[:60]), rationale=why)
        if a.kind is AnomalyKind.SHIFT_START:
            steps = [
                PlanStep(id="S1", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="查今日訂單、班表、出勤與原料", args={}),
                PlanStep(id="S2", tool=ToolName.SOLVE_SCHEDULE, depends_on=["S1"], purpose="依今日訂單與出勤排程",
                         args={"time_limit_s": 10.0}),
                PlanStep(id="S3", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S2"], purpose="確認今日排程（需主管核准）",
                         args={"schedule_version": "$S2.schedule_version", "reason": "早班開工：今日計畫"}),
                PlanStep(id="S4", tool=ToolName.NOTIFY_TEAMS, depends_on=["S1"], purpose="派工：今日計畫（需主管核准）",
                         args={"teams": all_teams, "kind": NotifyKind.DISPATCH.value,
                               "message": "今日計畫：依 SOP 暖機（密煉 → 開煉），首批膠料補到 A 線，依排程開工"}),
            ]
            return CommanderPlan(
                goal="早班開工：今日訂單準時開工", steps=steps,
                hypothesis=Hypothesis(id=hid, statement="依今日訂單與出勤開工，暖機與補料完成後即可依排程生產",
                                      cause_code="shift_start", status=HypothesisStatus.ACTIVE, evidence=[a.message]),
                alert_card=AlertCard(title="早班開工：今日計畫", summary="已查訂單、出勤、暖機順序與原料；今日計畫待核准。"),
                rationale="開班例行：讀取今日狀況，排程由求解器產生，派工需主管核准，任務由程式派給現場。")
        if a.kind is AnomalyKind.ORDER_RUSH:
            oid = str(((pe.get("order") or {}).get("id")) or "急單")
            steps = [
                PlanStep(id="S1", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose=f"查 {oid} 與現有訂單交期", args={}),
                PlanStep(id="S2", tool=ToolName.SOLVE_SCHEDULE, depends_on=["S1"], purpose=f"插入 {oid} 重排，看誰延後",
                         args={"time_limit_s": 10.0}),
                PlanStep(id="S3", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S2"], purpose="確認插單排程（需主管核准）",
                         args={"schedule_version": "$S2.schedule_version", "reason": f"插單 {oid}"}),
                PlanStep(id="S4", tool=ToolName.NOTIFY_TEAMS, depends_on=["S3"], purpose=f"派工：{oid} 補料、備模（需主管核准）",
                         args={"teams": [TeamId.BODY.value, TeamId.CURING.value], "kind": NotifyKind.DISPATCH.value,
                               "message": f"急單 {oid} 插單：依新排程補料、備模"}),
            ]
            return CommanderPlan(
                goal=f"插單 {oid}：排入急單，看清誰延後、誰誤交期", steps=steps,
                hypothesis=Hypothesis(id=hid, statement=f"{oid} 可插入現有排程，其他訂單仍在交期內",
                                      cause_code="order_rush", status=HypothesisStatus.ACTIVE, evidence=[a.message]),
                alert_card=AlertCard(title=f"插單：{oid}", summary=f"{a.message[:40]}；重排看誰延後。"),
                rationale="急單加入訂單簿；求解器重排並列出延後的訂單，主管核准後才生效。")
        steps = [
            PlanStep(id="S1", tool=ToolName.QUERY_ORDERS_SHIFTS, purpose="查班表缺口與出勤", args={}),
            PlanStep(id="S2", tool=ToolName.SOLVE_SCHEDULE, depends_on=["S1"], purpose="依缺人重排，提出補人方案",
                     args={"time_limit_s": 10.0}),
            PlanStep(id="S3", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S2"], purpose="確認排程與補人方案（需主管核准）",
                     args={"schedule_version": "$S2.schedule_version", "reason": f"人力缺口：{a.message}"[:120]}),
        ]
        return CommanderPlan(
            goal="人力缺口：出入模不中斷", steps=steps,
            hypothesis=Hypothesis(id=hid, statement="缺 1 人可由加班或跨組支援補上，排程不變", cause_code="staffing_short",
                                  status=HypothesisStatus.ACTIVE, evidence=[a.message]),
            alert_card=AlertCard(title="人力缺口", summary=f"{a.message[:40]}；求解器提出補人方案。"),
            rationale="缺人由求解器找出受影響的班別並提出加班／調人方案（A/B），主管選擇後派工。")

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
                                  "message": f"{lbl}：系統工具失敗，請依 SOP 人工處置，排程維持原版"})],
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


# =====================================================================================
# Few-shot examples for the real model (static text: same bytes every call, so prefix caching works)
# =====================================================================================
def _example_plans() -> str:
    """Two worked plans written by the rule commander on synthetic anomalies (press 9 temperature
    drop, B-line open mill overheat). They show the standard procedure — formula, orders, solver,
    approval-gated writes, "$S" references, expectation from the prediction band — not the numbers."""
    from datetime import datetime

    from warroom.contracts import Anomaly, AnomalyKind, Prediction, Severity
    from warroom.factory import TZ
    ts = datetime(2026, 1, 5, 3, 0, tzinfo=TZ)
    press = Anomaly(id="AN-EX1", ts=ts, machine_id="CURE-09", kind=AnomalyKind.TEMP_DROP, channel=Channel.TEMP,
                    value=124.0, baseline=127.0, severity=Severity.WARN, message="9 號 127 → 124°C，預測穩定在 124±1°C",
                    prediction=Prediction(machine_id="CURE-09", channel=Channel.TEMP, now_value=124.0,
                                          predicted_value=124.0, band_lo=123.0, band_hi=125.0, horizon_s=1800,
                                          trend_per_min=0.0))
    mill = Anomaly(id="AN-EX2", ts=ts, machine_id="B-OPEN", kind=AnomalyKind.OVERHEAT, channel=Channel.TEMP,
                   value=104.0, baseline=95.0, severity=Severity.CRITICAL, message="B 線開煉機 膠溫 104°C 持續上升，水流偏低")
    m = MockCommander()
    out = []
    for an, ctx in ((press, {"cure_job": {"size": "L", "setpoint_c": 127, "cure_elapsed_h": 6}}),
                    (mill, {"machine": {"compound_stage": "unvulcanized"}})):
        req = CommanderRequest(phase="plan", event_id="EX", anomaly=an, allowed_tools=list(ToolName), context=ctx)
        d = m.build(req).model_dump(mode="json", exclude_none=True)
        d = {"alert_card": d.pop("alert_card"), **d}
        out.append(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    first = m.build(CommanderRequest(phase="plan", event_id="EX", anomaly=press, allowed_tools=list(ToolName),
                                     context={"cure_job": {"size": "L", "setpoint_c": 127, "cure_elapsed_h": 6}}))
    h1 = first.hypothesis.model_copy(update={"status": HypothesisStatus.REJECTED})
    rep = m.build(CommanderRequest(phase="replan", event_id="EX", anomaly=press, allowed_tools=list(ToolName),
                                   rejected=[h1],
                                   context={"failed_expectation": {"observed": 121.5},
                                            "neighbours": {"CURE-10": {"temp_c_now": 127.0},
                                                           "CURE-11": {"temp_c_now": 127.1}},
                                            "sop": [{"title": "硫化中斷處置"}]}))
    d = rep.model_dump(mode="json", exclude_none=True)
    out.append(json.dumps({"alert_card": d.pop("alert_card"), **d}, ensure_ascii=False, separators=(",", ":")))
    return ("\n\n範例（格式與步驟示範；數值一律以本次事件與工具結果為準）：\n"
            "硫化機降溫（expectation 下限取預測區間 band_lo；先公式、再查訂單班表、再求解排程，寫入步驟需核准）：\n"
            f"{out[0]}\n開煉機過熱（通知維修為自動，放慢投料需核准）：\n{out[1]}\n"
            "replan：H1 已被程式驗證推翻（同管線其他機台正常），改提 H2、停用該機並重排：\n"
            f"{out[2]}")


SYSTEM_PROMPT = SYSTEM_PROMPT + _example_plans()
