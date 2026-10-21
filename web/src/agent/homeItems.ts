// 處理清單 (UX brief, confirmed by CJ 2026-10-20: 「清單順序對，第一眼用「現在要你處理的事」」; names: veteran 10-22 for HY).
// One ranked list of what needs a human, built only from live data:
//   機台異常   — detector events (pending approval, safe stop, Agent Nemo still planning)
//   交期風險   — orders the active schedule finishes after their due date, and the solver's own late warnings
//   插單與人力 — supervisor tasks (下新任務) waiting for approval
//   處理中     — approved work being repaired, recovering or still done by people on the floor
//   已處理完   — closed events, Safety Core actions, rejected readings (collapsed)
import type { ApprovalRequest, DashboardSnapshot, Event, ScheduleVersion, WorldSnapshot } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { machineLabel } from '../factory'
import { REPAIR_KINDS } from '../aiStatus'
import { fmtHM, fmtMD, ms } from '../format'
import { plain } from '../plain'
import { deadline } from './planAnswer'

export type Rank = 1 | 2 | 3 | 4 | 5
/** No ①–⑤: a group shows only when it has items, and its name says what it is. */
export const RANK_LABEL: Record<Rank, string> = { 1: '機台異常', 2: '交期風險', 3: '插單與人力', 4: '處理中', 5: '已處理完' }

export interface HomeItem {
  key: string                // event id, or `order:<id>` / `fyi:<id>`
  rank: Rank
  title: string
  sub: string
  pill: string
  tone: 'wait' | 'bad' | 'run' | 'risk' | 'ok' | 'done'   // ok = back to normal (green)
  eventId: string | null
  orderId: string | null
  req: ApprovalRequest | null
  ts: number
  progress?: number         // 處理中: share of the event's field work done (0–1)
}

const OPEN = (e: Event) => e.state !== 'closed' && e.state !== 'safe_stop'
/** 「7 號硫化機・溫度下降 128→122.3°C」: the machine once (a card title often starts with 「7 號機」 or 「異常：B 線開煉機」);
 *  「主管指令：…」 once for a supervisor task, whose card title may already start with it. */
function named(head: string, title: string, machine?: string): string {
  let t = title.replace(/^異常[：:]\s*/, '')
  if (t.startsWith(`${head}：`)) return t
  const n = machine ? /^CURE-0?(\d+)$/.exec(machine) : null
  const own = [machine ? machineLabel(machine) : null, n ? `${n[1]} 號機` : null].find((x) => x && t.startsWith(x))
  if (own) t = t.slice(own.length).trim()
  return t ? `${head}${machine ? '・' : '：'}${t}` : head
}
/** How a closed event ended, in the supervisor's words: 已恢復正常 only when a reading confirmed it; a supervisor task or
 *  plant event has no reading to recover, so it is 已完成. */
export function ended(ev: Event): { pill: string; tone: HomeItem['tone'] } {
  if (ev.source === 'supervisor' || ev.source === 'plant') return { pill: '已完成', tone: 'ok' }
  const out = ev.outcome ?? ''
  if (/停用/.test(out)) return { pill: '已停用該機', tone: 'done' }
  if (/未驗證|未以讀值/.test(out)) return { pill: '已處置，未用讀值確認', tone: 'done' }
  return { pill: '已恢復正常', tone: 'ok' }
}
/** The outcome without the backend's 「已修復：」 head (the pill already says how it ended). */
export const outcomeText = (ev: Event) => plain(ev.outcome ?? '').replace(/^(已修復：)+/, '')

export function homeItems(snap: DashboardSnapshot | null, pending: ApprovalRequest[], schedule: ScheduleVersion | null, world: WorldSnapshot | null, now: number): HomeItem[] {
  const out: HomeItem[] = []
  const events = snap?.events ?? []
  const pendBy = new Map<string, ApprovalRequest>()
  for (const p of pending) { const o = pendBy.get(p.event_id); if (!o || (p.version ?? 1) > (o.version ?? 1)) pendBy.set(p.event_id, p) }
  for (const ev of events) {
    const req = pendBy.get(ev.id) ?? null
    const who = ev.source === 'plant' ? '工廠事件' : machineLabel(ev.machine_id)
    const task = ev.source === 'supervisor' || ev.source === 'plant'   // 下新任務 and plant events (開工、插單、缺人) are 插單與人力
    const head = task ? (ev.source === 'plant' ? who : '主管指令') : who
    const machine = task ? undefined : ev.machine_id
    const title = named(head, plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message), machine)
    const tasks = (world?.tasks?.items ?? []).filter((t) => t.event_id === ev.id)
    const tasksOpen = tasks.filter((t) => t.status !== '完成').length
    const base = { key: ev.id, eventId: ev.id, orderId: null, req, ts: ms(ev.opened_at) }
    const closed = !OPEN(ev)
    if (req) {
      // one mention of the approval per card: the pill (veteran 10-22: 「03:54 前核准」); no R code on the supervisor's screen
      const by = deadline(req.expires_at, now)
      out.push({ ...base, rank: task ? 3 : 1, tone: 'wait', pill: `${by ? `${by}核准` : '待核准'}${(req.version ?? 1) > 1 ? '・已重算' : ''}`,
        title: named(head, plain(req.title), machine), sub: '' })
    } else if (ev.state === 'safe_stop') {
      out.push({ ...base, rank: 1, tone: 'bad', pill: '安全停止', title, sub: plain(ev.outcome ?? '安全核心已停機，待現場處理') })
    } else if (ev.state === 'execute' || ev.state === 'verify' || (tasks.length > 0 && tasksOpen > 0)) {
      // approved work stays on the list until people finish it (field work, warroom/sim/work.py); once a reading
      // confirmed the machine is back, it turns green and only counts the field work left
      const total = ev.steps.length, ok = ev.steps.filter((s) => s.state === 'ok').length
      const done = tasks.length - tasksOpen
      const repair = tasks.filter((t) => REPAIR_KINDS.has(t.kind))
      const repairOpen = repair.filter((t) => t.status !== '完成').length
      const progress = tasks.length ? done / tasks.length : ok / Math.max(1, total)
      if (closed) {
        const e = ended(ev)
        out.push({ ...base, rank: 4, tone: e.tone, progress, pill: e.pill, title, sub: `還有 ${tasksOpen} 件現場工作` })
      } else {
        out.push({ ...base, rank: 4, tone: 'run', progress,
          pill: ev.recovered_at ? '已恢復正常' : ev.repaired_at ? '維修完成・回溫中' : repairOpen ? `維修中（${repair.length - repairOpen}/${repair.length}）`
            : tasks.length ? `現場工作 ${done}/${tasks.length}` : `${STATE_LABEL[ev.state]} ${ok}/${total}`,
          title,
          sub: tasks.length ? `現場工作 ${done}/${tasks.length} 件完成${ev.state === 'verify' ? '・讀值確認中' : ''}` : ev.state === 'verify' ? '系統比對讀值中，回到正常就自動確認' : `執行核准內容・${ok}/${total} 步完成` })
      }
    } else if (!closed) {
      out.push({ ...base, rank: task ? 3 : 1, tone: 'run', pill: 'Agent Nemo 規劃中', title, sub: 'Agent Nemo 處理中，有需要你決定的會變成待核准' })
    } else {
      const e = ended(ev)
      out.push({ ...base, rank: 5, tone: e.tone, pill: e.pill, title, sub: `${outcomeText(ev)}${ev.closed_at ? `・${fmtHM(ev.closed_at)}` : ''}` })
    }
  }
  // 交期風險: the active schedule's last de-mold for an order after its due date, plus the solver's warnings
  const seen = new Set<string>()
  const jobs = schedule?.schedule.jobs ?? []
  for (const o of world?.orders.items ?? []) {
    if (o.status === '已出貨' || o.status === '完成' || o.status === '待出貨') continue
    const mine = jobs.filter((j) => j.order_id === o.id)
    if (!mine.length) continue
    const eta = Math.max(...mine.map((j) => ms(j.demold)))
    const due = /^\d{4}-\d{2}-\d{2}$/.test(o.due) ? ms(`${o.due}T23:59:00+08:00`) : ms(o.due)
    if (!(eta > due)) continue
    seen.add(o.id)
    out.push({ key: `order:${o.id}`, rank: 2, tone: 'risk', pill: '交期預警', eventId: null, orderId: o.id, req: null, ts: eta,
      title: `${o.id} 可能晚 ${Math.ceil((eta - due) / 3_600_000)} 小時交貨`, sub: `${o.customer}・排程最後一條 ${fmtMD(eta)} ${fmtHM(eta)} 出模・交期 ${fmtMD(o.due)}` })
  }
  for (const w of schedule?.schedule.warnings ?? []) {
    const [head, list] = w.includes('：') ? [w.slice(0, w.indexOf('：')), w.slice(w.indexOf('：') + 1)] : ['', w]
    for (const seg of list.split('、')) {
      const id = /#\p{Lu}-\d+/u.exec(seg)?.[0]
      if (!id || seen.has(id)) continue
      seen.add(id)
      out.push({ key: `order:${id}`, rank: 2, tone: 'risk', pill: '交期預警', eventId: null, orderId: id, req: null, ts: now,
        title: `${id} 預估晚於交期`, sub: `${plain(seg.trim())}${head ? `・${plain(head)}` : ''}` })
    }
  }
  // 已處理完: the Safety Core and the validation layer acted without Agent Nemo
  for (const a of (snap?.safety_actions ?? []).slice(-5)) {
    out.push({ key: `fyi:${a.id}`, rank: 5, tone: 'done', pill: '安全核心', eventId: null, orderId: null, req: null, ts: ms(a.ts),
      title: `${machineLabel(a.machine_id)}：${plain(a.message)}`, sub: `不經 AI・${a.latency_ms.toFixed(0)} ms・${fmtHM(a.ts)}` })
  }
  for (const b of snap?.header.bad_data ?? []) {
    out.push({ key: `fyi:bad-${b.machine_id}`, rank: 5, tone: 'done', pill: '已擋下不可信的讀值', eventId: null, orderId: null, req: null, ts: ms(b.last_at),
      title: `${machineLabel(b.machine_id)} ${b.count} 筆讀值不可信，已自動擋下`, sub: `沒有交給 Agent Nemo・${fmtHM(b.last_at)}` })
  }
  return out.sort((a, b) => a.rank - b.rank || (a.req && b.req ? (a.req.risk > b.req.risk ? -1 : a.req.risk < b.req.risk ? 1 : 0) : 0) || b.ts - a.ts)
}
