// Unit checks (no server, no browser) for the shared AI status line (aiStatus.ts), 修復預估 (repairEstimate), the SOP
// pick (sop.ts) and 「怎麼選」 (optionGuide.ts), on real server data captured in fixtures/status-cases.json (mock mode:
// press7_temp_drop = p.11 with round 1 B / round 2 A, and concurrent_3).
// Run: cd web && node e2e/status-unit.mjs     (vite loads the TS modules directly, import.meta.glob included)
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
const fx = JSON.parse(readFileSync(resolve(here, 'fixtures/status-cases.json'), 'utf8'))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false } })
const load = (p) => vite.ssrLoadModule(p)
const { aiStatusParts, repairEstimate } = await load('/src/aiStatus.ts')
const { loadSopSteps } = await load('/src/sop.ts')
const { optionGuide } = await load('/src/optionGuide.ts')

/** The useWarRoom state the helpers read, from one captured snapshot. */
const stateOf = (s, patch = (t) => t) => ({
  clock: { factory_time: s.now }, snapshot: { header: { factory_time: s.now }, events: s.events, machines: [] },
  world: { tasks: { items: s.tasks.map(patch) } }, subagents: null,
  pending: s.approvals.filter((a) => a.status === 'pending'),
  resolved: s.approvals.filter((a) => a.status !== 'pending').reverse(),
})
const evOf = (s, tail) => s.events.find((e) => e.id.endsWith(tail))
const line = (p) => [p.lead, p.who, p.phase, p.eta].filter(Boolean).join('｜')

// ---------------------------------------------------------------- D1: closed is 已恢復正常 only when verified
{
  const s = fx.p11_end
  const ev = evOf(s, 'CURE-07')
  const p = aiStatusParts(ev, stateOf(s), null)
  check('p.11 round 2 closed, 停用檢修 running → Agent Nemo 已派人維修｜維修中（0/1）', p.who === 'Agent Nemo 已派人維修' && p.phase === '維修中（0/1）', line(p))
  check('… keeps the repair ETA (停用檢修 03:03)', /03:03 完成/.test(p.eta), p.eta)
  check('… what = the detection, not 「7 號機停用，重排中」: 偵測到：7 號硫化機溫度 128 → 122°C (no forecast, no 「AI」)', p.what === '7 號硫化機溫度 128 → 122°C' && p.lead === '偵測到：7 號硫化機溫度 128 → 122°C', p.lead)
  const fixed = aiStatusParts(ev, stateOf(s, (t) => (t.kind === '停用檢修' ? { ...t, status: '完成', done_at: '2026-10-03T03:03:04+08:00' } : t)), null)
  check('… 停用檢修 done → 已停用該機，排程已調整', fixed.phase === '已停用該機，排程已調整' && !fixed.eta, line(fixed))
  // a screen opened later (no cards in its store) says the same
  const late = aiStatusParts(ev, { ...stateOf(s), resolved: [], pending: [] }, null)
  check('… same text on a screen opened later', late.what === p.what, late.what)
}
{
  const s = fx.c3_closed
  const mill = aiStatusParts(evOf(s, 'B-OPEN'), stateOf(s), null)
  check('concurrent_3 B 線開煉機 closed, 檢修 running → 維修中（1/2）', mill.phase === '維修中（1/2）', line(mill))
  check('… ETA from the 檢修 task (10:34)', /10:34 完成/.test(mill.eta), mill.eta)
  const press = aiStatusParts(evOf(s, 'CURE-03'), stateOf(s), null)
  check('concurrent_3 3 號 verified (H1 成立), no fast repair → 3 號硫化機已恢復正常 (no value: it held at 123°C)', press.lead === '3 號硫化機已恢復正常' && !press.who && !press.phase && !press.eta, line(press))
}

// ---------------------------------------------------------------- D2: 修復預估 counts the newest card's tasks only
{
  const s = fx.p11_r2_pending
  const ev = evOf(s, 'CURE-07')
  const est = repairEstimate(ev, stateOf(s))
  check('round-2 card pending: round-1 tasks (宣達 28.5 h, 調派支援) left out', est.tasks.length === 0, est.tasks.map((t) => t.kind).join('、'))
  check('… no 「預計完成」 from the never-assigned 調派支援 (no verification window for H2)', est.doneBy == null && est.decided == null)
  const sop = await loadSopSteps('CURE-07', ev)
  check('… SOP = SOP-CP-01 硫化中斷處置 (H2 「依 SOP「硫化中斷處置」」)', sop?.sop_id === 'SOP-CP-01' && sop.steps.length === 7, `${sop?.sop_id} ${sop?.title} ${sop?.steps.length} 步`)
  // no SOP named anywhere: a press taken out still gets CP-01 without asking the search
  const bare = { ...ev, plan: { ...ev.plan, hypothesis: { ...ev.plan.hypothesis, evidence: [] }, rationale: '' } }
  const orig = globalThis.fetch
  let searched = 0
  globalThis.fetch = async () => { searched++; return { ok: true, json: async () => [] } }
  const sop2 = await loadSopSteps('CURE-07', bare)
  globalThis.fetch = orig
  check('… machine_out without a named SOP → SOP-CP-01, no search', sop2?.sop_id === 'SOP-CP-01' && searched === 0, `${sop2?.sop_id} searched=${searched}`)
}
{
  const s = fx.p11_r1_pending
  const ev = evOf(s, 'CURE-07')
  const orig = globalThis.fetch
  const urls = []
  globalThis.fetch = async (u) => { urls.push(String(u)); return { ok: true, json: async () => [{ sop_id: 'SOP-CP-02', title: '硫化機溫度下降處置', text: '', score: 1 }] } }
  const sop = await loadSopSteps('CURE-07', ev)
  globalThis.fetch = orig
  check('round 1 (H1 open, no machine out) still uses the search → SOP-CP-02', sop?.sop_id === 'SOP-CP-02' && urls.length === 1, sop?.sop_id)
  const est = repairEstimate(ev, stateOf(s))
  check('round-1 card pending: window runs from now (現在核准的話)', est.decided == null && est.doneBy === new Date(s.now).getTime() + 30 * 60_000)
}
{
  const s = fx.p11_end
  const ev = evOf(s, 'CURE-07')
  const est = repairEstimate(ev, stateOf(s))
  const kinds = est.tasks.map((t) => t.kind)
  check('round 2 approved: only round-2 tasks (+ 撤回 notices), no round-1 宣達 / 設定硫化時間', !kinds.includes('設定硫化時間') && kinds.includes('停用檢修'), kinds.join('、'))
  check('… decided = the round-2 decision 02:32:02', est.decided === '2026-10-03T02:32:02+08:00', est.decided)
  const fresh = repairEstimate(ev, { ...stateOf(s), resolved: [] })
  check('… a screen opened after the decision: the card\'s first task stands in (not 「現在核准的話」)', fresh.decided === '2026-10-03T02:32:03+08:00', fresh.decided)
  // 10 more minutes with the 品管 task still 待派: it no longer drives the ETA
  const later = { ...s, now: '2026-10-03T02:50:00+08:00' }
  const e2 = repairEstimate(ev, stateOf(later, (t) => (t.kind === '停用檢修' ? { ...t, status: '完成' } : t)))
  check('… a task 待派 > 10 min is left out of the ETA', e2.lastEta !== s.tasks.find((t) => t.kind === '品管').eta, `lastEta=${e2.lastEta}`)
}

// ---------------------------------------------------------------- D3: 「怎麼選」 says what fixes it and why B
{
  const s = fx.p11_r1_pending
  const card = s.approvals.find((a) => a.round === 1)
  const g = optionGuide(card, evOf(s, 'CURE-07'))
  check('round 1 解除異常靠 = cure extension + verification', g?.fix === '延長 7 號機硫化到 28.5 h，約 30 分鐘內驗證是否回到 ≥121°C', g?.fix)
  check('round 1 rec gives the reason and the safer choice', g?.rec === '系統建議 B：排程求解器挑的方案；交期相同，想降低風險可改選 A', g?.rec)
}
{
  const s = fx.p11_r2_pending
  const card = s.approvals.find((a) => a.round === 2)
  const g = optionGuide(card, evOf(s, 'CURE-07'))
  check('round 2 解除異常靠 = machine out, one line', g?.fix === '7 號機停用交維修，其他模次改由別台', g?.fix)
}

await vite.close()
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
