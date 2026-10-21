"""Issue #10: the supervisor's words -> things code can check.

Pure helpers, no model and no state machine: the rule-based intent classifier (mock mode, and the
fallback when the model fails), the time-window parser, the constraint builder and validator, and
the check that refuses a command no tool can carry out (moving a machine, changing a setpoint, a PLC).

Code decides: whatever the model proposes, a constraint only exists when it validates here (a real
machine that the supervisor actually named, a kind code can enforce, a window code computed)."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

from warroom import factory as F
from warroom.contracts import PlanConstraint, TeamId

# ------------------------------------------------------------------ machines
_PRESS_RX = re.compile(r"(\d{1,2})\s*號(?:硫化)?機?")


def machines_in(text: str) -> list[str]:
    """Machine ids the text names ('9 號機', 'CURE-09', 'A 線大開煉機'). Same parser as 問戰情室."""
    from warroom.agent.ask import machines_in as _m
    return _m(text)


def press_label(machine_id: str) -> str:
    from warroom.agent.narrate import machine_label
    return machine_label(machine_id)


# ------------------------------------------------------------------ time windows
def window_for(text: str, now: datetime) -> tuple[str, datetime, datetime, bool]:
    """(label, start, end, explicit) for the time the text names, in factory time. Night shift is
    20:00-05:00 (page 6). Before 05:00, 「今晚」 still means the coming night (the supervisor at
    02:14 talks about tonight, not the night that is ending)."""
    now = now.astimezone(F.TZ)
    day = now.date()

    def at(d, h, m=0):
        return datetime.combine(d, time(h, m), tzinfo=F.TZ)
    t = text.replace(" ", "")
    tomorrow = day + timedelta(days=1)
    if re.search(r"明晚|明天晚上|明天夜班", t):
        return "明晚", at(tomorrow, 20), at(tomorrow + timedelta(days=1), 5), True
    if re.search(r"今晚|今天晚上|今夜|晚上|夜班|今天夜裡", t):
        s = at(day, 20)
        return "今晚", max(s, now) if now >= s else s, at(day + timedelta(days=1), 5), True
    if re.search(r"明早|明天早上|明天上午|明天一早", t):
        return "明早", at(tomorrow, 6), at(tomorrow, 12), True
    if re.search(r"明天|明日", t):
        return "明天", at(tomorrow, 0), at(tomorrow + timedelta(days=1), 0), True
    if re.search(r"這週|本週|這禮拜|這星期|本周|這周", t):
        return "本週", now, now + timedelta(days=7), True
    if re.search(r"今天|今日|白天", t):
        return "今天", now, at(tomorrow, 0), True
    if re.search(r"現在|馬上|立刻|目前|立即", t):
        return "現在起 8 小時", now, now + timedelta(hours=8), True
    return "接下來 24 小時", now, now + timedelta(hours=24), False


WHEN_WORDS = {"tonight": "今晚", "tomorrow_night": "明晚", "tomorrow_morning": "明早", "tomorrow": "明天",
              "today": "今天", "now": "現在", "this_week": "本週"}


# ------------------------------------------------------------------ intent rules
DOWN_RX = re.compile(r"保養|維修|檢修|停機|停用|不能用|不可用|不能排|不要排|別排|故障|壞了|壞掉|要修|拆模|換模|清模")
NO_OT_RX = re.compile(r"不要加班|不加班|別加班|不能加班|不准加班|沒辦法加班|無法加班|不想加班|避免加班")
NO_TRANSFER_RX = re.compile(r"不要調人|不調人|別調人|不要跨組|不跨組|別跨組|不能調人|不能跨組|不要從.{0,6}調|避免跨組|不要動.{0,3}線的人")
PREFER_RX = re.compile(r"(?:選|用|改用|改選|採用|就用)\s*([AB])\s*(?:方案|案)?|([AB])\s*方案|方案\s*([AB])")
PREFER_KIND_RX = re.compile(r"(?:選|用|改用|改選|採用|就用)\s*(加班|調人|跨組)")
SHIFT_RX = re.compile(r"延後|提前|改到|挪到|改成")
QUESTION_RX = re.compile(r"[？?]$|為什麼|為何|怎麼|多少|哪|嗎|呢|是否|會不會|能不能|可不可以|幾|什麼")
CHANGE_HINT_RX = re.compile(r"改|換|調|不要|別|要|請|幫我|應該|必須|先|停|加|減|延|提前")


FACTORY_RX = re.compile(r"機|排程|訂單|交期|班|人力|加班|調人|溫度|硫化|方案|計劃|計畫|保養|維修|SOP|sop|延誤|產能|胎|模|核准")


def rule_intent(text: str) -> str:
    """'chitchat' | 'change' | 'question' | 'unsupported' (sounds like a change, but nothing code can
    enforce) | 'unclear' (not a factory message: nothing is guessed)."""
    from warroom.agent.ask import smalltalk_reply
    t = text.strip()
    if smalltalk_reply(t) is not None:
        return "chitchat"
    if NO_OT_RX.search(t) or NO_TRANSFER_RX.search(t) or PREFER_RX.search(t) or PREFER_KIND_RX.search(t):
        return "change"
    if DOWN_RX.search(t) and machines_in(t):
        return "change"
    if SHIFT_RX.search(t) and machines_in(t):
        return "change"
    if not FACTORY_RX.search(t) and not machines_in(t):
        return "unclear"
    if QUESTION_RX.search(t):
        return "question"
    if CHANGE_HINT_RX.search(t) or DOWN_RX.search(t):
        return "unsupported"
    return "question"


def _chip(c: dict[str, Any]) -> str:
    k = c["kind"]
    if k == "machine_unavailable":
        return f"{press_label(c['machine_id'])} {c['window_label']}不可用"
    if k == "no_overtime":
        return "不加班"
    if k == "no_transfer":
        return "不跨組調人"
    opt = c.get("option")
    return {"overtime": "優先夜班加班", "transfer": "優先跨組調人"}.get(opt, f"優先方案 {opt}")


def make_constraint(kind: str, text: str, now: datetime, *, machine_id: str | None = None,
                    option: str | None = None, when: str | None = None) -> PlanConstraint:
    d: dict[str, Any] = {"kind": kind, "text": text, "at": now}
    if kind == "machine_unavailable":
        label, start, end, explicit = window_for(text, now)
        if not explicit and when in WHEN_WORDS:                 # the model named a time the rules missed
            label, start, end, _ = window_for(WHEN_WORDS[when], now)
        d.update(machine_id=machine_id, window_label=label, start=start, end=end)
    if kind == "prefer_option":
        d["option"] = option
    d["label"] = _chip(d)
    return PlanConstraint(**d)


def rule_constraints(text: str, now: datetime) -> tuple[list[PlanConstraint], str | None]:
    """Constraints the rules find in the text, and why a change could not be encoded (or None)."""
    out: list[PlanConstraint] = []
    t = text.strip()
    why: str | None = None
    if DOWN_RX.search(t) or SHIFT_RX.search(t):
        named = machines_in(t)
        presses = [m for m in named if m in F.CURE_PRESS_IDS]
        for m in presses:
            out.append(make_constraint("machine_unavailable", t, now, machine_id=m))
        others = [m for m in named if m not in F.CURE_PRESS_IDS]
        if others:
            why = (f"{'、'.join(press_label(m) for m in others)}不在硫化排程裡（排程只排 11 台硫化機），"
                   "我沒辦法用排程保證它不被使用")
        elif not named and DOWN_RX.search(t):
            why = "我需要知道是哪一台機台（例如「9 號機今晚要保養」）"
    if NO_OT_RX.search(t):
        out.append(make_constraint("no_overtime", t, now))
    if NO_TRANSFER_RX.search(t):
        out.append(make_constraint("no_transfer", t, now))
    m = PREFER_RX.search(t)
    if m:
        out.append(make_constraint("prefer_option", t, now, option=next(g for g in m.groups() if g)))
    else:
        mk = PREFER_KIND_RX.search(t)
        if mk:
            out.append(make_constraint("prefer_option", t, now,
                                       option="overtime" if mk.group(1) == "加班" else "transfer"))
    return out, why


def validate_constraint(raw: dict[str, Any], text: str, now: datetime) -> tuple[PlanConstraint | None, str | None]:
    """A constraint the model proposed, checked by code. The machine must be a curing press the
    supervisor actually named in this message; the window is computed by code from the words."""
    kind = raw.get("kind")
    if kind not in ("machine_unavailable", "no_overtime", "no_transfer", "prefer_option"):
        return None, f"unknown constraint kind {kind!r}"
    if kind == "machine_unavailable":
        mid = str(raw.get("machine") or raw.get("machine_id") or "")
        named = machines_in(text)
        if mid not in named:
            return None, f"machine {mid!r} is not named in the message (named: {named})"
        if mid not in F.CURE_PRESS_IDS:
            return None, f"{mid} is not a curing press: the schedule cannot keep it free"
        return make_constraint(kind, text, now, machine_id=mid, when=raw.get("when")), None
    if kind == "prefer_option":
        opt = str(raw.get("option") or "")
        if opt not in ("A", "B", "overtime", "transfer"):
            return None, f"unknown option {opt!r}"
        return make_constraint(kind, text, now, option=opt), None
    words = NO_OT_RX if kind == "no_overtime" else NO_TRANSFER_RX
    if not words.search(text) and not re.search(r"加班" if kind == "no_overtime" else r"調人|跨組|調.{0,4}人", text):
        return None, f"{kind} is not supported by the words of the message"
    return make_constraint(kind, text, now), None


def same_constraint(a: PlanConstraint, b: PlanConstraint) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind == "machine_unavailable":
        return a.machine_id == b.machine_id and a.start == b.start and a.end == b.end
    if a.kind == "prefer_option":
        return a.option == b.option
    return True


CAN_DO = ("我能做的：把某台硫化機某時段排除在排程外（例如「9 號機今晚要保養」）、不加班、不跨組調人、"
          "改選 A／B 方案；或直接核准、駁回。")


# ------------------------------------------------------------------ commands
PHYSICAL_RX = re.compile(r"搬|移動|移走|挪動|拆掉|吊|開機|關機|啟動|重啟|關掉電|斷電|調溫|升溫|降溫|加熱|"
                         r"溫度設|設定溫度|設成.{0,4}度|調.{0,3}蒸汽|開閥|關閥|閥門|按鈕|PLC|plc|致動|馬達|轉速")


def physical_request(text: str) -> str | None:
    """Why no tool can do this (it would move or set a machine), else None. Page 5: the agent has no
    actuator and no PLC write; people do that, after a notice."""
    m = PHYSICAL_RX.search(text)
    if not m:
        return None
    return (f"「{m.group(0)}」要直接動到機台或設定值，我沒有這種工具（系統裡沒有任何能寫入 PLC 或移動機台的工具，"
            "這是安全設計）。我能做的：通知相關組別去現場處理（需你核准）、把機台排除在排程外並重排、查讀值與 SOP。")


TEAM_WORDS: list[tuple[str, TeamId]] = [
    ("維修", TeamId.MAINTENANCE), ("保養", TeamId.MAINTENANCE), ("品管", TeamId.QC), ("品保", TeamId.QC),
    ("硫化", TeamId.CURING), ("胎體", TeamId.BODY), ("胎面", TeamId.BODY), ("胎側", TeamId.SIDE),
    ("鋼絲", TeamId.BEAD_NYLON), ("尼龍", TeamId.BEAD_NYLON), ("大開煉", TeamId.BIGOPEN), ("貼合", TeamId.BOND),
    ("修邊", TeamId.TRIM), ("整修", TeamId.TRIM), ("主管", TeamId.SUPERVISOR),
]


def command_teams(text: str) -> list[TeamId]:
    out: list[TeamId] = []
    for w, team in TEAM_WORDS:
        if w in text and team not in out:
            out.append(team)
    return out


def command_kind(text: str) -> str:
    """notify | schedule | query | other (rule-mode command planner)."""
    t = text
    if re.search(r"重排|排程|不要排|別排|排除", t) or (DOWN_RX.search(t) and re.search(r"排", t)):
        return "schedule"
    if re.search(r"通知|告訴|叫|請.{0,6}(組|班|人)|聯絡|提醒|派", t):
        return "notify"
    if QUESTION_RX.search(t) or re.search(r"查|看一下|看看|確認", t):
        return "query"
    return "other"


def command_machine(text: str) -> str | None:
    named = machines_in(text)
    return named[0] if named else None
