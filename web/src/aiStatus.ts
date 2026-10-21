// 公版 AI 處理狀態 (Kyle 10-21: 「這得要是一個公版」): one sentence for every anomaly, the same on the phone, the 3D
// factory and the field SOP drawer —
//   偵測到：<異常>｜Agent Nemo 建議派人維修（<NemoClaw 沙箱>）｜<階段>｜核准後約 N 秒維修完成
// Read-only: the runtime label comes from /api/health agent_runtime (WARROOM_AGENT_RUNTIME, only set when the app really
// runs inside OpenShell), the time is the repair estimate of #73 (verification window from approval, or the last field
// task's ETA if later), or the wall seconds of a fast repair (/api/health repair_fast_s).
// Detection is a Python rule (warroom/detect), not AI: 「偵測到」, never 「AI 偵測到」. The repair task is made before the
// approval but held 待派 (warroom/sim/work.py): 「建議派人維修」 before, 「已派人維修」 after — read from the task itself
// (released), not the event's state: the R1 檢修 is made in EXECUTE before the card, and code re-solves an approved
// round's stale schedule (REPLAN … GATE, concurrent_3) while the approved repair goes on.
// Fast repair (Kyle 10-21 flow): 維修中（n/m）→ 維修完成・溫度回升中 (Event.repaired_at, the readings ramp back) →
// 7 號硫化機已恢復正常（128°C）｜Agent Nemo 異常報告已產生 (closed, Event.review pending = the incident report unread, and
// Event.ai_report written: until then 「Agent Nemo 異常報告整理中」) → …｜主管已讀報告.
import type { Channel, Event, Expectation, WorkTask } from './contracts'
import { machineLabel } from './factory'
import { fmtHM, fmtMD, ms } from './format'
import { plain } from './plain'
import type { useWarRoom } from './useWarRoom'

type WarRoom = ReturnType<typeof useWarRoom>['state']

/** Field tasks that repair the machine itself (warroom/sim/work.py plan_tasks, runtime REPAIR_KINDS); staffing and 宣達
 *  are not repairs. */
export const REPAIR_KINDS = new Set(['檢修', '停用檢修', '放慢投料', '暫停投料'])
/** A task still 待派 this long after it was made is left out of the ETA: nobody took it, so its ETA only follows the
 *  clock (p.11 round 1's 調派支援 kept 「預計完成 02:46」 moving). */
const STALE_UNASSIGNED_MS = 10 * 60_000

export const CH_LABEL: Record<Channel, string> = { temp_c: '溫度', pressure_kgf_cm2: '壓力', water_flow_lpm: '水流', steam_kgf_cm2: '蒸汽壓' }
const UNIT: Record<Channel, string> = { temp_c: '°C', pressure_kgf_cm2: ' kgf/cm²', water_flow_lpm: ' L/min', steam_kgf_cm2: ' kgf/cm²' }

/** The expectation's target, e.g. 「≥121°C」. */
export function targetText(x: Expectation): string {
  const u = UNIT[x.channel]
  return x.op === 'between' ? `${x.value}–${x.value_hi}${u}` : `${x.op === '>=' ? '≥' : '≤'}${x.value}${u}`
}

/** A reading with its unit, e.g. 「100.2°C」. */
export function readingText(ch: Channel, v: number): string {
  return `${Math.round(v * 10) / 10}${UNIT[ch]}`
}

/** Does this reading meet the expectation (the same test VERIFY makes)? */
export function meetsExpectation(x: Expectation, v: number): boolean {
  return x.op === '>=' ? v >= x.value : x.op === '<=' ? v <= x.value : v >= x.value && v <= (x.value_hi ?? Infinity)
}

/** The expectation the event will be verified against: the plan's hypothesis first, else any open one. */
export function expectationOf(ev: Event | null): Expectation | null {
  if (!ev) return null
  return ev.plan?.hypothesis.expectation
    ?? ev.hypotheses.find((h) => h.status !== 'rejected' && h.expectation)?.expectation
    ?? null
}

/** The event's tasks that belong to its newest card: once a newer card exists (round 2 「7 號機停用」, a discussed v2),
 *  the older card's tasks are history (round 1's 宣達 28.5 h and 調派支援). Tasks with no card (R1 steps such as the
 *  檢修 notice, 撤回 notices) stay. */
function currentTasks(ev: Event, items: WorkTask[]): WorkTask[] {
  const card = ev.approvals.at(-1) ?? null
  return items.filter((t) => t.event_id === ev.id && (!t.approval_id || t.approval_id === card))
}

/** The latest ETA of these open tasks, leaving out the ones nobody has taken for STALE_UNASSIGNED_MS. */
function latestEta(open: WorkTask[], now: string | null): string | null {
  const live = open.filter((t) => !(t.status === '待派' && now && ms(now) - ms(t.created_at) > STALE_UNASSIGNED_MS))
  return live.map((t) => t.eta).filter((e): e is string => !!e).sort().at(-1) ?? null
}

/** When the anomaly should be fixed: from the approval (or now) + the verification window, or the last task ETA. */
export function repairEstimate(ev: Event, state: WarRoom) {
  const now = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? null
  const x = expectationOf(ev)
  const tasks = currentTasks(ev, state.world?.tasks.items ?? [])
  const open = tasks.filter((t) => t.status !== '完成')
  const lastEta = latestEta(open, now)
  // the newest card's decision: while it waits (round 2 pending) the window runs from now (「現在核准的話」); a screen
  // opened after the decision has no resolved card in its store, so the card's first task (made on approval) stands in
  const card = ev.approvals.at(-1)
  const waiting = !card || state.pending.some((p) => p.id === card)
  const decided = waiting ? null
    : state.resolved.find((r) => r.id === card && r.status === 'approved')?.decision?.decided_at
      ?? tasks.filter((t) => t.approval_id === card).map((t) => t.created_at).sort()[0] ?? null
  const from = decided ?? now
  const byWindow = x && from ? ms(from) + x.within_min * 60_000 : null
  const doneBy = lastEta ? Math.max(ms(lastEta), byWindow ?? 0) : byWindow
  return { now, x, tasks, open, lastEta, decided, doneBy }
}

/** The machine's normal value the readings ramp back to after a fast repair: the detector's baseline of the anomaly
 *  (≈ the simulator's nominal: 7 號 128°C, B 線開煉機 94°C) when it is the expectation's channel; null otherwise (a
 *  supervisor or plant event has no baseline). */
export function normalOf(ev: Event, x: Expectation | null = expectationOf(ev)): number | null {
  const a = ev.anomaly
  return x && a && a.channel === x.channel && a.baseline > 0 ? a.baseline : null
}

/** 「溫度回升中」 / 「溫度回降中」: which way the reading goes, toward the normal value when known, else to meet the
 *  expectation. */
export function recoverWord(x: Expectation | null, now: number | null = null, normal: number | null = null): string {
  if (!x) return '讀值恢復中'
  const down = normal != null && now != null ? now > normal
    : x.op === '<=' || (x.op === 'between' && now != null && now > (x.value_hi ?? x.value))
  return `${CH_LABEL[x.channel]}${down ? '回降中' : '回升中'}`
}

/** 「溫度回升中 122.1°C → 128°C」: the reading toward the machine's normal value (the ramp ends there; Kyle: 恢復正常溫度
 *  後轉綠燈), or toward the expectation's target (「→ ≥121°C」) when that is unknown; the reading left out while unknown.
 *  Not the expectation when the normal value is known: a temperature drop 128 → 122°C already meets 「≥121°C」. */
export function recoverText(x: Expectation | null, now: number | null, normal: number | null = null): string {
  const to = x ? (normal != null ? readingText(x.channel, normal) : targetText(x)) : ''
  return `${recoverWord(x, now, normal)}${x && now != null ? ` ${readingText(x.channel, now)} → ${to}` : ''}`
}

/** The field already works on this task: someone took it, or (fast repair) its wall seconds run. Until the supervisor's
 *  decision a fast repair task stays 待派 with no progress (warroom/sim/work.py TaskBoard.hold, runtime
 *  _awaits_decision); after it the task runs on, even while code re-solves the approved round's schedule. */
function released(t: WorkTask): boolean {
  return t.status !== '待派' || !!t.assigned_at || (t.progress ?? 0) > 0
}

/** The incident report is unread and written: Event.review pending is set at close, Event.ai_report once Agent Nemo's
 *  draft is in (warroom/agent/reporter.py, up to its close wait later). */
export function reportReady(ev: Event): ev is Event & { review: NonNullable<Event['review']> } {
  return ev.review?.status === 'pending' && !!ev.ai_report
}

/** Where a fast repair stands: the crew working (after the supervisor's decision), the readings coming back after the
 *  repair (repaired_at), back to normal (recovered_at), the report unread / read (closed, Event.review and ai_report). */
export type RepairStage = 'repairing' | 'recovering' | 'recovered' | 'report' | 'read'
export function repairStage(ev: Event, items: WorkTask[]): RepairStage | null {
  if (ev.state === 'closed') return ev.review?.status === 'done' ? 'read' : reportReady(ev) ? 'report' : ev.repaired_at ? 'recovered' : null
  if (ev.state === 'safe_stop') return null
  if (ev.recovered_at) return 'recovered'
  if (ev.repaired_at) return 'recovering'
  return items.some((t) => t.event_id === ev.id && REPAIR_KINDS.has(t.kind) && t.status !== '完成' && released(t)) ? 'repairing' : null
}

/** What was detected: the same text on every screen for the whole event (not the latest alert title, which a later round
 *  rewrites into its action, p.11 round 2: 「7 號機停用，重排中」). A press's temperature is put together from the
 *  anomaly's own fields, 「7 號硫化機溫度 128 → 122°C」 (no forecast); other machines keep the detector's sentence up to its
 *  first clause (「B 線開煉機 膠溫 96°C 持續上升（+4.5°C/分）」). A supervisor command or plant event has no detection: its
 *  alert title as before. */
function detection(ev: Event): string {
  if (ev.source === 'supervisor' || ev.source === 'plant') return plain(ev.alert_card?.title ?? ev.anomaly.message)
  const a = ev.anomaly
  if (a.machine_id.startsWith('CURE-') && a.channel === 'temp_c' && (a.kind === 'temp_drop' || a.kind === 'overheat') && a.baseline > 0) {
    return `${machineLabel(a.machine_id)}${CH_LABEL.temp_c} ${Math.round(a.baseline)} → ${Math.round(a.value)}°C`
  }
  return plain(a.message || ev.alert_card?.title || machineLabel(ev.machine_id)).split(/[，；]/)[0].replace(/^異常[：:]\s*/, '')
}

/** The field work of these repair tasks in the supervisor's words: 檢修／停用檢修 = 維修, 放慢投料, 暫停投料
 *  (B 線開煉機: 維修、放慢投料). */
function repairWork(tasks: WorkTask[]): string[] {
  const work = new Set(tasks.map((t) => (t.kind === '放慢投料' || t.kind === '暫停投料' ? t.kind : '維修')))
  return [...work].sort((a, b) => Number(b === '維修') - Number(a === '維修'))   // 維修 first, whatever the task order
}

/** 「維修中（1/2）」: the repair tasks done out of all of them, named by the work still open (維修 first). */
function repairPhase(repair: WorkTask[], open: WorkTask[]): string {
  const work = repairWork(open)
  return `${work.includes('維修') ? '維修' : work[0]}中（${repair.length - open.length}/${repair.length}）`
}

/** A closed event is 已恢復正常 only when a reading verified it: a repair task still running, a machine taken out
 *  (p.11 round 2: 「未驗證：已停用該機…」) or another unverified close says what really happened; null = verified. */
function unverifiedClose(ev: Event, repair: WorkTask[], repairOpen: WorkTask[]): string | null {
  if (repairOpen.length) return repairPhase(repair, repairOpen)
  if (/停用/.test(ev.outcome ?? '')) return '已停用該機，排程已調整'
  if (/未驗證/.test(ev.outcome ?? '')) return '已處置，未用讀值確認'
  return null
}

/** 「7 號硫化機已恢復正常（128°C）」: after a fast repair the readings ramped back to the machine's normal value (the
 *  anomaly's baseline); a close verified some other way leaves the value out (H1 「溫度會穩定」 holds at 123°C, not 128). */
function recoveredText(ev: Event): string {
  const x = expectationOf(ev)
  const normal = ev.recovered_at ? normalOf(ev, x) : null
  return `${machineLabel(ev.machine_id)}已恢復正常${x && normal != null ? `（${readingText(x.channel, normal)}）` : ''}`
}

const BEFORE_APPROVAL = new Set(['detected', 'classify', 'plan', 'simulate', 'gate', 'replan', 'await_approval'])

/** `fastS`: REPAIR_FAST_S from /api/health. A fast repair runs on the wall clock, so its ETA is wall seconds
 *  (「約 8 秒後維修完成」), not factory time (at 10× a 12-second repair read 「約 2 分鐘」); before the approval it is the
 *  whole repair (「核准後約 12 秒維修完成」), not the verification window (「約 30 分鐘」, then 「約 2 秒」 once approved).
 *  `lead` is the line's first, bold part: 「偵測到：<what>」, or 「<機台>已恢復正常（128°C）」 once a reading verified the
 *  close; `who` is empty while no repair is planned yet. */
export function aiStatusParts(ev: Event, state: WarRoom, runtime: string | null, fastS: number | null = null) {
  const what = detection(ev)
  const est = repairEstimate(ev, state)
  const done = est.tasks.length - est.open.length
  // any round's repair task counts: the machine is not fixed while maintenance is still on it
  const repair = (state.world?.tasks.items ?? []).filter((t) => t.event_id === ev.id && REPAIR_KINDS.has(t.kind))
  const repairOpen = repair.filter((t) => t.status !== '完成')
  // the crew is on the open repair (the supervisor decided, or no card was needed)
  const running = repairOpen.some(released)
  // before the supervisor's decision: the open repair is still held (and the newest card not decided), or, with none
  // open, nothing was ever sent out while the event plans or waits
  const ended = ev.state === 'closed' || ev.state === 'safe_stop' || !!ev.repaired_at
  const before = !ended && (repairOpen.length ? !running && est.decided == null
    : !repair.some(released) && BEFORE_APPROVAL.has(ev.state))
  const plantEvent = ev.source === 'supervisor' || ev.source === 'plant'
  // closed and verified by a reading (a supervisor or plant event has nothing to verify: 已處理完)
  const unverified = ev.state === 'closed' && !plantEvent ? unverifiedClose(ev, repair, repairOpen) : null
  const verified = ev.state === 'closed' && !plantEvent && unverified == null
  // after the repair, until the event closes: the readings ramp back to normal (a few wall seconds)
  const recovering = ev.state !== 'closed' && ev.state !== 'safe_stop' && !!ev.repaired_at
  const lead = verified ? recoveredText(ev) : plantEvent ? what : `偵測到：${what}`
  // Kyle 10-21: the single agent is called Agent Nemo on screen; no typed sub-agent in brackets (CJ 2026-10-21: 「現在先不要派發」)
  const work = repairWork(repair)
  const who = !verified && work.length ? `Agent Nemo ${before ? '建議' : '已'}派人${work.join('、')}` : ''
  // once closed, and while the crew repairs the machine, the ETA is the repair tasks' own (a fast repair takes seconds,
  // not the verification window)
  const repairing = ev.state === 'closed' || (!before && repairOpen.length > 0)
  const phase =
    ev.state === 'closed' ? (plantEvent ? '已處理完' : unverified
      ?? (ev.review?.status === 'done' ? '主管已讀報告' : ev.review?.status === 'pending'
        ? (ev.ai_report ? 'Agent Nemo 異常報告已產生' : 'Agent Nemo 異常報告整理中') : ''))
      : ev.state === 'safe_stop' ? '已安全停止'
        : recovering ? (ev.recovered_at ? '已恢復正常' : `維修完成・${recoverWord(est.x)}`)
        : ev.state === 'await_approval' ? '待核准'
          : repairing ? repairPhase(repair, repairOpen)
            : ev.state === 'execute' || ev.state === 'verify'
              ? (before ? 'Agent Nemo 擬方案中' : est.open.length ? `現場工作中（${done}/${est.tasks.length}）` : '確認讀值中')
              // the newest card approved, its schedule gone stale: code re-solves it, the repair already done
              : !before && est.decided != null && repair.length ? '重新排程中'
                : ev.state === 'detected' || ev.state === 'classify' ? 'Agent Nemo 分析中' : 'Agent Nemo 擬方案中'
  const repairEta = repairing ? latestEta(repairOpen, est.now) : null
  const doneBy = ev.state === 'safe_stop' || recovering ? null : repairEta ? ms(repairEta) : ev.state === 'closed' ? null : est.doneBy
  let eta = ''
  if (fastS && fastS > 0 && ev.state !== 'closed' && !recovering && repairing && repairOpen.length) {
    const left = Math.max(...repairOpen.map((t) => (1 - Math.min(1, Math.max(0, t.progress ?? 0))) * fastS))
    eta = `約 ${Math.max(1, Math.round(left))} 秒後維修完成`
  } else if (fastS && fastS > 0 && before) {
    // while Agent Nemo still plans there is no repair yet: no 「約 30 分鐘」 (the verification window) before 「約 12 秒」
    eta = repairOpen.length ? `核准後約 ${Math.max(1, Math.round(fastS))} 秒維修完成` : ''
  } else if (doneBy) {
    const s = est.now ? (doneBy - ms(est.now)) / 1000 : null
    const left = s == null ? '' : s <= 0 ? '即將完成' : s < 60 ? `約 ${Math.max(1, Math.round(s))} 秒` : `約 ${Math.round(s / 60)} 分鐘`
    // 時:分 in factory time; the date only when it is another day
    const day = est.now && fmtMD(doneBy) === fmtMD(est.now) ? '' : `${fmtMD(doneBy)} `
    eta = `預計 ${day}${fmtHM(doneBy)} 完成${left ? `（${left}）` : ''}`
  }
  // the runtime label (NemoClaw 沙箱) ends the part that names Agent Nemo
  const tag = runtime ? `（${runtime}）` : ''
  return {
    what,
    who: who && `${who}${tag}`,
    phase: tag && !who && phase.startsWith('Agent Nemo') ? `${phase}${tag}` : phase,
    eta,
    lead,
  }
}
