"""Rebuild data/orders_seed.xlsx so the imported order book matches the plant sim.

The plant sim (warroom.sim) has its own presses already curing #M-201 / #L-301 and #A-311 (one
large tire, due in 3 days, page 11) as press 7's next mold. With ORDERS_XLSX set, orders and shifts
come from this workbook while the presses stay the sim's, so the two must name the same orders.
This script takes the 訂單 and 排程 sheets from the sim (press-7 scenario, 10/03 02:12) and keeps
the workbook's own 班表 / 人員 / 請假 sheets (10/03 night shift short 1, page 11). Order ids in
半成品 and 作業紀錄 are remapped to the sim's.

    uv run python data/make_orders_seed.py
"""
from __future__ import annotations

from pathlib import Path

import openpyxl

from warroom.contracts import ScenarioName
from warroom.sim import Factory

HERE = Path(__file__).resolve().parent
XLSX = HERE / "orders_seed.xlsx"

# old workbook order id -> sim order id (sizes agree)
REMAP = {"#A-309": "#M-201", "#A-315": "#M-202", "#A-318": "#M-202", "#B-207": "#L-301", "#B-212": "#L-302",
         "#S-103": "#S-102", "#A-311": "#A-311", "#S-101": "#S-101", "#S-102": "#S-102"}
CUSTOMERS = {"#S-101": "南亞堆高機", "#S-102": "台灣搬運車", "#M-201": "高雄港機具", "#M-202": "東元物流",
             "#L-301": "台中港務", "#A-311": "台塑工業車輛", "#L-302": "中鋼運輸"}


def _rewrite(ws, header: list[str], rows: list[list]) -> None:
    ws.delete_rows(1, ws.max_row)
    ws.append(header)
    for r in rows:
        ws.append(r)


def main() -> None:
    sim = Factory(seed=7)
    sim.load_scenario(ScenarioName.PRESS7_TEMP_DROP)
    wb = openpyxl.load_workbook(XLSX)

    orders = [[o.id, o.size.value, o.qty, o.done, o.due.isoformat(), CUSTOMERS.get(o.id, o.customer), o.mold_id,
               o.priority] for o in sim.orders()]
    _rewrite(wb["訂單"], ["id", "size", "qty", "done", "due", "customer", "mold_id", "priority"], orders)

    jobs = []
    for m in sorted(sim.machines(), key=lambda m: m.machine_id):
        cj = m.cure_job
        if cj is None:
            continue
        jobs.append([cj.job_id, cj.order_id, cj.size.value, m.machine_id, cj.mold_id, cj.mold_in_at.isoformat(),
                     round(cj.cure_required_h, 2), 1])
    _rewrite(wb["排程"], ["id", "order_id", "size", "press_id", "mold_id", "mold_in", "cure_h", "frozen"], jobs)

    # 作業紀錄: each press's 入模 row is the mold-in of the tire the sim has in it
    size_txt = {"S": "小胎", "M": "中胎", "L": "大胎"}
    by_press = {m.machine_id: m.cure_job for m in sim.machines() if m.cure_job is not None}
    ws = wb["作業紀錄"]
    header = [c.value for c in ws[1]]
    ix = {h: i for i, h in enumerate(header)}
    for row in ws.iter_rows(min_row=2):
        mid, kind = row[ix["machine_id"]].value, row[ix["kind"]].value
        cj = by_press.get(mid)
        if kind == "入模" and cj is not None:
            row[ix["ts"]].value = cj.mold_in_at.isoformat()
            row[ix["order_id"]].value = cj.order_id
            row[ix["note"]].value = f"{size_txt[cj.size.value]}入模，128°C，預計 {cj.cure_required_h:g} h"
            h = cj.mold_in_at.hour        # who was on duty (page 6 shifts: curing 6-15 / 14-23, body night 20-5)
            team, op = ("curing", "CURI-早2") if 6 <= h < 14 else ("curing", "CURI-中1") if 14 <= h < 20 \
                else ("body", "BODY-夜1")
            row[ix["team"]].value, row[ix["operator"]].value = team, op
    rows = sorted(ws.iter_rows(min_row=2, values_only=True), key=lambda r: str(r[ix["ts"]]))
    _rewrite(ws, header, [list(r) for r in rows])

    for name in ("半成品", "作業紀錄"):
        ws = wb[name]
        header = [c.value for c in ws[1]]
        if "order_id" not in header:
            continue
        col = header.index("order_id") + 1
        for row in ws.iter_rows(min_row=2):
            v = row[col - 1].value
            if v in REMAP:
                row[col - 1].value = REMAP[v]
    wb.save(XLSX)
    print(f"wrote {XLSX}: {len(orders)} orders, {len(jobs)} in-press jobs")


if __name__ == "__main__":
    main()
