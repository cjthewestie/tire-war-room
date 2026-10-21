# p.11 on the real model after the fix (Mac, 2026-10-22)

Mac, Ollama on :11435, `nemotron-3.5-lightning:30b-a3b-nvfp4`. One preview on :8260 (`LLM_MODE=llm`, branch `cj/fix-p11-h2`), driven by `p11_drive.py`: load `press7_temp_drop` with preroll, speed 10, approve the recommended option on every card. Time is wall seconds from the scenario load.

| run | H1 rejected at | H2 source | reached H2 card | H2 card at | end |
|---|---|---|---|---|---|
| 1 | 119.5 °C | model | yes | 145.0 s | closed (7 號停用) |
| 2 | 119.5 °C | model | yes | 153.9 s | closed |
| 3 | 119.5 °C | model | yes | 145.8 s | closed |
| 4 | 119.5 °C | model | yes | 150.4 s | closed |
| 5 | 119.5 °C | model | yes | 142.8 s | closed |

In all 5 runs the model's H1 test was `≥123 °C`; code pinned it to the cure band `≥121 °C` (audit `expectation_bounded … (cure band)`), so H1 fell at 119.5 °C as on p.11. The model did not repeat H1 in these runs, so the retry and the rule fallback did not fire (both are covered by `tests/test_p11_replan_repeat.py`).

`before-fix/summary-no-pin-no-bind.json`: the same 5 runs one step earlier (feedback + retry only). All 5 reached an H2 card, but H1 fell at 122 °C right away (the `≥123` test) and 3 of 5 then safe-stopped, because the model's H2 re-solved without CURE-09 (the system prompt's example press) instead of CURE-07. Both are fixed in `_bound_expectation` (cure band) and `_bind_takeout`.

Files: `runs/runN-drive.log` (what the driver saw), `runs/runN-audit.jsonl` (the audit rows of that run), `runs/summary.json`.
