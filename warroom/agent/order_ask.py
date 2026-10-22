"""Order questions in 問戰情室 (issue #95): what is being asked, which order is meant, and the answer text.

All of it is code. The questions it covers:
  今天有哪些訂單？ / 今天安排生產哪些訂單？ / 今天要交哪些訂單？ / 哪些訂單有遲交風險？
  [訂單編號] 現在做到哪裡？ / [訂單編號] 在哪台機器？預計何時完成？ / 那張單呢？ 它在哪台機器？ 現在完成多少？

Why code and not the model: the filtering, counting, progress and lateness are computed in warroom/tools/orders_today.py
from the plant's data; the answer is those results written out (orders_today.order_line), so no number in it can be
one a model made up. The model still plans and answers every other kind of question.

Which order a follow-up means (the conversation's memory holds ids only, see convo.py):
  - an id named in the question is used, whatever the memory says;
  - 「那張單」「它」 with one remembered focus order -> that order; with several recent orders and no focus -> the user
    is asked to pick (the ids are offered); with nothing remembered -> the user is asked for the order id.
Nothing is guessed, and an id that is not an order is reported as not found rather than replaced by another order."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from warroom.agent.convo import ConvContext
from warroom.tools import orders_today as OT

Kind = Literal["today", "production", "delivery", "risk", "status", "where"]
SINGLE: frozenset[str] = frozenset({"status", "where"})

_EVENT_RX = re.compile(r"\bEV-\d{8}-\d{6}-[A-Z0-9][A-Z0-9-]*\b")
_ORDER_WORD = re.compile(r"訂單|工單|單子|[這那該]張|[這那]單|[這那]筆|批單")
_TODAY = re.compile(r"今天|今日|本日|當天")
_DEICTIC = re.compile(r"[那這該]張|[那這]一張|[那這]筆|[那這]單|[那這]個訂單|該單|上一張|剛剛那|剛才那|前面那|同一張|(?<!其)它(?!們)")
_RISK = re.compile(r"遲交|晚交|延誤|延遲交|逾期|誤交期|來不及|會晚|趕不上|交不出|風險")
_DELIV_STRONG = re.compile(r"要交|交貨|交付|出貨|該交")
_DELIV_DATE = re.compile(r"交期|到期")
_PROD = re.compile(r"生產|投產|製造|開工|在做|做哪些|安排")
# issue #182: 「SO-004 什摸時候完成」 (typo of 什麼) and 幾點好 / 何時做好 are when-done questions too
_WHERE = re.compile(r"哪台|哪一台|哪個機|哪部|機器|機台|號機|何時|幾時|(?:什麼|什摸|甚麼|神麼|啥)時候|啥時|"
                    r"幾點(?:完成|好|做好|做完|出來)|多久(?:完成|好|做好|做完)|預計.{0,4}完成|完工|做好|做完")
_PROGRESS = re.compile(r"做到哪|進度|完成多少|完成幾|完成了多少|做了多少|做了幾|做到|還剩|剩多少|剩幾|好了幾")
_LISTQ = re.compile(r"哪些|有什麼|有幾|多少張|幾張|有哪")


@dataclass(frozen=True)
class OrderIntent:
    kind: Kind
    ids: tuple[str, ...] = ()            # order-id-shaped words named in the question (normalised)
    pointed: bool = False                # 「那張單」「它」 or a bare follow-up: the order is meant, not named
    events: tuple[str, ...] = ()         # event ids named in the question (remembered, not answered here)


def classify(question: str, *, has_machine: bool, memory: ConvContext) -> OrderIntent | None:
    """The order question this is, or None (not an order question: the usual planner takes it)."""
    q = question.strip()
    ids = tuple(OT.ids_in_text(q))
    events = tuple(dict.fromkeys(_EVENT_RX.findall(q.upper())))
    order_word, deictic = bool(_ORDER_WORD.search(q)), bool(_DEICTIC.search(q))
    prog, where = bool(_PROGRESS.search(q)), bool(_WHERE.search(q))
    today, listq = bool(_TODAY.search(q)), bool(_LISTQ.search(q))
    mem = bool(memory.focus_order or memory.recent_orders)
    single: Kind = "where" if (where and not prog) else "status"
    if ids:                                                      # a named order
        return OrderIntent(single, ids=ids, events=events)
    if deictic and (order_word or mem or prog or where):         # 「那張單呢？」「它在哪台機器？」
        return OrderIntent(single, pointed=True, events=events)
    if has_machine:                                              # a machine question belongs to the usual planner
        return None
    if _RISK.search(q) and (order_word or listq or today or mem):
        return OrderIntent("risk", events=events)
    if (_DELIV_STRONG.search(q) or (_DELIV_DATE.search(q) and today)) and (today or order_word):
        return OrderIntent("delivery", events=events)
    if _PROD.search(q) and (today or order_word) and (listq or today):
        return OrderIntent("production", events=events)
    if order_word and (today or listq):
        return OrderIntent("today", events=events)
    if (prog or where) and mem:                                  # 「現在完成多少？」 right after talking about an order
        return OrderIntent(single, pointed=True, events=events)
    return None


@dataclass
class Resolution:
    ids: list[str] = field(default_factory=list)
    needs: Literal["choice", "order"] | None = None              # the user must say which order (nothing is guessed)
    choices: list[str] = field(default_factory=list)
    via: Literal["named", "focus", "recent"] | None = None


def resolve(intent: OrderIntent, memory: ConvContext) -> Resolution:
    if intent.kind not in SINGLE:
        return Resolution()
    if intent.ids:
        return Resolution(ids=list(intent.ids), via="named")
    if memory.focus_order:
        return Resolution(ids=[memory.focus_order], via="focus")
    if len(memory.recent_orders) == 1:
        return Resolution(ids=[memory.recent_orders[0]], via="recent")
    if len(memory.recent_orders) > 1:
        return Resolution(needs="choice", choices=list(memory.recent_orders))
    return Resolution(needs="order")


def need_text(res: Resolution) -> str:
    if res.needs == "choice":
        return "你指的是哪一張訂單？最近談到的有：" + "、".join(res.choices) + "。請直接回我訂單編號，我沒有猜。"
    return "我不確定你指的是哪一張訂單（這段對話還沒有談到任何一張）。請告訴我訂單編號，例如 #A-311。"


# ------------------------------------------------------------------ the answer
def _labels(ids: list[str]) -> str:
    return "、".join(OT._press_label(p) for p in ids)


def where_text(b: dict[str, Any]) -> str:
    if b["status"] in ("完成", "待出貨", "已出貨"):
        return "已完成，沒有機台在做它。"
    if not b["machines"]:
        return f"{OT.NO_SCHEDULE}，目前沒有機台資料可說。"
    ahead = [m for m in b["machines"] if m not in b["machines_now"]]
    parts = []
    if b["machines_now"]:
        parts.append(f"硫化中：{_labels(b['machines_now'])}")
    if ahead:
        parts.append(f"排程接下來：{_labels(ahead)}")
    return "；".join(parts) + "。"


def compose(intent: OrderIntent, data: dict[str, Any], ids: list[str], *, limit: int = 12
            ) -> tuple[str, list[str], str | None]:
    """(answer text, order ids the answer was about, the single focus order or None)."""
    d = datetime.fromisoformat(data["date"])
    by_id = {b["id"]: b for b in data["orders"]}
    c = data["counts"]
    if intent.kind == "today":
        shown = list(dict.fromkeys(data["production"] + data["delivery"]))
        return OT.overview_text(data, limit=limit), shown, (shown[0] if len(shown) == 1 else None)
    if intent.kind == "risk":
        shown = data["overdue"] + data["late"] + data["other_risk"]
        return OT.risk_overview_text(data, limit=limit), shown, (shown[0] if len(shown) == 1 else None)
    if intent.kind in ("production", "delivery"):
        key, title = ("production", "今日生產") if intent.kind == "production" else ("delivery", "今日交貨")
        shown = list(data[key])
        if not shown:
            basis = ("沒有在硫化機裡、今天也沒有製程排程的訂單" if intent.kind == "production"
                     else f"沒有交期是 {d:%m/%d} 的訂單")
            return f"今天（{d:%m/%d}）沒有{title}的訂單：{basis}（共檢查 {c['total']} 張）。", [], None
        more = f"…另有 {len(shown) - limit} 張，見訂單頁" if len(shown) > limit else ""
        head = f"今天（{d:%m/%d}）{title} {len(shown)} 張（共檢查 {c['total']} 張）："
        return head + "；".join(OT.order_line(by_id[i]) for i in shown[:limit]) + more, shown, (shown[0] if len(shown) == 1 else None)
    # a named / pointed order (status or where)
    found: list[str] = []
    lines: list[str] = []
    for oid in ids:
        b = OT.find(data, oid)
        if b is None:
            lines.append(f"查無訂單 {oid}：目前訂單清單（共 {c['total']} 張）沒有這個編號，請確認編號。")
            continue
        found.append(b["id"])
        lead = f"{b['id']} 的機台與預計完成：{where_text(b)}" if intent.kind == "where" else f"{b['id']} 現在的進度："
        lines.append(lead + OT.order_line(b) if intent.kind == "status" else lead + "（" + OT.order_line(b) + "）")
    return " ".join(lines), found, (found[0] if len(found) == 1 else None)
