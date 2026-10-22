"""後台 系統參數 and 系統健康: the live operating parameters (read at request time from settings, module constants and
the running objects, never copied into text) and the status turned into plain-language health cards.

Every parameter row says where its value comes from (file:line, or the env var behind a setting). A value that
does not exist on this build reads 「未啟用」 rather than a guess. Nothing here can change a value: runtime=False."""
from __future__ import annotations

import functools
import os
import re
import statistics
import subprocess
from pathlib import Path
from typing import Any

from warroom.config import ROOT

MISSING = object()


# ---------------------------------------------------------------------------------------------- sources
@functools.lru_cache(maxsize=256)
def _line(rel: str, pattern: str) -> str:
    """'rel:line' of the first line matching pattern (a regex), or just rel."""
    try:
        rx = re.compile(pattern)
        for i, ln in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(ln):
                return f"{rel}:{i}"
    except OSError:
        pass
    return rel


def _const_src(rel: str, name: str) -> str:
    return _line(rel, rf"^\s*{re.escape(name)}\s*(:[^=]*)?=")


def _setting_src(field: str) -> str:
    return f"env {field.upper()}（{_line('warroom/config.py', rf'^    {re.escape(field)}\s*:')}）"


def _row(key: str, value: Any, meaning: str, source: str) -> dict[str, Any]:
    on = value is not MISSING
    return {"key": key, "value": value if on else None, "enabled": on, "meaning": meaning, "source": source,
            "runtime": False}


def _mod(name: str) -> Any:
    import importlib
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - a module missing on this build: its rows read 未啟用
        return None


def _const(modname: str, attr: str) -> Any:
    m = _mod(modname)
    return getattr(m, attr, MISSING) if m is not None else MISSING


def _c(modname: str, attr: str, meaning: str, key: str | None = None) -> dict[str, Any]:
    rel = modname.replace(".", "/") + ".py"
    return _row(key or attr, _const(modname, attr), meaning, _const_src(rel, attr))


def _s(settings: Any, field: str, meaning: str) -> dict[str, Any]:
    return _row(field, getattr(settings, field, MISSING), meaning, _setting_src(field))


def _live(obj: Any, modname: str, cls: str, attr: str, key: str, meaning: str, source: str) -> dict[str, Any]:
    """The running object's value; when the running part is a fallback without it, the real class's default
    (read from its signature, never typed here), marked as such."""
    if obj is not None and hasattr(obj, attr):
        return _row(key, getattr(obj, attr), meaning, source)
    import inspect
    c = _const(modname, cls)
    try:
        d = inspect.signature(c).parameters[attr].default if c is not MISSING else MISSING
    except (KeyError, TypeError, ValueError):
        d = MISSING
    if d is inspect.Parameter.empty:
        d = MISSING
    return _row(key, d, meaning + ("（元件目前是備援，顯示程式預設值）" if d is not MISSING else ""), source)


def _plain(v: Any) -> Any:
    """JSON-safe: enums to their value, sets to sorted lists, paths to strings."""
    from enum import Enum
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, dict):
        return {str(_plain(k)): _plain(x) for k, x in v.items()}
    if isinstance(v, (set, frozenset)):
        return sorted(_plain(x) for x in v)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, Path):
        return str(v)
    return v


# ---------------------------------------------------------------------------------------------- 系統參數
def params(r: Any, app_state: Any = None) -> dict[str, Any]:
    s = r.settings
    sm = getattr(r, "state_machine", None)
    safety = getattr(getattr(r, "c", None), "safety", None)
    groups: list[dict[str, Any]] = []

    def g(gid: str, label: str, rows: list[dict[str, Any]]) -> None:
        groups.append({"id": gid, "label": label, "rows": [{**x, "value": _plain(x["value"])} for x in rows]})

    g("model", "模型", [
        _s(s, "llm_mode", "mock＝規則模式不呼叫模型；llm＝呼叫本地模型"),
        _s(s, "model", "呼叫的模型名稱"),
        _row("base_url", s.base_url, "模型伺服器位址（金鑰不顯示）", _setting_src("base_url")),
        _s(s, "llm_timeout_s", "單次模型呼叫最多等幾秒，超過算失敗"),
        _s(s, "commander_max_tokens", "指揮官一次計劃最多輸出幾個 token"),
        _s(s, "llm_context_tokens", "模型的上下文長度；超過會先刪減事件背景"),
        _row("strict（不退回規則）", not getattr(s, "llm_fallback_to_mock", True),
             "true＝模型失敗就安全停止，不用規則計劃代替", _setting_src("llm_fallback_to_mock")),
        _s(s, "llm_fallback_to_mock", "模型輸出不合格時改用規則計劃（會記稽核）"),
        _s(s, "model_slots", "同時可跑幾個模型呼叫"),
        _c("warroom.agent.main_agent", "CASUAL_TEMPERATURE", "閒聊和一般回答的溫度；讀廠內資料、計劃維持 0"),
    ])

    trim = _const("warroom.agent.commander", "TRIM_STEPS")
    g("commander", "指揮官／狀態機", [
        _s(s, "approval_timeout_s", "送核准後幾秒沒人決定就逾時"),
        _live(sm, "warroom.agent.state_machine", "StateMachine", "max_replans", "max_replans", "換假設重新計劃最多幾次，超過就安全停止",
              _line("warroom/agent/state_machine.py", r"max_replans: int =")),
        _c("warroom.contracts", "MAX_CONSECUTIVE_FAILURES", "連續失敗幾次就安全停止並保留原排程"),
        _c("warroom.contracts", "MAX_TOOL_RETRIES", "工具失敗後重試幾次"),
        _c("warroom.contracts", "MAX_DISCUSS_REPLANS", "主管討論後重新計劃最多幾次"),
        _live(sm, "warroom.agent.state_machine", "StateMachine", "verify_poll_s", "verify_poll_s", "驗證階段每隔幾秒（工廠時間）讀一次感測器",
              _line("warroom/agent/state_machine.py", r"verify_poll_s: float =")),
        _live(sm, "warroom.agent.state_machine", "StateMachine", "verify_confirm_s", "verify_confirm_s", "數值沒回來要持續幾秒才判定假設不成立",
              _line("warroom/agent/state_machine.py", r"verify_confirm_s: float =")),
        _row("TRIM_STEPS", list(trim) if trim is not MISSING else MISSING, "上下文太長時，依這個順序刪減背景資料",
             _const_src("warroom/agent/commander.py", "TRIM_STEPS")),
    ])

    g("nemo", "Agent Nemo", [
        _c("warroom.agent.main_agent", "MAX_STEPS", "一次對話最多幾輪工具呼叫"),
        _c("warroom.agent.main_agent", "MAX_CALLS_PER_STEP", "每輪最多同時呼叫幾個工具"),
        _c("warroom.agent.main_agent", "MAX_SOP_SEARCHES", "一次對話最多查幾次 SOP"),
        _c("warroom.agent.main_agent", "MAX_QUERY_TOPICS", "一次對話最多讀幾個廠內主題", key="MAX_QUERY_TOPICS（每輪主題上限）"),
        _c("warroom.agent.main_agent", "BUDGET_S", "一次對話的時間上限（秒）"),
        _c("warroom.agent.main_agent", "MEMORY_TURNS", "記得最近幾輪對話"),
        _c("warroom.agent.main_agent", "MAX_TOKENS", "一則回覆最多幾個 token"),
        _c("warroom.agent.main_agent", "MAX_CHARS", "一則回覆最多幾個字"),
        _c("warroom.api.plant_query", "MAX_CHARS", "query_plant 一次回傳最多幾個字", key="query_plant 上限"),
    ])

    # 模型用量上限 (warroom/agent/budget.py): 80 % 閒聊改程式回覆、90 % 暫停預先規劃、100 % 對話改規則；事件規劃永不擋
    g("budget", "模型用量上限", [
        _s(s, "budget_daily_tokens", "今天（實際日期）全部模型 token 上限；80% 閒聊改程式回覆、90% 暫停預先規劃、100% 對話改規則模式"),
        _s(s, "budget_daily_model_s", "今天全部模型呼叫的秒數上限（和 token 取較高的比例）"),
        _s(s, "budget_event_tokens", "單一事件的 token 上限；超過就不再用模型重新規劃（改規則計劃）"),
        _s(s, "budget_conversation_tokens", "單段 Agent Nemo 對話的 token 上限；超過這段對話改規則模式"),
    ])

    g("preplan", "預先擬計劃", [
        _row("啟用", getattr(r, "preplan_on", MISSING), "有風險的訂單先擬好計劃，等主管核准或討論",
             "env WARROOM_PREPLAN（warroom/api/runtime.py）"),
        _row("PREPLAN_MAX", getattr(type(r), "PREPLAN_MAX", MISSING), "同時最多擬幾份預先計劃",
             _const_src("warroom/api/runtime.py", "PREPLAN_MAX")),
        _row("PREPLAN_EVERY_S", getattr(type(r), "PREPLAN_EVERY_S", MISSING), "每隔幾秒（工廠時間）看一次有風險的訂單",
             _const_src("warroom/api/runtime.py", "PREPLAN_EVERY_S")),
        _row("優先順序", _prio("preplan"), "在模型排隊裡的順序（數字小先跑）；事件永遠排在前面",
             _const_src("warroom/agent/orchestrator.py", "PRIORITY")),
    ])

    prio = _const("warroom.agent.orchestrator", "PRIORITY")
    labels = _const("warroom.agent.orchestrator", "PRIORITY_LABEL")
    qrows = [_s(s, "model_slots", "同時可跑幾個模型呼叫")]
    if prio is not MISSING:
        for k, v in sorted(prio.items(), key=lambda kv: (kv[1], kv[0])):
            lab = labels.get(v, "") if labels is not MISSING else ""
            qrows.append(_row(f"PRIORITY[{k}]", v, f"{lab}：數字小的先拿到模型", _const_src("warroom/agent/orchestrator.py", "PRIORITY")))
    else:
        qrows.append(_row("PRIORITY", MISSING, "模型排隊的優先順序", "warroom/agent/orchestrator.py"))
    g("queue", "模型排隊", qrows)

    srows: list[dict[str, Any]] = []
    for name in ("COMPOUND_STOP_C", "CURATIVE_STOP_C", "CURE_LOW_STOP_C", "CURE_TEMP_MAX_C"):
        srows.append(_c("warroom.factory", name, {"COMPOUND_STOP_C": "煉膠（未加硫）達到這個溫度就停機",
                                                   "CURATIVE_STOP_C": "已加硫膠料達到這個溫度就停機（防焦燒）",
                                                   "CURE_LOW_STOP_C": "硫化中低於這個溫度就扣模待 QC",
                                                   "CURE_TEMP_MAX_C": "硫化中高於這個溫度就警示"}[name]))
    rules = getattr(safety, "rules", None)
    if rules is None:
        dr = _const("warroom.safety.core", "default_rules")
        rules = dr() if dr is not MISSING else []
    for rule in rules:
        srows.append(_row(rule.id, f"{rule.op} {rule.threshold:g}°C → {_plain(rule.action)}", rule.description,
                          _line("warroom/safety/core.py", rf'id="{re.escape(rule.id)}"')))
    core = "warroom/safety/core.py"
    for attr, meaning in (("confirm_samples", "突然跳變的讀值要連續幾筆違規才動作"),
                          ("rearm_samples", "恢復正常要連續幾筆才重新上膛"),
                          ("data_loss_s", "接近上限時斷訊幾秒就發警示（SR-DATA-LOSS）"),
                          ("near_margin_c", "離上限幾度內算「接近」"),
                          ("max_rate_c_per_s", "每秒變化超過這個值算跳變")):
        srows.append(_live(safety, "warroom.safety.core", "RuleSafetyCore", attr, attr, meaning, _line(core, rf"{attr}: (int|float) =")))
    g("safety", "安全核心", srows)

    kb = getattr(getattr(r, "c", None), "kb", None)
    g("kb", "知識庫", [
        _s(s, "embed_model", "把 SOP 轉成向量的嵌入模型"),
        _row("目前模式", getattr(kb, "mode", "keyword") if kb is not None else MISSING, "vector＝向量＋字詞；其他＝只有字詞",
             "warroom/kb/store.py"),
        _s(s, "kb_min_score", "SOP 搜尋分數低於這個值就不算查到"),
        _c("warroom.kb.store", "MAX_CHUNK_CHARS", "SOP 切段時每段最多幾個字"),
    ])

    trows: list[dict[str, Any]] = []
    specs = _const("warroom.tools.specs", "TOOL_SPECS")
    if specs is not MISSING:
        for name, spec in specs.items():
            trows.append(_row(_plain(name), f"{_plain(spec.base_risk)}・{_plain(spec.kind)}・{spec.timeout_s:g} 秒",
                              f"{spec.description}｜可用狀態：{'、'.join(_plain(st) for st in spec.allowed_states) or '—'}",
                              _line("warroom/tools/specs.py", rf"ToolName\.{name.name}\s*:")))
    tbs = _const("warroom.contracts", "TOOLS_BY_STATE")
    trows.append(_row("TOOLS_BY_STATE", _plain(tbs) if tbs is not MISSING else MISSING, "每個狀態開放哪些工具",
                      _const_src("warroom/contracts.py", "TOOLS_BY_STATE")))
    g("tools", "工具", trows)

    guard = getattr(app_state, "pin_guard", None) if app_state is not None else None
    access = getattr(app_state, "access", None) if app_state is not None else None
    cats = _blocked_categories()
    g("net", "網路／安全", [
        _row("後台只限廠內網路", True, "外部位址（含 X-Forwarded-For 任一跳）一律 403，再檢查 PIN",
             _line("warroom/api/admin.py", r"後台只限廠內網路")),
        _row("主管 PIN 已設定", bool(getattr(access, "supervisor_pin", None)) if access is not None else MISSING,
             "遠端核准與後台要輸入主管 PIN；現場螢幕免輸入（PIN 本身不顯示）", "env WARROOM_SUPERVISOR_PIN"),
        _row("免主管 PIN（展示）", bool(getattr(access, "no_pin", False)) if access is not None else MISSING,
             "開著時所有裝置都當現場主管、不問 PIN（決賽展示用）", "env WARROOM_NO_PIN"),
        _row("PIN 錯誤上限（每台）", getattr(guard, "per_host_max", MISSING), "同一台裝置錯幾次就鎖，鎖的時間逐次加倍",
             _line("warroom/api/app.py", r"per_host_max: int =")),
        _row("PIN 鎖定起始秒數", getattr(guard, "base_lock_s", MISSING), "第一次鎖多久",
             _line("warroom/api/app.py", r"base_lock_s: float =")),
        _row("弱網推播延遲（秒）", getattr(app_state, "weak_push_delay_s", MISSING) if app_state is not None else MISSING,
             "弱網時遠端畫面延遲並合併推播", "env WARROOM_WEAK_PUSH_DELAY_S"),
        _row("敏感詞類別數", cats if cats is not None else MISSING, "Agent Nemo 對話前後用程式擋下的詞彙類別",
             "config/safety/blocked_terms.yaml"),
        _row("稽核金鑰來源", getattr(r.audit, "key_source", MISSING), "env／file＝金鑰在資料夾外；beside_log＝只防意外損壞",
             "env WARROOM_AUDIT_KEY / WARROOM_AUDIT_KEY_FILE"),
    ])
    return {"groups": groups, "note": "唯讀：這些值目前都不能在執行中修改，要改請改設定或程式後重新啟動"}


def _prio(kind: str) -> Any:
    p = _const("warroom.agent.orchestrator", "PRIORITY")
    return p.get(kind, MISSING) if p is not MISSING else MISSING


def _blocked_categories() -> int | None:
    try:
        import yaml
        d = yaml.safe_load((ROOT / "config" / "safety" / "blocked_terms.yaml").read_text(encoding="utf-8")) or {}
        return len(d.get("categories") or {})
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------- 系統健康
@functools.lru_cache(maxsize=1)
def commit_sha() -> str | None:
    env = os.environ.get("WARROOM_GIT_SHA") or os.environ.get("GIT_SHA")
    if env:
        return env[:12]
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             timeout=2, check=False)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - no git in the container: version only
        return None


def llm_latency(entries: list[Any], n: int = 20) -> dict[str, Any]:
    """Median total_ms of the last n successful model calls in the audit log."""
    ms = [e.payload.get("total_ms") for e in entries
          if str(getattr(e.kind, "value", e.kind)) == "llm_call" and not (e.payload or {}).get("error")]
    ms = [float(x) for x in ms if isinstance(x, (int, float))][-n:]
    return {"median_ms": round(statistics.median(ms)) if ms else None, "calls": len(ms)}


COMPONENT = {"sim": "工廠模擬", "detector": "異常偵測", "safety": "安全核心", "store": "排程版本庫", "approvals": "核准",
             "tools": "工具", "gate": "動作把關", "state_machine": "狀態機", "kb": "SOP 知識庫", "commander": "指揮官（模型）",
             "scheduler": "排程求解器"}
NET = {"online": ("ok", "連線正常", "手機核准、推播都正常"),
       "weak": ("warn", "弱網", "遠端畫面推播會延遲合併；Agent 照常運作，手機仍可核准"),
       "wan_down": ("warn", "外網斷線", "Agent 在本機照常運作；手機要連廠內 Wi-Fi 才能核准"),
       "lan_down": ("bad", "廠內網路斷線", "手機和遠端都不能核准，只能用現場觸控螢幕；警示燈亮")}
TEAM_MODE = {"off": "未載入", "unloaded": "尚未載入", "vector": "向量", "lexical": "只有字詞", "keyword": "只有字詞"}
SCENARIO = {"day_loop": "一日循環", "normal": "正常運轉"}


def _hms(s: float) -> str:
    s = int(s)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return (f"{d} 天 " if d else "") + f"{h} 小時 {m} 分"


def health(st: dict[str, Any]) -> list[dict[str, Any]]:
    """Plain-language cards from the status fields. light: ok (green) / warn (amber) / bad (red) / off (gray)."""
    cards: list[dict[str, Any]] = []

    def card(cid: str, title: str, light: str, headline: str, meaning: str, facts: list[str], fix: str,
             **extra: Any) -> None:
        cards.append({"id": cid, "title": title, "light": light, "headline": headline, "meaning": meaning,
                      "facts": [f for f in facts if f], "fix": fix, **extra})

    lat = st.get("llm_latency") or {}
    if st.get("llm_mode") == "mock":
        card("llm", "AI 模型", "off", "規則模式（沒有呼叫模型）", "指揮官用固定規則擬計劃，不會連模型。",
             [f"模型設定：{st.get('model')}"], "要用模型：以 LLM_MODE=llm 重新啟動，並確認模型伺服器有開。")
    else:
        ok = bool(st.get("llm_reachable"))
        slow = ok and (lat.get("median_ms") or 0) > 30_000
        card("llm", "AI 模型", "ok" if ok and not slow else "warn" if ok else "bad",
             f"{st.get('model')}・{'連得上' if ok else '連不上'}",
             "Agent 擬計劃和回答都靠這個模型。" if ok else "模型連不上：計劃會改用規則（或安全停止，看 strict 設定）。",
             [f"主機 {st.get('llm_host') or '—'}",
              f"最近 {lat['calls']} 次呼叫中位數 {lat['median_ms'] / 1000:.1f} 秒" if lat.get("median_ms") is not None else "還沒有成功的模型呼叫紀錄"],
             "檢查模型伺服器（Ollama／vLLM）有沒有在跑、BASE_URL 對不對；回應太慢就減少同時異常或換小模型。")

    kb_mode = st.get("kb_mode")
    kb_ok = bool(st.get("kb_ok"))
    card("kb", "知識庫", "bad" if not kb_ok else "ok" if kb_mode == "vector" else "warn",
         ("向量＋字詞搜尋" if kb_mode == "vector" else "只有字詞搜尋") if kb_ok else "不可用",
         "Agent 查 SOP 時用的資料。" + ("" if kb_mode == "vector" else "嵌入模型沒載入，搜尋只比對字詞，結果會比較不準。"),
         [f"本廠 SOP {st.get('kb_docs', 0)} 份・{st.get('kb_chunks', 0)} 段",
          f"團隊 SOP {st.get('team_docs', 0)} 份・{st.get('team_chunks', 0)} 段（{TEAM_MODE.get(st.get('team_mode'), st.get('team_mode'))}）"],
         "到「RAG 知識庫」按「重新索引」；仍是紅燈就檢查 Qdrant 與嵌入模型。")

    chain = st.get("audit_chain_ok")
    card("audit", "稽核鏈", "ok" if chain else "bad", "雜湊鏈完整 ✓" if chain else "雜湊鏈有問題 ✗",
         "每筆紀錄都用金鑰串在一起，被改過、刪過就會驗不過。",
         [f"{st.get('audit_entries', 0)} 筆", f"最新第 {st.get('audit_last_seq', 0)} 筆"],
         "到「稽核紀錄」按「驗證整條鏈」看第幾筆斷掉；不要手動改檔案，保留現場並通知系統負責人。", verify=True)

    net = st.get("network") or "online"
    light, word, means = NET.get(net, ("warn", net, ""))
    card("net", "網路", light, word, means, [], "檢查廠內 Wi-Fi／交換器與對外線路；斷線時改用現場觸控螢幕核准。")

    dl = st.get("day_loop") or {}
    speed = st.get("speed")
    card("sim", "模擬與情境", "warn" if st.get("paused") else "ok",
         f"{SCENARIO.get(st.get('scenario'), st.get('scenario'))}・{'已暫停' if st.get('paused') else '執行中'}",
         "示範用的工廠模擬；時間可以加速。",
         [f"工廠時間 {str(st.get('factory_time') or '')[:16].replace('T', ' ')}",
          f"速度 ×{speed:g}" if isinstance(speed, (int, float)) else "",
          f"一日循環第 {dl.get('loop')} 輪" if dl.get("loop") is not None else ""],
         "在示範控制列按「播放」；時間不對就「重來」重新載入情境。")

    rules = st.get("safety_rules") or []
    acts = st.get("safety_actions_today", 0)
    card("safety", "安全核心", "ok" if rules and not acts else "warn" if rules else "bad",
         f"{len(rules)} 條規則上膛" if rules else "沒有規則上膛",
         "不經過 AI、直接用溫度門檻停機或扣模的保護。" + (f"今天動作過 {acts} 次。" if acts else "今天還沒有動作。"),
         ["、".join(rules)], "有動作是正常保護：到該機台看原因並排除；沒有規則上膛請立刻通知系統負責人。")

    card("memory", "記憶", "ok", f"機台筆記 {st.get('memory_notes', 0)} 則",
         "主管寫的機台筆記（Agent 規劃時會參考）和進行中的對話。",
         [f"進行中對話 {st.get('conversations', 0)} 個"], "對話在換情境或一日循環換日時會清空，這是正常的。")

    deg = st.get("degraded") or {}
    card("service", "服務", "warn" if deg else "ok", f"已運轉 {_hms(st.get('uptime_s') or 0)}",
         "所有元件正常。" if not deg else "有元件改用備援：" + "、".join(COMPONENT.get(k, k) for k in deg) + "。",
         [f"版本 {st.get('version')}" + (f"・{st['commit']}" if st.get("commit") else "")],
         "重新啟動服務；仍在備援就看伺服器日誌中該元件的錯誤。",
         degraded=[{"name": COMPONENT.get(k, k), "why": v} for k, v in deg.items()])
    return cards
