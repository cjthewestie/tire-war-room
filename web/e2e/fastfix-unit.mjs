// Unit checks (no server, no browser) for the fast-repair front end (Kyle 10-21 flow: 觸發異常 → 主管核准 → 派人維修 →
// 維修完成通知 → 回溫 → 綠燈 → 異常報告): the AI status line's 核准後約 N 秒維修完成 / 維修中 / 維修完成・溫度回降中 /
// 已恢復正常｜Agent Nemo 異常報告已產生 / 主管已讀報告, the 3D ring of a repair task and its 回溫中 / 已恢復, the phone's 維修完成 and 報告已產生 notices, the Agent
// page's task, 已恢復正常 and 報告 rows, and the report's 維修完成 / 恢復正常 / 主管已讀 lines. Built on
// fixtures/status-cases.json (concurrent_3, B 線開煉機 overheat: ≤118°C) with the Event.repaired_at / recovered_at / review
// fields of the shared contract patched in.
// Run: cd web && node e2e/fastfix-unit.mjs
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
const { aiStatusParts } = await load('/src/aiStatus.ts')
const { machineRing, ringRecoveries, recoveryKey } = await load('/src/factory3d/machineRing.ts')
const { alertText, diffPending, repairNotices } = await load('/src/phone/alert.ts')
const { fromAudit } = await load('/src/agent/transcript.ts')
const { buildReport } = await load('/src/report/build.ts')

const s = fx.c3_closed
const T = (iso) => new Date(iso).getTime()
const iso = (ms) => new Date(ms).toISOString().replace('Z', '+00:00')
const base = s.events.find((e) => e.id.endsWith('B-OPEN'))
const stateOf = (events, tasks, now = s.now) => ({
  clock: { factory_time: now, speed: 10 }, snapshot: { header: { factory_time: now }, events, machines: [] },
  world: { factory_time: now, tasks: { items: tasks } }, subagents: null, pending: [], resolved: [],
})
const line = (p) => [p.lead, p.who, p.phase, p.eta].filter(Boolean).join('｜')
// a fast repair: 檢修 assigned at once, 30 factory seconds to go (3 wall s at 10×)
const now = T(s.now)
const repair = {
  ...s.tasks.find((t) => t.id === 'TK-0001'), status: '執行中', assigned_at: iso(now - 20_000), started_at: iso(now - 20_000),
  eta: iso(now + 30_000), progress: 0.2, done_at: null,
}
const slow = { ...s.tasks.find((t) => t.id === 'TK-0002'), progress: 1 }

// ---------------------------------------------------------------- AI status line
{
  const held = { ...repair, status: '待派', assigned_at: null, started_at: null, progress: 0 }
  const wait = { ...base, state: 'await_approval', closed_at: null, outcome: null }
  const a = aiStatusParts(wait, stateOf([wait], [held]), null, 12)
  check('card waiting, 檢修 held 待派 → 偵測到…｜Agent Nemo 建議派人維修｜待核准｜核准後約 12 秒維修完成 (REPAIR_FAST_S, not the verification window)',
    a.lead.startsWith('偵測到：B 線開煉機 膠溫') && a.who === 'Agent Nemo 建議派人維修' && a.phase === '待核准' && a.eta === '核准後約 12 秒維修完成', line(a))
  check('… the detection stops at its first clause (no forecast, no 水流 clause)', !/[，；]|預測/.test(a.what), a.what)
  check('… NemoClaw 沙箱 at the end of the Agent Nemo part when the runtime is set',
    aiStatusParts(wait, stateOf([wait], [held]), 'NemoClaw 沙箱', 12).who === 'Agent Nemo 建議派人維修（NemoClaw 沙箱）')
  const ev = { ...base, state: 'verify', closed_at: null, outcome: null }
  const p = aiStatusParts(ev, stateOf([ev], [repair, slow]), null)
  check('verify + 檢修 running → Agent Nemo 已派人維修、放慢投料｜維修中（1/2）', p.who === 'Agent Nemo 已派人維修、放慢投料' && p.phase === '維修中（1/2）', line(p))
  check('… ETA from the repair task (30 s), not the 20-minute window', /約 30 秒/.test(p.eta), p.eta)
  const fast = aiStatusParts(ev, stateOf([ev], [repair, slow]), null, 12)
  check('… with REPAIR_FAST_S (wall clock): 約 10 秒後維修完成 (20 % of 12 s done)', fast.eta === '約 10 秒後維修完成', fast.eta)
  const done = { ...repair, status: '完成', done_at: iso(now), progress: 1 }
  const warm = { ...base, state: 'verify', closed_at: null, outcome: null, repaired_at: iso(now), recovered_at: null, review: null }
  const w = aiStatusParts(warm, stateOf([warm], [done, slow]), null)
  check('repaired, not closed (overheat ≤118°C) → 維修完成・溫度回降中, no ETA', w.phase === '維修完成・溫度回降中' && !w.eta, line(w))
  const cold = { ...warm, plan: { ...warm.plan, hypothesis: { ...warm.plan.hypothesis, expectation: { ...warm.plan.hypothesis.expectation, op: '>=', value: 121 } } } }
  check('… a temperature drop (≥121°C) → 維修完成・溫度回升中', aiStatusParts(cold, stateOf([cold], [done, slow]), null).phase === '維修完成・溫度回升中')
  const closed = { ...base, repaired_at: iso(now), recovered_at: iso(now + 4000), review: { status: 'pending', requested_at: iso(now + 4000), done_at: null, by: null }, ai_report: null }
  const drafting = aiStatusParts(closed, stateOf([closed], [done, slow]), null)
  check('closed, review pending, Event.ai_report not written yet → …已恢復正常（94°C）｜Agent Nemo 異常報告整理中 (not 已產生)',
    drafting.lead === 'B 線開煉機已恢復正常（94°C）' && drafting.phase === 'Agent Nemo 異常報告整理中', line(drafting))
  const report = { text: '…', summary: '…', by: 'rules', drafted_ms: null, at: iso(now + 4000), late_ms: null }
  const q = aiStatusParts({ ...closed, ai_report: report }, stateOf([closed], [done, slow]), null)
  check('closed + report unread → B 線開煉機已恢復正常（94°C）｜Agent Nemo 異常報告已產生, no detection, no ETA',
    q.lead === 'B 線開煉機已恢復正常（94°C）' && !q.who && q.phase === 'Agent Nemo 異常報告已產生' && !q.eta, line(q))
  const r = aiStatusParts({ ...closed, review: { ...closed.review, status: 'done', done_at: iso(now + 5000), by: 'supervisor' } }, stateOf([closed], [done, slow]), null)
  check('report read → …已恢復正常（94°C）｜主管已讀報告', r.lead === q.lead && r.phase === '主管已讀報告', line(r))
}

// ---------------------------------------------------------------- 3D ring
{
  const world = (tasks, at = s.now) => ({ factory_time: at, tasks: { items: tasks }, maintenance: { in_progress: [] } })
  const tile = { machine_id: 'B-OPEN', status: 'alarm', step_progress: null }
  const r0 = machineRing('B-OPEN', tile, world([slow, repair]), now, 10)
  // assigned 20 s ago, ETA in 30 s: 40 % by the clock, over the server's 20 % from the last world step
  check('repair running: ring follows the clock between world steps', r0.kind === 'task' && Math.abs(r0.progress - 0.4) < 0.01, `${r0.label} ${r0.progress}`)
  const late = machineRing('B-OPEN', tile, world([repair]), now + 60_000, 10)
  check('… held under done until the server says 完成', late.progress === 0.99 && late.label === '檢修 99%', late.label)
  const done = { ...repair, status: '完成', done_at: iso(now + 30_000), progress: 1 }
  const g = machineRing('B-OPEN', { ...tile, status: 'running' }, world([done]), now + 32_000, 10)
  check('repair done 2 s ago (10×) → full green ring 檢修完成', g.kind === 'task' && g.progress === 1 && g.color === '#3fcf5a' && g.label === '檢修完成', `${g.label} ${g.color}`)
  const gone = machineRing('B-OPEN', { ...tile, status: 'running' }, world([done]), now + 30_000 + 70_000, 10)
  check('… back to the machine\'s own ring after the hold (6 wall s, at least 1 factory min)', gone.kind !== 'task', `${gone.kind} ${gone.label}`)
  // the gate's R1 檢修 finishes while the card waits; the fix lands only after 主管核准: never green over a red machine
  const landing = machineRing('B-OPEN', tile, world([done]), now + 31_000, 10)
  check('repair done, machine still red: 100% in its own colour for the landing moment', landing.label === '檢修 100%' && landing.color !== '#3fcf5a', `${landing.label} ${landing.color}`)
  const red = machineRing('B-OPEN', tile, world([done]), now + 30_000 + 25_000, 10)
  check('… then the alarm ring again, no 檢修完成 over a red machine', red.kind === 'alarm', `${red.kind} ${red.label}`)
  const other = { ...repair, id: 'TK-9', kind: '補料', status: '執行中' }
  const pick = machineRing('B-OPEN', tile, world([other, repair]), now, 10)
  check('a repair task wins over another task on the same machine', pick.label.startsWith('檢修'), pick.label)
  // Kyle 10-21 flow: after repaired_at the readings ramp back (RECOVER_FAST_S wall s): amber 回溫中 filling, then 已恢復
  const warm = { ...base, state: 'verify', closed_at: null, repaired_at: iso(now + 30_000), recovered_at: null, review: null }
  const wall0 = 1_000_000
  const recs = (evs, seen, wall, at = now + 32_000) => ringRecoveries(evs, seen, wall, 4, at, 10)
  const rw = recs([warm], { [recoveryKey(warm, 'recovering')]: wall0 }, wall0 + 2000)
  check('repaired 2 wall s ago (4 s ramp) → recovering ~50 %', rw['B-OPEN']?.phase === 'recovering' && Math.abs(rw['B-OPEN'].frac - 0.5) < 0.01, JSON.stringify(rw['B-OPEN']))
  const amber = machineRing('B-OPEN', { ...tile, status: 'warning' }, world([done]), now + 32_000, 10, rw['B-OPEN'])
  check('… ring 回溫中, amber, not the green 檢修完成', amber.kind === 'recover' && amber.label === '回溫中' && amber.color === '#f5b83d' && amber.progress === 0.5, `${amber.label} ${amber.color} ${amber.progress}`)
  const cure = { ...repair, id: 'TK-8', kind: '設定硫化時間', status: '執行中' }
  const over = machineRing('B-OPEN', { ...tile, status: 'warning' }, world([done, cure]), now + 32_000, 10, rw['B-OPEN'])
  check('… 回溫中 over another task of the plan (設定硫化時間) on the same machine', over.kind === 'recover' && over.label === '回溫中', `${over.kind} ${over.label}`)
  const fresh = recs([warm], {}, wall0)
  check('… a phase not recorded yet counts as just seen (no green flash first)', fresh['B-OPEN']?.phase === 'recovering' && fresh['B-OPEN'].frac < 0.1)
  const back = { ...warm, state: 'closed', closed_at: iso(now + 34_000), recovered_at: iso(now + 34_000), review: { status: 'pending', requested_at: iso(now + 34_000), done_at: null, by: null } }
  const rg = recs([back], { [recoveryKey(back, 'recovered')]: wall0 }, wall0 + 3000, now + 64_000)
  const green = machineRing('B-OPEN', { ...tile, status: 'running' }, world([done]), now + 64_000, 10, rg['B-OPEN'])
  check('recovered 3 wall s ago → green 已恢復', green.kind === 'recover' && green.label === '已恢復' && green.color === '#3fcf5a', `${green.label} ${green.color}`)
  const later = recs([back], { [recoveryKey(back, 'recovered')]: wall0 }, wall0 + 7000, now + 104_000)
  const own = machineRing('B-OPEN', { ...tile, status: 'running' }, world([done]), now + 104_000, 10, later['B-OPEN'])
  check('… after 6 wall s the machine\'s own ring again (no 檢修完成 after 已恢復)', own.kind !== 'recover' && own.kind !== 'task', `${own.kind} ${own.label}`)
  const old = recs([back], {}, wall0, now + 34_000 + 10 * 60_000)
  check('a recovery long past in factory time is not shown on a page opened later', !old['B-OPEN'])
}

// ---------------------------------------------------------------- phone notices
{
  const evs = ['B-OPEN', 'A-BIGOPEN', 'CURE-03'].map((m, i) => ({ ...base, id: `EV-${m}`, machine_id: m, state: 'verify', closed_at: null, outcome: null,
    repaired_at: iso(now + i * 1000), recovered_at: null, review: null }))
  const tiles = [{ machine_id: 'B-OPEN', temp_c: 124.26 }]
  const items = repairNotices(evs, tiles)
  check('repaired (3 concurrent) → 3 維修完成 notices, oldest repair first', items.length === 3 && items.every((a) => a.notice === 'repaired') && items[0].title === 'B 線開煉機', items.map((a) => a.title).join('、'))
  check('… the text carries the live reading and the normal value (the anomaly\'s baseline 94°C): 溫度回降中 124.3°C → 94°C', items[0].reason === '溫度回降中 124.3°C → 94°C', items[0].reason)
  // p.11 / steam_low: 128 → 122°C meets 「≥121°C」 already; the notice heads for the normal 128°C, not the expectation
  const drop = { ...evs[0], id: 'EV-CURE-07', machine_id: 'CURE-07', anomaly: { ...base.anomaly, machine_id: 'CURE-07', value: 122.07, baseline: 127.98 },
    plan: { ...base.plan, hypothesis: { ...base.plan.hypothesis, expectation: { ...base.plan.hypothesis.expectation, machine_id: 'CURE-07', op: '>=', value: 121 } } } }
  const dn = repairNotices([drop], [{ machine_id: 'CURE-07', temp_c: 125.94 }])[0]
  check('… a temperature drop (≥121°C) heads for the normal value: 溫度回升中 125.9°C → 128°C', dn?.reason === '溫度回升中 125.9°C → 128°C', dn?.reason)
  const sup = repairNotices([{ ...drop, anomaly: { ...drop.anomaly, baseline: 0 } }], [{ machine_id: 'CURE-07', temp_c: 125.94 }])[0]
  check('… no baseline (supervisor / plant event): the expectation\'s target, 溫度回升中 125.9°C → ≥121°C', sup?.reason === '溫度回升中 125.9°C → ≥121°C', sup?.reason)
  const st = { seen: new Set(), held: [] }
  const first = diffPending(st, items.slice(0, 1), true)
  check('one notice → rings once: 維修完成：B 線開煉機', first.fire.length === 1 && alertText(first.fire, false).title === '維修完成：B 線開煉機', alertText(first.fire, false).title)
  const more = diffPending(st, items, true)
  check('two more (3 concurrent) → only the new ones ring: 2 件維修完成', more.fire.length === 2 && alertText(more.fire, false).title === '2 件維修完成', alertText(more.fire, false).title)
  check('… and not again (the reading moving does not re-ring)', diffPending(st, repairNotices(evs, [{ machine_id: 'B-OPEN', temp_c: 120 }]), true).fire.length === 0)
  const closed = evs.map((e, i) => (i === 0 ? { ...e, state: 'closed', closed_at: iso(now + 4000), recovered_at: iso(now + 4000), outcome: '已修復：B 線開煉機 膠溫回到 100°C',
    review: { status: 'pending', requested_at: iso(now + 4000), done_at: null, by: null },
    ai_report: { text: '…', summary: '…', by: 'rules', drafted_ms: null, at: iso(now + 4000), late_ms: null } } : e))
  const drafting = repairNotices(closed.map((e, i) => (i === 0 ? { ...e, ai_report: null } : e)), tiles)
  check('closed, report not written yet (ai_report null) → no 報告已產生 notice yet', drafting.length === 2 && drafting.every((a) => a.notice === 'repaired'), drafting.map((a) => a.notice).join(','))
  const rep = repairNotices(closed, tiles)
  const ring = diffPending(st, rep, true)
  check('closed with its report → 已恢復正常，報告已產生：B 線開煉機 rings once', ring.fire.length === 1 && ring.fire[0].notice === 'report' && alertText(ring.fire, false).title === '已恢復正常，報告已產生：B 線開煉機', alertText(ring.fire, false).title)
  check('… its text: 溫度回到 94°C（10:32 維修完成）, no 「已修復：」 and no machine twice', alertText(ring.fire, false).body === '溫度回到 94°C（10:32 維修完成）', alertText(ring.fire, false).body)
  const read = repairNotices(closed.map((e, i) => (i === 0 ? { ...e, review: { ...e.review, status: 'done' } } : e)))
  check('a read report leaves the list', read.length === 2 && read.every((a) => a.notice === 'repaired'))
  const mixed = alertText([{ id: 'AP-1', title: '7 號機溫降', reason: 'x', risk: 'R2' }, items[0]], false)
  check('approval + notice together → 2 件待核准與通知', mixed.title === '2 件待核准與通知', mixed.title)
  const card = alertText([{ id: 'AP-1', title: '7 號機溫度下降 128→122.3°C', reason: '假設 H1：蒸汽壓不足，溫度會穩定', risk: 'R3' }], false)
  check('approval alert (phone push, 3D notice): no 「R3・」, 判斷一 for 假設 H1', card.title === '待核准：7 號機溫度下降 128→122.3°C' && card.body.replace(/\u2060/g, '') === '判斷一：蒸汽壓不足，溫度會穩定', `${card.title} / ${card.body}`)
}

// ---------------------------------------------------------------- Agent page 處理歷程
{
  let seq = 100
  const a = (kind, actor, payload, ts) => ({ seq: seq++, ts, kind, actor, event_id: base.id, payload, prev_hash: '', hash: '' })
  const t0 = now
  const at = (sec) => iso(t0 + sec * 1000)
  const rows = [
    a('state_transition', 'state_machine', { from: null, to: 'detected', reason: 'detector wake-up' }, at(0)),
    a('approval_decision', 'supervisor', { request_id: 'AP-1', decision: 'approve', option_id: 'A', via: 'phone' }, at(10)),
    a('task', 'state_machine', { created: [{ id: 'TK-0001', kind: '檢修', title: '檢修 B 線開煉機', machine_id: 'B-OPEN', status: '待派' }], from_step: 's3' }, at(11)),
    a('task', 'world', { task_id: 'TK-0001', status: '前往中', change: 'assigned', kind: '檢修', title: '檢修 B 線開煉機', worker: '陳志明' }, at(11)),
    a('task', 'world', { task_id: 'TK-0001', status: '執行中', change: 'started', kind: '檢修', title: '檢修 B 線開煉機', worker: '陳志明' }, at(12)),
    a('task', 'world', { task_id: 'TK-0001', status: '完成', change: 'done', kind: '檢修', title: '檢修 B 線開煉機', worker: '陳志明' }, at(14)),
    a('system', 'world', { msg: '維修完成，故障排除', machine_id: 'B-OPEN' }, at(14)),
    a('state_transition', 'state_machine', { from: 'execute', to: 'verify', reason: 'compare expectation with readings (code)' }, at(14)),
    a('system', 'world', { msg: '已恢復正常', machine_id: 'B-OPEN', value: 101.24 }, at(18)),
    a('system', 'verifier', { hypothesis: 'H1', verdict: 'confirmed', observed: 101.2 }, at(18)),
    a('state_transition', 'state_machine', { from: 'verify', to: 'closed', reason: '已修復：B 線開煉機 膠溫回到 101°C' }, at(18)),
    a('system', 'state_machine', { msg: '異常報告已產生' }, at(18)),
    a('system', 'supervisor', { msg: '主管已讀報告', note: '現場確認正常' }, at(30)),
  ]
  const items = fromAudit([...rows].reverse(), base)
  const seqd = items.map((i) => (i.k === 'task' ? `task:${i.phase}${i.who ? `@${i.who}` : ''}` : i.k === 'review' ? `review:${i.phase}` : i.k === 'state' ? `state:${i.to}` : i.k === 'note' ? 'note' : i.k))
  const want = ['state:detected', 'approval', 'task:created', 'task:assigned@陳志明', 'task:started@陳志明', 'task:done@陳志明', 'review:repaired',
    'state:verify', 'review:recovered', 'note', 'state:closed', 'review:report', 'review:read']
  check('處理歷程 rows in time order: 派工 → 前往中 → 執行中 → 完成 → 維修完成 → 驗證 → 已恢復正常 → 結案 → 異常報告已產生 → 主管已讀報告', JSON.stringify(seqd) === JSON.stringify(want), seqd.join(' '))
  const rep = items.find((i) => i.k === 'review' && i.phase === 'repaired')
  check('… 維修完成 names the machine', rep?.text === '維修完成，故障排除・B 線開煉機', rep?.text)
  const rec = items.find((i) => i.k === 'review' && i.phase === 'recovered')
  check('… 已恢復正常 with the machine and the value in the event\'s channel', rec?.text === '已恢復正常・B 線開煉機 101.2°C', rec?.text)
  check('… 主管已讀報告 keeps the note', items.at(-1)?.text === '主管已讀報告' && items.at(-1)?.note === '現場確認正常')
  const older = fromAudit([a('system', 'state_machine', { msg: '請主管覆核' }, at(15)), a('system', 'supervisor', { msg: '主管覆核' }, at(30))], base)
  check('… an older audit file (請主管覆核 / 主管覆核) reads as 異常報告已產生 / 主管已讀報告', older.map((i) => `${i.phase}:${i.text}`).join(',') === 'report:異常報告已產生,read:主管已讀報告', older.map((i) => `${i.phase}:${i.text}`).join(','))
  // the world assigns at once, so its 前往中 row is written before the state machine's 派工 row
  const swapped = fromAudit([rows[3], rows[2], rows[4]].map((r, i) => ({ ...r, seq: 500 + i })), base)
  check('派工 shown before 前往中 even when the audit wrote 前往中 first', swapped.map((i) => i.phase).join(',') === 'created,assigned,started', swapped.map((i) => i.phase).join(','))
  check('… 派工 text does not repeat the machine already in the title', swapped[0].text === '檢修 B 線開煉機', swapped[0].text)
  const old = fromAudit([a('system', 'world', { msg: '現場處置完成，故障排除', machine_id: 'B-OPEN' }, at(14))], base)
  check('… the runtime\'s older wording counts as 維修完成 too', old[0]?.k === 'review' && old[0].phase === 'repaired')
}

// ---------------------------------------------------------------- report
{
  const ev = { ...base, repaired_at: iso(now), recovered_at: iso(now + 4000), review: { status: 'done', requested_at: iso(now + 4000), done_at: iso(now + 60_000), by: 'supervisor' } }
  const r = buildReport({ ev, approvals: [], tasks: s.tasks, chainOk: true, auditCount: null })
  const head = Object.fromEntries(r.head.map((h) => [h.k, h.text]))
  check('report head: 維修完成 / 恢復正常 / 主管已讀 with seconds', /^10\/03 10:32:40$/.test(head['維修完成'] ?? '') && /^10\/03 10:32:44$/.test(head['恢復正常'] ?? '') && /^10\/03 10:33:40/.test(head['主管已讀'] ?? ''), JSON.stringify([head['維修完成'], head['恢復正常'], head['主管已讀']]))
  const p = buildReport({ ev: { ...ev, review: { ...ev.review, status: 'pending', done_at: null, by: null } }, approvals: [], tasks: s.tasks, chainOk: true, auditCount: null })
  check('… unread → 尚未讀取', /^尚未讀取/.test(p.head.find((h) => h.k === '主管已讀')?.text ?? ''), p.head.find((h) => h.k === '主管已讀')?.text)
  const none = buildReport({ ev: base, approvals: [], tasks: s.tasks, chainOk: true, auditCount: null })
  check('… an event without the fields keeps the old head', !none.head.some((h) => h.k === '主管已讀' || h.k === '維修完成' || h.k === '恢復正常'))
}

await vite.close()
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
