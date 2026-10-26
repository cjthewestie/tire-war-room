# tire-war-room — instructions for any Claude working in this repo

Teammates drive Claude with short Chinese requests. This file holds the team rules; `CONTRIBUTING.md` was deleted (CJ, 2026-10-26: 「contributing 刪掉」). Setup and run steps are in `README.md`. Reply to teammates in Traditional Chinese, plain words, one step at a time.

## Always

- Work on an issue: open one with `gh issue create` (template 前端體驗改進 for UX work) before changing code, unless the request names an existing issue. Docs-only changes (README and similar) need no issue (CJ, 2026-10-26, on the README rewrite: "dont need issue for this").
- Branch `座位/issue號-簡述` (teammates: `d/<n>-<slug>`); never commit or push to `main`.
- Teammates change the front end only: `web/`. Anything in `warroom/`, `deploy/`, `bench/` or `warroom/contracts.py` → open an issue for CJ instead of editing. CJ, 2026-10-18: 「我希望他們改的是前端客戶體驗」.
- Before a PR: `cd web && pnpm build && pnpm lint`, then `uv run pytest -q tests/test_web.py tests/test_ui_state.py tests/test_contracts.py`; all green. Attach before/after screenshots at 1440 and 390 width (headless browser).
- PR body: what changed, `Closes #<n>`, the test output, the screenshots.

## Never

- Add any tool or code path that can move a machine; writes only through the Action Gate (`warroom/agent/gate.py`).
- Import agent, tools, api or an LLM client into `warroom/safety/`.
- Commit `data/runtime/`, `.env`, keys or the supervisor PIN.
- Put a number in docs or slides without a file in `bench/results/`, labelled Mac or DGX Spark.
- Merge your own PR.

## Data

- `data/team_dataset/` is the team's official dataset (synthetic; replay + reference data). CJ, 2026-10-20: 「這份資料是隊友做的，當作團隊正式資料集」. Dataset machine codes map to ours only through `warroom/data/team_ids.py`; read `data/team_dataset/README.md` before using it.

## Notes for CJ's sessions

- Keep this file, `.github/` templates and `docs/onboarding/` in step when the workflow changes.

- Work dispatch: from now on, send work through `/dispatch` and report results only; do not run long work in CJ's main session (CJ, 2026-10-20: 「從現在開始都用/dispatch 派發工作不要佔用我這邊主頁面」「回報結果就好了」).
