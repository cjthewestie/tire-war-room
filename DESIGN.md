# Design — 實心胎智慧製造戰情室

The visual system as shipped in `web/`, recorded from the code (tokens in `web/src/index.css` `:root`). It is the incumbent look of the war room (NVIDIA-green accent on calm neutrals) with two themes. CJ, 2026-10-20: 「顏色風格改回原本多加亮色暗色切換鈕」, then 「暗色不太好看」 — light is the default, dark stays available. Product context is in `PRODUCT.md`; the information layout follows the UX brief and its prototype (`docs/ux/war-room-ia-prototype.html`, v12).

## Themes

- **Light (default).** Near-white neutrals, dark ink, the green accent darkened for text and buttons.
- **Dark.** The original palette, unchanged except `--ink-faint` (raised from `#7f867e` to `#868d85` so it reaches 4.5:1 on `--panel-2`).
- Switch: one icon button (aria-label 「切換亮色／暗色」) at the right end of the 主控台 ticker and in the phone header. The choice is kept in `localStorage` under `warroom-theme` (read and written inside try/catch). With nothing stored the page is light; the system preference is not used. An inline script in `index.html` and `phone.html` sets `data-theme` before first paint, and `theme.ts` keeps it in sync and updates `<meta name="theme-color">`.
- Mechanism: `:root` holds the dark values; `:root[data-theme="light"]` overrides them. Every colour that used to be hard-coded below `:root` now reads a variable whose fallback is its original dark value (`var(--c-ffd0cb, #ffd0cb)`, `rgb(var(--alarm-rgb, 255 93 82) / .45)`), so the dark look is byte-for-byte the old one and the light theme only defines variables. New code uses the tokens below, never a raw hex, and never a hard-coded white or black surface.

## Tokens

| Token | Light | Dark | Job |
|---|---|---|---|
| `--bg` | `#f4f5f2` | `#0b0d10` | page ground |
| `--panel` | `#ffffff` | `#14171c` | cards, panels, the plan, the conversation |
| `--panel-2` / `--panel-3` | `#eef0eb` / `#e2e6de` | `#1c2027` / `#272c34` | insets, tracks, selected rows |
| `--border` | `#d9ddd5` | `#242930` | 1 px rules |
| `--ink` / `--ink-dim` / `--ink-faint` | `#1a1d1a` / `#4f564e` / `#5c645b` | `#eceeea` / `#adb3ab` / `#868d85` | text |
| `--accent` / `--accent-ink` / `--accent-soft` | `#427500` / `#fff` / `#e6f2d4` | `#76b900` / `#0d1400` / `#1e3309` | NVIDIA green: current tab, primary action (核准), done / running, selection |
| `--alarm` / `--alarm-soft` | `#c0352a` / `#fbe3e0` | `#ff5d52` / `#3a1512` | abnormal, safe stop, late, shortfall |
| `--warn` / `--warn-soft` | `#935e00` / `#fbefd3` | `#f5b83d` / `#382a0e` | waiting for approval, early warning |
| `--info` | `#1f6db3` | `#5db2f5` | AI working, links inside data, extra staff |
| `--line-a…d`, `--sz-s/m/l` | darker per hue | original | production lines and tyre sizes in charts |

Contrast (WCAG, measured on `--panel` and `--panel-2`): light ink 17.0 / 14.8, ink-dim 7.6 / 6.6, ink-faint 5.6 / 5.3, accent 5.6 / 4.9, alarm 5.6 / 4.8, warn 5.5 / 4.8, info 5.4 / 4.7, white on the accent button 5.6. Dark ink 15.4 / 14.0, ink-faint 5.3 / 4.8, accent 7.5 / 6.8, alarm 5.9 / 5.4, warn 10.1 / 9.2. The prototype's light values (`#4f8a00`, `#d63b2f`, `#a86a00`) were darkened one step because they fell below 4.5:1 as text.

## Type, space, shape

- One CJK UI face (`Noto Sans TC` / `PingFang TC` / `Heiti TC`); all numbers `font-variant-numeric: tabular-nums`.
- Radius 12 px for panels, 8–10 px for inner blocks, 999 px for pills. Gaps 12–16 px; `--gap` is 16 px (12 px under 600 px).
- Pills carry state in a soft fill with the strong colour as text (`.hl-pill`, `.state-pill`, `.chip`); colour never appears alone where a word can say it.

## Layout (UX brief, prototype v12)

- **Header:** product name, tabs 主控台 · 產線監控 · 人力排程 · 訂單 · Agent · 狀態機 · 後台 (工廠全貌 merged into 產線監控, issue #37), factory time, network / LLM status. No extra buttons; demo controls stay in the 展示 strip (bottom sheet under 1024 px).
- **主控台 (home; was 對話台):** a 產線監控 ticker (CJ, 2026-10-20: 「幫我設計成產線監控的跑馬燈好了」): fixed 正常 / 異常 / 待核准 counts on the left, then live plant items scrolling (per line, each curing press, output vs plan, next truck, materials, dock, staffing, latest closed event, order completions and shipments); alarm / warning items pinned first in their colour; hover or focus pauses it; an item opens 產線監控 on its machine or line; reduced motion shows a static wrapping row; theme toggle at the right end; at 390 px the counts sit above the ticker. Then three docked columns at ≥1100 px:
  - 核准區 list (260 px), ranked ① 安全／機台異常 ② 交期風險 ③ 插單／人力決定 ④ 執行中 ⑤ AI 已自行處理 (collapsed);
  - 計劃板 (the rest): a dotted-grid board of step cards S1…Sn joined by `depends_on` arrows (waiting cards outlined in `--warn`, cards a new version changed marked 「改」), then 對交期的影響, 核准後誰去做, the option picker, and **核准 / 討論** pinned at the bottom;
  - Agent 對話 (360 px), which follows the selected item or shows 全部.
  - 760–1100 px: list + plan side by side, conversation below. Under 760 px: list → plan → conversation. Empty state: 「全廠正常，沒有要你處理的事」 plus what happens next.
- **產線監控:** 預警 first, then the existing monitor. **人力排程:** 「今日計劃」 card with shift-load bars, then the existing schedule. **訂單:** search → one order with per-stage progress, location, blockers. **/phone:** notification → one decision card (核准 / 討論) → 問一句.

## Components added in this round

`HomeStatus`, `ApprovalList` + `homeItems.ts`, `PlanArea`, `PlanFlow board` (extends the approval card's flow diagram), `EarlyWarnings`, `TodayPlan`, `ShiftLoad`, `OrdersTab` + `OrderCard`, `ThemeToggle` + `theme.ts`. #21's `ProgressRing`, `TaskList` and `GoodsQueue` are used as they are (task progress in ④ items and under the plan after approval).

## States

- Waiting: amber pill and amber-outlined step cards; 核准 in the accent.
- Abnormal / late / short: alarm colour plus the word (異常, 會晚, 缺 n).
- Executing: task rings from #21 and 「任務 k/n」 in the list until every task is done.
- Network: weak / WAN down chips in amber, LAN down in red with the banner; approvals then go through the on-site touch screen (`via: touchscreen`), unchanged.
- Focus: 2 px accent outline; disabled buttons at 55 % opacity.
