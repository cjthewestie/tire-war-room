# Merge train 2026-10-22: what "theirs wins" discarded

CJ, 2026-10-22: 「有衝突的地方先用他們的先merge」 and 「我修的那些要留著我等等要比較」. Teammates' PRs merged first and won every conflict; the side that lost is kept here and in tags.

- `prN.md`: every conflict hunk of PR #N, both sides side by side, with the commits that touched the file on each side.
- `prN.diff`: `git diff <merged result> <our side> -- <conflicted files>`: what to re-apply to get our version back.
- Tags: `keep/prN-ours` = our PR head before the merge (`keep/pr107-ours-948b0cf` = #107's later head); `keep/main-before-N` = main just before PR #N merged.

Compare any file: `git fetch --tags && git diff keep/pr105-ours origin/main -- web/src/phone/Phone.tsx`

| PR | winner | what was lost |
|---|---|---|
| #112 (Lufi) | Lufi | main's 討論 scroll-input-into-view fix (ChatBar); the 「核准後才執行；要改，按「討論」」 hint (Lufi's deadline + 「核准後 Agent Nemo 派人執行並回報」 text instead); simplified subagent tag in the approval list. Chat-agent labels renamed to Agent Nemo. |
| #105 (ours) | main | `Runtime._awaits_decision` early return for ended (SAFE_STOP) events, so 交給現場處理 tasks may wait; #105's PlanFlow 計劃板 block in PlanArea; old 覆核 ReviewCard on the phone. Safety endpoints, phone safety/stop cards and alerts kept. |
| #111 (ours) | main | 「系統健康」 tab label (main's 「狀態」); 稽核紀錄 / 系統參數 tabs kept. |
| #107 (ours) | main | demo speed buttons incl. 40× (main removed the speed row in #100); our approval-list row layout. return-to-service endpoint and 預檢 hint kept. |
| #114 (ours) | main | 30× speed button; #114's own copy of the morning_rush scenario/test/mock (main has #107's merged version). ▶ Demo button and demo guards kept. |
| #121 (ours) | main | old 覆核 banner wording only; demo banner and 現在核准 cue kept. |
