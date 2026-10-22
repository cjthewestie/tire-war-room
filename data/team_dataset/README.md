# Team dataset V2 (團隊正式資料集)

CJ, 2026-10-20: 「這份資料是隊友做的，當作團隊正式資料集」. Used as replay + reference data (option A).

**All of it is synthetic.** A teammate generated it with the generator scripts in `實心胎_合成資料.zip` (`generate_synthetic_data.py`, `gen_plan.py`, `gen_signals.py`, `gen_sop.py`, `sched_core.py`, `build_xlsx_v2.py`). Those scripts are not in this repo and were never run here; only their outputs are. `validation.txt` holds the generator's 25 consistency checks, all PASS.

## Files

| File | Rows | What |
|---|---|---|
| `orders.csv` | 9 | Orders SO-001..009, 300 tires (100 per size) |
| `tire_unit.csv` | 300 | One row per tire: press, mold-in, cure, de-mold, status at EV-1 / EV-2 |
| `tire_step.csv` | 7,500 | Every process step of every tire, with equipment and team |
| `staff.csv` / `roster.csv` | 20 / 1,980 | 20 people, daily roster 2026-10-20 onward (S11 sick on the EV-2 night) |
| `events.csv` | 2 | EV-1 A03 大開煉機 112 °C (2026-10-22 14:26), EV-2 V07 硫化機 128 → 122 → 119.5 °C (2026-10-28 02:14) |
| `ev1_affected.csv` | 2 | Tires hit by EV-1 |
| `audit_cure.csv` | 2 | Arrhenius equivalent cure for S-027 on V07 at 122 and 119.5 °C |
| `history_daily.csv` / `history_incident.csv` | 20 / 8 | Past days and 8 past incidents |
| `mold_change.csv` | 6 | Planned M↔L mold changes |
| `sensor_meta.csv` | 16 | The 16 sensors (A03/A04/A06, V06/V07/V08) |
| `sensor_readings.csv.gz` | 311,072 | ts, sensor_id, value, quality, scenario; 2026-10-21 00:00 → 10-28 03:00. 10 s steps, except 1 Hz in the two event windows (10-22 13:30–15:00 and 10-28 01:30–03:00) |
| `sop_chunks.jsonl` | 60 | 12 SOPs × 5 chunks (SOP-01..SOP-12) |
| `validation.txt` | 25 | The generator's checks, all PASS |
| `team_dataset.xlsx` (was 實心胎_合成資料_V2_1.xlsx) | — | The workbook: input sheets 1_–9_ (global assumptions, tire specs, machine list, orders, processes, teams, staff) and result sheets D1–D12 |

## What was committed and what was not

- `sensor_readings.csv` (13.0 MB) is committed gzip-compressed as `sensor_readings.csv.gz` (1.56 MB). The code reads the .gz directly.
- `synthetic_factory_1.db` (27.1 MB SQLite) is **not** committed. Its 13 tables hold the same rows as the CSVs (checked row by row on 2026-10-20: every table, symmetric difference 0). Rebuild it from the CSVs if you need SQL.
- The xlsx result sheets D1–D12 match the CSVs row for row (same check). The input sheets 1_–9_ exist only in the xlsx, which is why it is committed.
- `sop_chunks_1.jsonl` (the separate download) is byte-identical to `sop_chunks.jsonl` in the zip.

Source files, sha256:

```
6a9cb657685207cbbd4521d59f7e81a583188d9561755ffc5d153eac75c2941a  實心胎_合成資料.zip
c92be8a57b2e72da9e116825b6c877a77543cf91e4738c9acd0b4c58f2ea87f7  sensor_readings.csv
47cc156c100a47b56a4d1739067a292e01fe679668795a92c6162f2204d5f1b9  sop_chunks_1.jsonl
e7679c2cdd17620372e3787944771c24ff2794a24e032f738a238f79a9985830  synthetic_factory_1.db
08b89b72b4dd5b1633ecfa5291f38bcf749b37a674d310a8f08a2988202b7477  實心胎_合成資料_V2_1.xlsx
```

The hashes above are those of the files as the teammate delivered them. Two committed files were edited afterwards (see the next section), so their current hashes differ.

## Edit record: customer names (2026-10-22, issue text by CJ)

The placeholder customer names 客戶A–G (this dataset) were replaced with invented Taiwanese-style company names for the demo. The 客戶甲～癸 of the simulator (`warroom/sim/`) were replaced in the same pass with a second, separate set; the two sets are different customers and were not merged. **All names are made up for the demo (Demo／Synthetic Data) and do not refer to real companies.** Nothing else in the data changed: order ids, sizes, quantities, due dates, priorities and every other cell are as delivered.

| Was | Now | | Was | Now |
|---|---|---|---|---|
| 客戶A | 福氣輪業股份有限公司 | | 客戶甲 | 順風順水工業有限公司 |
| 客戶B | 金旺來工業社 | | 客戶乙 | 日日興物流股份有限公司 |
| 客戶C | 大力出奇蹟實業有限公司 | | 客戶丙 | 五路財神企業社 |
| 客戶D | 永固機械股份有限公司 | | 客戶丁 | 台灣勇腳機械有限公司 |
| 客戶E | 黑狗兄輪業商社 | | 客戶戊 | 金好運運輸商社 |
| 客戶F | 好運輪輪來企業社 | | 客戶己 | 萬年富工業股份有限公司 |
| 客戶G | 鐵牛運搬股份有限公司 | | 客戶庚 | 尚勇輪業有限公司 |
| | | | 客戶辛 | 一路發搬運企業社 |
| | | | 客戶壬 | 穩穩發物流有限公司 |
| | | | 客戶癸 | 台灣鐵漢實業股份有限公司 |

Files edited: `orders.csv` (customer column), `team_dataset.xlsx` (sheet `5_訂單`, including its 「範例列」 row, which had used 客戶甲, and sheet `D1_訂單結果`; the replaced cells keep their original font), plus `data/orders_seed.xlsx` and `data/orders_synthetic.xlsx` (sheet 訂單). In `orders_seed.xlsx` the rush tyre #A-311 had its own older customer name (台塑工業車輛, a name that resembled a real company); it is now 永固機械股份有限公司, the same customer as in `orders_synthetic.xlsx` and in the dataset (#A-311 is split off SO-004, see `warroom/data/team_plan.py`). Every other cell, formula and workbook part is unchanged (checked cell by cell against the committed versions).

sha256 after the edit:

```
77ef5fe96b56b4753990c4b3abda0634d5ba6aac079132036fdd5ca226b7eba0  orders.csv   (before: fa6c3508f5d71e75ea9cee4e74aa5b3917b6933b85c02fd571a1e8d1fe04e1d4)
41a60441c450054b6e0b9e3576071892854c95e6a2ce25e30e23048b1ecb102f  team_dataset.xlsx
d8fd4158eca460a646363aa632edc26301465b2909914ffff2547ee1abe77ede  ../orders_seed.xlsx
26469f052fb2f0878bd703a27919a6292d1e9e53b79d4f5f52f32cc9278f52fe  ../orders_synthetic.xlsx
```

A third, independent set lives in the built-in seed of `warroom/kb/orders_db.py` (used only when no workbook is imported and there is no sim) and in `data/make_orders_seed.py`. Its 9 older names, which resembled real companies, were replaced too (#A-311 uses 永固機械股份有限公司, the same customer as in the dataset). `make_orders_seed.py` is out of date and was edited but **not run**; no dataset was regenerated.

| Was | Now |
|---|---|
| 南亞堆高機 (#S-101) | 興旺堆高機股份有限公司 |
| 台灣搬運車 (#S-102) | 阿財搬運車行 |
| 中部物流設備 (#S-103) | 順手物流設備有限公司 |
| 台塑工業車輛 (#A-311) | 永固機械股份有限公司 |
| 高雄港機具 (#A-309) | 港都機具工業有限公司 |
| 東元物流 (#A-315) | 元氣滿滿物流股份有限公司 |
| 長榮貨櫃場 (#A-318) | 海派貨櫃場有限公司 |
| 台中港務 (#B-207) | 青海碼頭作業有限公司 |
| 中鋼運輸 (#B-212) | 鋼鐵人運輸股份有限公司 |

Not changed, so they may still hold the old names: the uncommitted `synthetic_factory_1.db` (its orders table), `data/runtime/` (history.sqlite, audit.jsonl and the memory files derived from it), and the historical logs in `bench/results/`.

## How the war room uses it

- **Machine ids**: `warroom/data/team_ids.py` maps the dataset codes onto our 28 machines: A01..A07 → A-MIX, A-OPEN, A-BIGOPEN, A-SMALLOPEN, A-CAL, A-COOL, A-WIND; B01..B04 → B-MIX, B-OPEN, B-FORM, B-ASSY; C01..C04 → C-PAYOFF, C-EXTR, C-WIND, C-WRAP; D01 → D-CUT; L01 → BOND-01; V01..V11 → CURE-01..11.
- **People, molds, history** (#14, factory world): `warroom.data.team_dataset.load()` reads staff, roster, mold_change, history_incident and history_daily, with a per-scenario date offset (page 11 10/03 ↔ EV-2 10/28).
- **Replay**: `WARROOM_REPLAY=team_ev1|team_ev2|team_both ./start.sh` or `uv run python -m warroom.api demo --scenario normal --replay team_both --seconds 2400 --auto-approve`. In the normal scenario, the mapped machines read the dataset's 1 Hz curves, time-shifted onto the sim clock (date 2026-10-03, the window's own time of day). See `warroom/sim/team_replay.py`.
- **Orders and presses** (#20): every scenario now starts from the dataset's plan (`warroom/data/team_plan.py`, read by `Factory`): the orders of `orders.csv` (SO-001..009; the aliased tyre S-033 becomes the one-tyre order `#A-311`, taken off SO-004: 20 -> 19) and the tyre curing on each press at one dataset moment (`tire_unit.csv`: cure_start <= t < cure_end), shifted onto the scenario's day. Page 11 plays 10/28 02:12 (EV-2), page 2 and the open-mill scenario 10/22 (EV-1), `concurrent_3` 10/27 10:00, every other scenario 10/28 at its own time of day. Tyres de-molded by then count as done (page 11: S 21 / M 9 / L 7 of 100, not the proposal's 38 / 12 / 8). If the dataset cannot be read, the built-in demo book is used.
- **SOPs**: the 60 chunks are indexed into the SOP knowledge base in their own collection, tagged `source = "team_dataset"`. `sop_search` returns them as `team_hits` beside the house SOPs' `hits`.
- **Past incidents**: `warroom.data.team_dataset.IncidentStore` (read-only). No agent tool uses it yet.

## Known differences from our model

- **M/L presses**: in the dataset, V08–V11 are the medium/large presses and V01–V07 make small tires only, so its EV-2 tire S-027 is a small tire (24 h cure, 864 min done). Our `warroom/factory.py` fixes CURE-07..10 as M/L, and page 11 has press 7 curing a medium tire (28 h, 10 h done). The id map still goes V07 → CURE-07.
- **Steam pressure** is bar(abs) in the dataset (2.5 at 128 °C, 1.8 at 119.5 °C). Our model uses a gauge scale (5.0 kgf/cm² at 128 °C). The replay re-scales it linearly from the dataset's own two steady states (`steam_from_bar_abs`).
- **Baselines**: in the dataset A03 runs at 92 °C on 38 L/min of water, and A06 at 45 °C. Our sim's nominals are 100 °C / 40 L/min and 35 °C.
- **Shifts**: in the dataset every team runs 08–17 plus 20–05. Our optimised premise (page 6) has the curing team on 06–15 / 14–23.
- **Staff support**: in the xlsx input sheet 9_員工 nobody in S01–S16 can support another team. In `staff.csv`, S15 (加硫組 night) can support 貼合組, and S17–S20's support teams are blank (the input sheet names them).
- **Dates**: EV-1 is 2026-10-22, the same day as our page-2 scenario. EV-2 is 2026-10-28, while our page-11 scenario runs on 2026-10-03.
