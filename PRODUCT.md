# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary:** the on-site supervisor and the war-room operator of a small or medium solid-tyre factory. The operator watches the big war-room screen and talks to the agent; the supervisor walks the plant and approves risky actions from a phone (CJ, 2026-10-20, confirmed).
- **Secondary:** hackathon judges at the 2026-10-23 finals, and later factory owners evaluating the product. They are shown the system, they don't operate it day to day.

## Product Purpose

An edge agent harness for factories. It watches every machine, order and shift, opens an event when something goes wrong, has a local model propose a plan, checks it with code, and asks the supervisor to approve anything that changes production. Success: a problem is noticed in about a minute instead of two hours, deliveries hold, and every decision is auditable.

## Positioning

Edge, offline and controllable: the model only proposes, code decides, the supervisor approves, and every step is in a hash-chained audit log. It runs on one box in the plant with no internet (CJ, 2026-10-20, confirmed). A general chat agent cannot honestly claim this, because its model chooses and acts freely.

## Operating Context

- Big screen in a glass war room, plus a phone carried around the plant, in the break room and the toilet.
- The 主控台 (was Agent 對話台; renamed by CJ, 2026-10-20: 「這個 page 改成主控台名字」) is the main page and the dashboard is secondary (CJ, 2026-10-20: 「我覺得對話台應該是主要頁面 儀表板次要」).
- Network tiers: normal, weak, WAN down (no change), LAN down (approve on the on-site touch screen).
- Traditional Chinese UI; factory vocabulary (硫化機, 開煉機, 模, 班表).

## Capabilities and Constraints

- 28 machines on lines A–D, 貼合 and 11 curing presses; per-second sensor data; event detection without the LLM; a separate Safety Core the model can't call.
- One local model (Nemotron 3.5 Lightning 30B-A3B), 9 allow-listed tools (6 read/compute, 3 write), Action Gate R0–R3, phone approval, plan discussion and supervisor tasks, read-only Q&A.
- Whole-plant live world data: orders, materials, dock, people, energy, quality, maintenance.
- Stack: React + Vite in `web/`, pnpm; served by the FastAPI app. Pages: `/` (console first), `/phone`, `/factory` (3D).
- Must work at 390 px phone width and on a large wall screen.

## Evidence on Hand

- Team official synthetic dataset (CJ, 2026-10-20: 「這份資料是隊友做的，當作團隊正式資料集」).
- Measured numbers in README "Measured numbers vs targets" (Mac stand-in; DGX Spark numbers pending). Never show Spark numbers that weren't measured.
- No real customers, testimonials or pricing exist; don't invent them.

## Product Principles

1. Show the reasoning: what the agent saw, which tools it used, what it concluded.
2. Make the human's decision point unmistakable: what needs approval, why, and what changes.
3. Numbers come from tools, never from the model's words.
4. Calm when normal, unmistakable when not.
5. Works offline and on a phone in the plant.
