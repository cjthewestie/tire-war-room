// Unit checks (no server, no browser) for the fast-repair front end (Kyle 10-21: 觸發異常 → 主管核准 → 專人維修 → 維修完成 →
// 請主管覆核 → 處理報告): the AI status line's 專人維修中 / 待主管覆核 / 主管已覆核, the 3D ring of a repair task, the phone's
// 覆核 alert, the Agent page's task and 覆核 rows, and the report's 主管覆核 line. Built on fixtures/status-cases.json
// (concurrent_3, B 線開煉機) with the Event.repaired_at / review fields of the shared contract patched in.
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
const { machineRing } = await load('/src/factory3d/machineRing.ts')
const { alertText, diffPending, reviewAlerts } = await load('/src/phone/alert.ts')
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
const line = (p) => [p.what, p.who, p.phase, p.eta].filter(Boolean).join('｜')
// a fast repair: 檢修 assigned at once, 30 factory seconds to go (3 wall s at 10×)
const now = T(s.now)
const repair = {
  ...s.tasks.find((t) => t.id === 'TK-0001'), status: '執行中', assigned_at: iso(now - 20_000), started_at: iso(now - 20_000),
  eta: iso(now + 30_000), progress: 0.2, done_at: null,
}
const slow = { ...s.tasks.find((t) => t.id === 'TK-0002'), progress: 1 }

// ---------------------------------------------------------------- AI status line
{
  const ev = { ...base, state: 'verify', closed_at: null, outcome: null }
  const p = aiStatusParts(ev, stateOf([ev], [repair, slow]), null)
  check('verify + 檢修 running → 專人維修中（1/2）', p.phase === '專人維修中（1/2）', line(p))
  check('… ETA from the repair task (30 s), not the 20-minute window', /約 30 秒/.test(p.eta), p.eta)
  const done = { ...repair, status: '完成', done_at: iso(now), progress: 1 }
  const closed = { ...base, repaired_at: iso(now), review: { status: 'pending', requested_at: iso(now), done_at: null, by: null } }
  const q = aiStatusParts(closed, stateOf([closed], [done, slow]), null)
  check('closed + review pending → 已修復，待主管覆核, no ETA', q.phase === '已修復，待主管覆核' && !q.eta, line(q))
  const r = aiStatusParts({ ...closed, review: { ...closed.review, status: 'done', done_at: iso(now + 5000), by: 'supervisor' } }, stateOf([closed], [done, slow]), null)
  check('review done → 已修復・主管已覆核', r.phase === '已修復・主管已覆核', line(r))
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
  const other = { ...repair, id: 'TK-9', kind: '補料', status: '執行中' }
  const pick = machineRing('B-OPEN', tile, world([other, repair]), now, 10)
  check('a repair task wins over another task on the same machine', pick.label.startsWith('檢修'), pick.label)
}

// ---------------------------------------------------------------- phone alert
{
  const evs = ['B-OPEN', 'A-BIGOPEN', 'CURE-03'].map((m, i) => ({ ...base, id: `EV-${m}`, machine_id: m, outcome: `已修復：${m}`,
    review: { status: 'pending', requested_at: iso(now + i * 1000), done_at: null, by: null } }))
  const items = reviewAlerts(evs)
  const st = { seen: new Set(), held: [] }
  const first = diffPending(st, items.slice(0, 1), true)
  check('one review → rings once: 請覆核：B 線開煉機維修完成', first.fire.length === 1 && alertText(first.fire, false).title === '請覆核：B 線開煉機維修完成', alertText(first.fire, false).title)
  const more = diffPending(st, items, true)
  check('two more reviews (3 concurrent) → only the new ones ring', more.fire.length === 2 && alertText(more.fire, false).title === '2 件待覆核', alertText(more.fire, false).title)
  const again = diffPending(st, items, true)
  check('… and not again', again.fire.length === 0)
  const doneOne = reviewAlerts(evs.map((e, i) => (i === 0 ? { ...e, review: { ...e.review, status: 'done' } } : e)))
  check('a reviewed event leaves the alert list', doneOne.length === 2)
  const mixed = alertText([{ id: 'AP-1', title: '7 號機溫降', reason: 'x', risk: 'R2' }, items[0]], false)
  check('approval + review together → 2 件待核准／覆核', mixed.title === '2 件待核准／覆核', mixed.title)
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
    a('system', 'verifier', { hypothesis: 'H1', verdict: 'confirmed', observed: 101.2 }, at(15)),
    a('state_transition', 'state_machine', { from: 'verify', to: 'closed', reason: '已修復：B 線開煉機 膠溫回到 101°C' }, at(15)),
    a('system', 'state_machine', { msg: '請主管覆核' }, at(15)),
    a('system', 'supervisor', { msg: '主管覆核', note: '現場確認正常' }, at(30)),
  ]
  const items = fromAudit([...rows].reverse(), base)
  const seqd = items.map((i) => (i.k === 'task' ? `task:${i.phase}${i.who ? `@${i.who}` : ''}` : i.k === 'review' ? `review:${i.phase}` : i.k === 'state' ? `state:${i.to}` : i.k === 'note' ? 'note' : i.k))
  const want = ['state:detected', 'approval', 'task:created', 'task:assigned@陳志明', 'task:started@陳志明', 'task:done@陳志明', 'review:repaired',
    'state:verify', 'note', 'state:closed', 'review:requested', 'review:done']
  check('處理歷程 rows in time order: 派工 → 前往中 → 執行中 → 完成 → 維修完成 → 驗證 → 結案 → 請主管覆核 → 主管覆核', JSON.stringify(seqd) === JSON.stringify(want), seqd.join(' '))
  const rep = items.find((i) => i.k === 'review' && i.phase === 'repaired')
  check('… 維修完成 names the machine', rep?.text === '維修完成，故障排除・B 線開煉機', rep?.text)
  check('… 主管覆核 keeps the note', items.at(-1)?.note === '現場確認正常')
  // the world assigns at once, so its 前往中 row is written before the state machine's 派工 row
  const swapped = fromAudit([rows[3], rows[2], rows[4]].map((r, i) => ({ ...r, seq: 500 + i })), base)
  check('派工 shown before 前往中 even when the audit wrote 前往中 first', swapped.map((i) => i.phase).join(',') === 'created,assigned,started', swapped.map((i) => i.phase).join(','))
  check('… 派工 text does not repeat the machine already in the title', swapped[0].text === '檢修 B 線開煉機', swapped[0].text)
  const old = fromAudit([a('system', 'world', { msg: '現場處置完成，故障排除', machine_id: 'B-OPEN' }, at(14))], base)
  check('… the runtime\'s older wording counts as 維修完成 too', old[0]?.k === 'review' && old[0].phase === 'repaired')
}

// ---------------------------------------------------------------- report
{
  const ev = { ...base, repaired_at: iso(now), review: { status: 'done', requested_at: iso(now), done_at: iso(now + 60_000), by: 'supervisor' } }
  const r = buildReport({ ev, approvals: [], tasks: s.tasks, chainOk: true, auditCount: null })
  const rv = r.head.find((h) => h.k === '主管覆核')
  check('report head: 維修完成 and 主管覆核 已覆核', !!r.head.find((h) => h.k === '維修完成') && /^已覆核/.test(rv?.text ?? ''), rv?.text)
  const p = buildReport({ ev: { ...ev, review: { ...ev.review, status: 'pending', done_at: null, by: null } }, approvals: [], tasks: s.tasks, chainOk: true, auditCount: null })
  check('… pending → 待主管覆核', /^待主管覆核/.test(p.head.find((h) => h.k === '主管覆核')?.text ?? ''))
  const none = buildReport({ ev: base, approvals: [], tasks: s.tasks, chainOk: true, auditCount: null })
  check('… an event without the fields keeps the old head', !none.head.some((h) => h.k === '主管覆核' || h.k === '維修完成'))
}

await vite.close()
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
