# Design — 台鐵發車時刻表

The committed visual world of the war room. CJ chose it on the impeccable decision page on 2026-10-20 (Operate mode, code-led build). Product context lives in `PRODUCT.md`; tokens live in `web/src/index.css` `:root`.

Thesis: every event, plan and order is set like a train on a station departure board — 時間｜機台｜狀態｜下一步｜負責人 — so the operator reads the whole plant in one pass. The world lends type, palette, density and one signature move. Navigation, layout and controls stay standard web components: tabs, buttons, inputs, tables, chips.

## Palette

| Token | Value | Job |
|---|---|---|
| `--station` | `#0b2a4a` | Shell: header, bottom nav, phone header, the departure board's ground, supervisor chat bubbles, selected option |
| `--station-2` / `--station-3` | `#123659` / `#24507c` | Presenter sub-bar and active tab; board rules, links, tool icons |
| `--station-ink` / `--station-dim` | `#f4f1ea` / `#a8bdd3` | Text on blue (dim is 7.9:1) |
| `--paper` | `#f4f1ea` | Station white: conversation, cards, panels — every working surface |
| `--paper-2` / `--paper-3` / `--rule` | `#ebe6dc` / `#dcd5c7` / `#d2cabb` | Insets, tracks, 1 px rules |
| `--ground` | `#e3ded3` | Page ground between surfaces |
| `--ink` / `--ink-dim` / `--ink-faint` | `#1b2531` / `#4c5763` / `#66707a` | Text; never pure black. 13.9 / 6.9 / 4.6 : 1 on paper |
| `--orange` | `#f29a1f` | Arrival orange, the one accent: the most urgent board row, the current stage, "now" on the Gantt, focus rings, the primary action (核准, 送出). Text on it is `--orange-ink` |
| `--ok` / `--ok-text` / `--ok-soft` | `#2e9e5b` | State only: running, done, approved |
| `--bad` / `--bad-text` / `--bad-soft` | `#d63b2f` | State only: abnormal, blocked, safe stop, shortfall |
| `--wait` / `--wait-text` / `--wait-soft` | `#e2b93b` | State only: waiting for approval. Amber, distinct from the orange accent |

Rules (raised lines, kept as written):

- No decorative colour. Line A–D colours, the NVIDIA green and the purple "replan" tint are gone; tyre sizes on the Gantt are three labelled blue tints.
- Colour lives only inside bounded areas: chips, the board, the approval card, charts, bars. Running text is always ink.
- Severity is also size and weight, never colour alone: board rows step 17 px bold (critical: safe stop, R3 approval, critical anomaly) → 15 px semibold (needs attention) → 14 px regular dim (closed).
- Legacy token names (`--panel`, `--accent`, `--alarm`, `--warn`, `--info`…) are mapped onto these, so older and new components inherit the world. New components use the tokens, never a raw hex.

## Type

- UI: the platform CJK sans (`--font-ui`: PingFang TC, Noto Sans TC / Noto Sans CJK TC on the DGX box). Traditional Chinese copy unchanged.
- Numbers: IBM Plex Mono 500/600, self-hosted from `@fontsource/ibm-plex-mono` (latin subset, works offline) as `--font-num`, for times, clock, board times, KPI values, tiles, the timeline, shift hours. The whole page also sets `font-variant-numeric: tabular-nums`.
- Headings carry their own weight; no eyebrow labels. Board column heads are 12 px, letter-spaced .16em, dim on blue.

## Space

One 8 px module (`--u`). Paddings, gaps, row heights (board rows 40/48 px, column heads 32 px, shift rows 32 px), buttons (40 px desktop, 56 px phone) and radii (4 / 8 px) are multiples of it. The board's columns (56 | 96 | 112 | 1fr | 80, 16 px gutters) are the column lines the approval card and conversation align to.

## Components

- **Header** (`.topbar`): station blue, full bleed. Logo mark (drawn SVG), product name, tabs as plain tabs with a 4 px orange underline on the current one (Agent 對話台 first), then network tier chip, factory clock in Plex Mono, LLM status. On the 對話台 tab the plant counts move into the board header.
- **Presenter sub-bar** (`.demo-sheet`): a 40 px `--station-2` strip under the header on desktop; a bottom sheet below 1024 px.
- **Departure board** (`DepartureBoard.tsx`, row model in `boardRows.ts`): blue ground; header button = title + 正常/異常/待核准 counts + 產線監控 → (clicking opens #monitor, `data-testid=status-strip`). One row per event: pending approval (R3 before R2, earliest deadline) → safe stop → open events newest first → two most recent closed. The top row is orange. A selected row has a 2 px inset outline; clicking a row selects its event, and the approval card below shows it.
- **Status chips on the board** (`.bstat`): amber 待核准, red 安全停止, soft green 執行/驗證, blue 規劃中, outlined 結案.
- **Conversation** (`.console`): paper surface. Supervisor bubbles are blue with station-white text; agent replies sit on paper with a 1 px blue rule; thinking blocks on `--paper-2`; tool rows are 1 px ruled; gate and approval rows use the state soft fills.
- **Approval card** (`.apv`): paper, 4 px amber top rule (waiting), numbered blocks with square blue numerals, the decision bar pinned to the bottom of the rail with 核准 in orange. The phone uses the same rules at hero size (24 px title, 56 px buttons).
- **Shift capacity bars** (`ShiftLoad.tsx`, 排程與人力 ⑧): timetable rows 班組｜班別｜時段 (hours)｜人力. Blue fill = people on shift up to what the shift needs, lighter blue = extra, red hatched overflow = shortfall with a 「缺 n」 chip.
- **Phone** (`/phone`): blue header (主管手機, factory time, LAN status), up to three board rows as a summary, then the approval card as the hero.
- **3D factory** (`/factory`): the in-game desk monitor, wall screen and phone are the real pages in iframes, so they carry this world; the HUD keeps its cream look but takes its state colours and toast accent from these tokens.

## Signature move — split-flap row update

When a board row's status, plan version or approval changes (`sig` in `boardRows.ts`), every cell of that row turns over character by character: each glyph rotates on the X axis from −96° (240 ms, ease-out-expo, slight overshoot), 50 ms apart, cells left to right, so the row reads like a train arriving on a station board (`Flap.tsx`, `flip.ts`). First render never flips. Under `prefers-reduced-motion: reduce` the text swaps instantly and the changed cells carry a 2 px orange underline for 1 s (ink on the orange row).

## States

- Calm: the board reads 「全廠正常・目前沒有事件」; recently closed rows stay dim at the bottom.
- Waiting: amber chips, amber top rule on the card, orange top row, orange 核准.
- Critical: red chips and red header chip; 17 px bold rows.
- Network: weak / WAN down → amber chip; LAN down → red chip, red banner, decisions go to the touch screen.
- Disabled buttons fall to `--paper-3` with faint ink; focus is a 2 px orange ring everywhere; selection is orange; scrollbars are thin paper-3.

## Honest risk

A blue board can slide back into a dark monitoring wall. The white conversation surface, the light page ground and the strict column rhythm keep it a working screen; keep new panels on paper, not blue.
