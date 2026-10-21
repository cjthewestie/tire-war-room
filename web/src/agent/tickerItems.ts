// 主控台 ticker (CJ, 2026-10-20: 「幫我設計成產線監控的跑馬燈好了」): short live lines about the plant, built only from the
// snapshot, the active schedule and GET /api/world — per-line status, each curing press, output vs plan, the next
// truck, materials, the dock, staffing, the latest closed event and order success moments. Alarm / warning items
// carry their tone so the ticker can pin them first.
import type { DashboardSnapshot, Line, ScheduleVersion, Shift, WorldEvent, WorldSnapshot } from '../contracts'
import { LINE_SHORT, MACHINE_META, machineLabel } from '../factory'
import { factoryMidnight, fmtHM, ms } from '../format'
import { plain } from '../plain'
import { ended } from './homeItems'

export type Tone = 'bad' | 'warn' | 'ok' | 'info' | 'good'
export interface TickItem { key: string; text: string; tone: Tone; machine?: string; line?: Line }

const H = 3_600_000
const STATUS: Record<string, string> = { warning: '注意', alarm: '異常', stopped: '停機', out_of_service: '停用', offline: '離線', idle: '待機' }
const toneOf = (s: string): Tone => (s === 'alarm' || s === 'stopped' || s === 'out_of_service' ? 'bad' : s === 'warning' || s === 'offline' ? 'warn' : 'ok')

/** issue #36 success moments (the other branch emits order_completed / shipment_departed with order_ids, qty, on_time);
 *  main's world feed has order_done / shipment, read the same way. Unknown kinds are ignored. */
export interface Success { key: string; kind: 'done' | 'shipped'; orderIds: string[]; qty: number | null; onTime: boolean | null; ts: string; text: string }
export function successOf(e: WorldEvent): Success | null {
  const kind = e.kind === 'order_completed' || e.kind === 'order_done' ? 'done' : e.kind === 'shipment_departed' ? 'shipped' : null
  if (!kind) return null
  const ids = e.order_ids?.length ? e.order_ids : (/#\p{Lu}-\d+/u.exec(e.text) ?? []).slice(0, 1)
  return { key: `${e.ts}|${e.kind}|${ids.join(',')}`, kind, orderIds: ids, qty: e.qty ?? null, onTime: e.on_time ?? null, ts: e.ts, text: e.text }
}
export function successText(s: Success): string {
  const id = s.orderIds.join('、') || ''
  const late = s.onTime === false ? '・逾期' : s.onTime === true ? '・準時' : ''
  return s.kind === 'done' ? `✓ 訂單 ${id} 完成${s.qty != null ? ` ${s.qty} 條` : ''}${late}` : `🚚 ${id} 已出貨${s.qty != null ? ` ${s.qty} 條` : ''}${late}`
}

export function tickerItems(snap: DashboardSnapshot | null, schedule: ScheduleVersion | null, world: WorldSnapshot | null, shifts: Shift[], now: number): TickItem[] {
  const out: TickItem[] = []
  const machines = snap?.machines ?? []
  // per line: the machine that needs attention, else the line's lead machine with its reading
  for (const ln of ['A', 'B', 'C', 'D', 'BOND'] as Line[]) {
    const ms_ = machines.filter((m) => m.line === ln)
    if (!ms_.length) continue
    const name = `${LINE_SHORT[ln]}${ln.length === 1 ? ' 線' : ''}`
    const bad = ms_.filter((m) => toneOf(m.status) !== 'ok')
    for (const m of bad) {
      out.push({ key: `m-${m.machine_id}`, tone: toneOf(m.status), machine: m.machine_id, line: ln,
        text: `${name} ${MACHINE_META[m.machine_id]?.name ?? m.short_name} ${STATUS[m.status] ?? m.status}${m.temp_c != null ? ` ${Math.round(m.temp_c)}°C` : ''}` })
    }
    if (bad.length) continue
    const lead = ms_.find((m) => m.machine_id === 'A-BIGOPEN') ?? ms_.find((m) => m.temp_c != null)
    const run = ms_.filter((m) => m.status === 'running').length
    out.push({ key: `l-${ln}`, tone: 'ok', line: ln, machine: lead?.machine_id,
      text: lead?.temp_c != null ? `${name} ${MACHINE_META[lead.machine_id]?.name ?? ''} ${lead.machine_id === 'A-BIGOPEN' ? '膠溫 ' : ''}${Math.round(lead.temp_c)}°C ✓` : `${name} 運轉 ${run}/${ms_.length} 台 ✓` })
  }
  // curing presses: how far the tyre in the mold is
  const jobs = schedule?.schedule.jobs ?? []
  for (const m of machines.filter((x) => x.line === 'CURE')) {
    const t = toneOf(m.status)
    const j = jobs.find((x) => x.press_id === m.machine_id && ms(x.mold_in) <= now && ms(x.demold) > now)
    const pct = j ? Math.round((100 * (now - ms(j.mold_in))) / Math.max(1, ms(j.demold) - ms(j.mold_in))) : null
    out.push({ key: `m-${m.machine_id}`, tone: t, machine: m.machine_id, line: 'CURE',
      text: t !== 'ok' ? `${machineLabel(m.machine_id)} ${STATUS[m.status] ?? m.status}${m.temp_c != null ? ` ${m.temp_c.toFixed(1)}°C` : ''}`
        : j ? `${machineLabel(m.machine_id)} 硫化中 ${pct}%（${j.order_id}）` : `${machineLabel(m.machine_id)} 待入模` })
  }
  // output vs plan today
  const d0 = factoryMidnight(now)
  const today = jobs.filter((j) => ms(j.demold) >= d0 && ms(j.demold) < d0 + 24 * H)
  if (today.length) out.push({ key: 'out', tone: 'info', text: `今日出模 ${today.filter((j) => ms(j.demold) <= now).length}/${today.length} 條` })
  if (world) {
    const dep = world.dock.next_departure
    if (dep) out.push({ key: 'ship', tone: 'info', text: `🚚 下一車出貨 ${fmtHM(dep)}・待出貨 ${world.finished_goods.waiting_ship} 條` })
    const wait = world.goods?.items.filter((g) => g.status === '待補料') ?? []
    if (wait.length) {
      const g = wait[0]
      const ln = g.machine_id ? MACHINE_META[g.machine_id]?.line : undefined
      out.push({ key: 'refill', tone: 'warn', line: ln, machine: g.machine_id ?? undefined, text: `${ln ? `${LINE_SHORT[ln]}${ln.length === 1 ? ' 線' : ''} ` : ''}待補料 ${wait.length} 條${g.note ? `：${plain(g.note)}` : ''}` })
    }
    for (const mt of world.materials.filter((x) => x.status !== 'ok')) {
      out.push({ key: `mat-${mt.key}`, tone: mt.status === 'reorder' ? 'warn' : 'bad', text: `原料 ${mt.name} 剩 ${mt.days_left ?? '—'} 天${mt.on_order ? '・已下單' : ''}` })
    }
    for (const tr of world.dock.trucks) out.push({ key: `dock-${tr.id}`, tone: 'info', text: `碼頭 ${tr.kind === 'inbound' ? '進貨卸貨中' : '出貨裝車中'}：${tr.cargo}` })
    const leave = world.people.workers.filter((p) => p.state === '請假').length
    if (leave) out.push({ key: 'leave', tone: 'info', text: `在廠 ${world.people.on_duty}/${world.people.total} 人・請假 ${leave}` })
    for (const s of world.events.map(successOf).filter((x): x is Success => !!x).slice(0, 4)) out.push({ key: `ok-${s.key}`, tone: 'good', text: successText(s) })
  }
  const onNow = shifts.filter((s) => ms(s.start) <= now && ms(s.end) > now)
  const short = onNow.filter((s) => (s.required ?? s.headcount) > s.headcount)
  if (onNow.length) {
    out.push(short.length ? { key: 'staff', tone: 'warn', text: `本班缺 ${short.reduce((n, s) => n + (s.required! - s.headcount), 0)} 人（${short.map((s) => s.name).join('、')}）` }
      : { key: 'staff', tone: 'ok', text: '本班人力足 ✓' })
  }
  const closed = [...(snap?.events ?? [])].filter((e) => e.state === 'closed').sort((a, b) => ((a.closed_at ?? '') < (b.closed_at ?? '') ? 1 : -1))[0]
  // the latest finished event in the supervisor's words (結案 is a state-machine word): 「✓ 7 號硫化機已恢復正常」
  if (closed) {
    const own = MACHINE_META[closed.machine_id] ? closed.machine_id : undefined
    const how = ended(closed, world?.tasks?.items ?? []).pill
    const what = plain(closed.alert_card?.title ?? closed.command ?? closed.anomaly.message)
    out.push({ key: `ev-${closed.id}`, tone: (own ? how === '已恢復正常' : how === '已完成') ? 'good' : 'info', machine: own,
      text: own ? (how === '已恢復正常' ? `✓ ${machineLabel(own)}已恢復正常` : `${machineLabel(own)}：${how}`) : how === '已完成' ? `✓ 已完成：${what}` : `${how}：${what}` })
  }
  return out
}
