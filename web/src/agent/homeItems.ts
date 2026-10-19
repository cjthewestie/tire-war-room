// 核准區 waiting list (UX brief, confirmed by CJ 2026-10-20: 「清單順序對，第一眼用「現在要你處理的事」」).
// One ranked list of what needs a human, built only from live data:
//   ① 安全／機台異常 — detector events (pending approval, safe stop, AI still planning)
//   ② 交期風險       — orders the active schedule finishes after their due date, and the solver's own late warnings
//   ③ 插單／人力決定 — supervisor tasks (下新任務) waiting for approval
//   ④ 執行中，等確認完成 — approved work being executed or verified
//   ⑤ AI 已自行處理 — closed events, Safety Core actions, rejected bad data (collapsed)
import type { ApprovalRequest, DashboardSnapshot, Event, ScheduleVersion, WorldSnapshot } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD, ms } from '../format'
import { RISK_PLAIN, plain } from '../plain'

export type Rank = 1 | 2 | 3 | 4 | 5
export const RANK_LABEL: Record<Rank, string> = { 1: '① 安全／機台異常', 2: '② 交期風險', 3: '③ 插單／人力決定', 4: '④ 執行中，等確認完成', 5: '⑤ AI 已自行處理' }

export interface HomeItem {
  key: string                // event id, or `order:<id>` / `fyi:<id>`
  rank: Rank
  title: string
  sub: string
  pill: string
  tone: 'wait' | 'bad' | 'run' | 'risk' | 'done'
  eventId: string | null
  orderId: string | null
  req: ApprovalRequest | null
  ts: number
}

const OPEN = (e: Event) => e.state !== 'closed' && e.state !== 'safe_stop'

export function homeItems(snap: DashboardSnapshot | null, pending: ApprovalRequest[], schedule: ScheduleVersion | null, world: WorldSnapshot | null, now: number): HomeItem[] {
  const out: HomeItem[] = []
  const events = snap?.events ?? []
  const pendBy = new Map<string, ApprovalRequest>()
  for (const p of pending) { const o = pendBy.get(p.event_id); if (!o || (p.version ?? 1) > (o.version ?? 1)) pendBy.set(p.event_id, p) }
  for (const ev of events) {
    const req = pendBy.get(ev.id) ?? null
    const who = machineLabel(ev.machine_id)
    const task = ev.source === 'supervisor'
    const base = { key: ev.id, eventId: ev.id, orderId: null, req, ts: ms(ev.opened_at) }
    if (req) {
      out.push({ ...base, rank: task ? 3 : 1, tone: 'wait', pill: `待核准・${req.risk}${(req.version ?? 1) > 1 ? `・v${req.version}` : ''}`,
        title: plain(req.title), sub: `${task ? '主管指令' : who}・${RISK_PLAIN[req.risk]}・${fmtHM(req.created_at)} 起${req.expires_at ? `・${fmtMD(req.expires_at)} ${fmtHM(req.expires_at)} 前決定` : ''}` })
    } else if (ev.state === 'safe_stop') {
      out.push({ ...base, rank: 1, tone: 'bad', pill: '安全停止', title: `${who}：${plain(ev.alert_card?.title ?? ev.anomaly.message)}`, sub: plain(ev.outcome ?? '安全核心已停機，待現場處理') })
    } else if (ev.state === 'execute' || ev.state === 'verify') {
      const total = ev.steps.length, ok = ev.steps.filter((s) => s.state === 'ok').length
      out.push({ ...base, rank: 4, tone: 'run', pill: `${STATE_LABEL[ev.state]} ${ok}/${total}`, title: `${task ? '主管指令' : who}：${plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message)}`,
        sub: ev.state === 'verify' ? '程式比對預期與讀值中，達標即自動結案' : `執行核准內容・${ok}/${total} 步完成` })
    } else if (OPEN(ev)) {
      out.push({ ...base, rank: task ? 3 : 1, tone: 'run', pill: `AI ${STATE_LABEL[ev.state]}`, title: `${task ? '主管指令' : who}：${plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message)}`,
        sub: 'AI 指揮官處理中，有需要你決定的會變成待核准' })
    } else {
      out.push({ ...base, rank: 5, tone: 'done', pill: '已結案', title: `${task ? '主管指令' : who}：${plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message)}`,
        sub: `${plain(ev.outcome ?? '已結案')}${ev.closed_at ? `・${fmtHM(ev.closed_at)}` : ''}` })
    }
  }
  // ② delivery risk: the active schedule's last de-mold for an order after its due date, plus the solver's warnings
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
      const id = /#[A-Z]-\d+/.exec(seg)?.[0]
      if (!id || seen.has(id)) continue
      seen.add(id)
      out.push({ key: `order:${id}`, rank: 2, tone: 'risk', pill: '交期預警', eventId: null, orderId: id, req: null, ts: now,
        title: `${id} 預估晚於交期`, sub: `${plain(seg.trim())}${head ? `・${plain(head)}` : ''}` })
    }
  }
  // ⑤ the Safety Core and the validation layer acted without the AI
  for (const a of (snap?.safety_actions ?? []).slice(-5)) {
    out.push({ key: `fyi:${a.id}`, rank: 5, tone: 'done', pill: '安全核心', eventId: null, orderId: null, req: null, ts: ms(a.ts),
      title: `${machineLabel(a.machine_id)}：${plain(a.message)}`, sub: `不經 AI・${a.latency_ms.toFixed(0)} ms・${fmtHM(a.ts)}` })
  }
  for (const b of snap?.header.bad_data ?? []) {
    out.push({ key: `fyi:bad-${b.machine_id}`, rank: 5, tone: 'done', pill: '壞資料', eventId: null, orderId: null, req: null, ts: ms(b.last_at),
      title: `${machineLabel(b.machine_id)}：驗證層攔下 ${b.count} 筆讀值`, sub: `未喚醒 AI・${fmtHM(b.last_at)}` })
  }
  return out.sort((a, b) => a.rank - b.rank || (a.req && b.req ? (a.req.risk > b.req.risk ? -1 : a.req.risk < b.req.risk ? 1 : 0) : 0) || b.ts - a.ts)
}

/** What comes next when nothing needs a human (empty state): the next de-mold, truck, delivery and maintenance. */
export function nextUp(schedule: ScheduleVersion | null, world: WorldSnapshot | null, now: number): string[] {
  const out: { t: number; s: string }[] = []
  const dm = (schedule?.schedule.jobs ?? []).map((j) => ({ t: ms(j.demold), j })).filter((x) => x.t > now).sort((a, b) => a.t - b.t)[0]
  if (dm) out.push({ t: dm.t, s: `${fmtHM(dm.t)} ${machineLabel(dm.j.press_id)}出模（${dm.j.order_id}）` })
  const dep = world?.dock.next_departure
  if (dep && ms(dep) > now) out.push({ t: ms(dep), s: `${fmtHM(dep)} 出貨車離廠・待出貨 ${world?.finished_goods.waiting_ship ?? 0} 條` })
  const po = (world?.purchase_orders ?? []).filter((p) => p.status === '在途' && ms(p.eta) > now).sort((a, b) => ms(a.eta) - ms(b.eta))[0]
  if (po) out.push({ t: ms(po.eta), s: `${fmtHM(po.eta)} ${po.name}到貨` })
  const mt = (world?.maintenance.upcoming ?? []).filter((m) => ms(m.start) > now).sort((a, b) => ms(a.start) - ms(b.start))[0]
  if (mt) out.push({ t: ms(mt.start), s: `${fmtMD(mt.start)} ${fmtHM(mt.start)} ${machineLabel(mt.machine_id)}${mt.title}` })
  return out.sort((a, b) => a.t - b.t).map((x) => x.s)
}
