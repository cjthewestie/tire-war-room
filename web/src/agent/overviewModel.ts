// 今日工廠概況 (HomeOverview) data, built by code from what the state already carries — no new API:
// the machine one-liner (detail stays on 產線監控) and 接下來 (prototype v12: 「告訴你下一件會發生的事」).
import type { MachineTile, ScheduleVersion, Shift, WorldSnapshot } from '../contracts'
import { STATUS_LABEL } from '../components/common'
import { TEAM_LABELS, machineLabel } from '../factory'
import { factoryMidnight, fmtHM, fmtMD, ms } from '../format'
import { isAbnormal } from '../store'

const ORDER: MachineTile['status'][] = ['running', 'idle', 'warning', 'alarm', 'stopped', 'out_of_service', 'offline']

/** 「硫化機 11 台：運轉 10・停用 1」 and 「其他機台 17 台：正常 16・異常 1」. */
export function machineSummary(tiles: MachineTile[]): { press: string | null; other: string | null; bad: number } {
  const press = tiles.filter((m) => m.line === 'CURE')
  const other = tiles.filter((m) => m.line !== 'CURE')
  const counts = ORDER.map((s) => [s, press.filter((m) => m.status === s).length] as const).filter(([, n]) => n > 0)
  const otherBad = other.filter((m) => isAbnormal(m.status)).length
  return {
    press: press.length ? `硫化機 ${press.length} 台：${counts.map(([s, n]) => `${STATUS_LABEL[s]} ${n}`).join('・')}` : null,
    other: other.length ? `其他機台 ${other.length} 台：正常 ${other.length - otherBad}${otherBad ? `・異常 ${otherBad}` : ''}` : null,
    bad: tiles.filter((m) => isAbnormal(m.status)).length,
  }
}

export interface Upcoming { key: string; t: number; when: string; text: string }

/** 「02:40」 today, 「10/04 06:00」 another day. */
export function when(t: number, now: number): string {
  const d0 = factoryMidnight(now)
  return t >= d0 && t < d0 + 86_400_000 ? fmtHM(t) : `${fmtMD(t)} ${fmtHM(t)}`
}

/** The next things that will happen: each press's next de-mold or mold-in (the earliest few), the next shift start,
 *  and in live the next truck, delivery and maintenance. Up to `n`, soonest first. */
export function upcoming(schedule: ScheduleVersion | null, world: WorldSnapshot | null, shifts: Shift[], now: number, n = 5): Upcoming[] {
  if (isNaN(now)) return []
  const press: Upcoming[] = []
  const jobs = schedule?.schedule.jobs ?? []
  for (const id of [...new Set(jobs.map((j) => j.press_id))]) {
    const mine = jobs.filter((j) => j.press_id === id)
    const dm = mine.filter((j) => ms(j.demold) > now).sort((a, b) => ms(a.demold) - ms(b.demold))[0]
    const mi = mine.filter((j) => ms(j.mold_in) > now).sort((a, b) => ms(a.mold_in) - ms(b.mold_in))[0]
    const next = dm && (!mi || ms(dm.demold) <= ms(mi.mold_in)) ? { t: ms(dm.demold), j: dm, k: '出模' } : mi ? { t: ms(mi.mold_in), j: mi, k: '入模' } : null
    if (next) press.push({ key: `p-${id}`, t: next.t, when: when(next.t, now), text: `${machineLabel(id)}${next.k}（${next.j.order_id}）` })
  }
  const other: Upcoming[] = []
  const starts = shifts.filter((s) => ms(s.start) > now).sort((a, b) => ms(a.start) - ms(b.start))
  if (starts.length) {
    const t = ms(starts[0].start)
    const at = starts.filter((s) => ms(s.start) === t)
    const names = [...new Set(at.map((s) => s.name))].join('、')
    other.push({ key: 'shift', t, when: when(t, now), text: `${names}上工（${at.map((s) => `${TEAM_LABELS[s.team] ?? s.team} ${s.headcount} 人`).join('、')}）` })
  }
  if (world) {
    const dep = world.dock.next_departure
    if (dep && ms(dep) > now) other.push({ key: 'dep', t: ms(dep), when: when(ms(dep), now), text: `出貨車離廠・待出貨 ${world.finished_goods.waiting_ship} 條` })
    const arr = world.dock.next_arrival
    if (arr && ms(arr) > now) other.push({ key: 'arr', t: ms(arr), when: when(ms(arr), now), text: '進貨車到廠' })
    const po = (world.purchase_orders ?? []).filter((p) => p.status === '在途' && ms(p.eta) > now).sort((a, b) => ms(a.eta) - ms(b.eta))[0]
    if (po) other.push({ key: `po-${po.id}`, t: ms(po.eta), when: when(ms(po.eta), now), text: `${po.name}到貨` })
    const mt = (world.maintenance?.upcoming ?? []).filter((m) => ms(m.start) > now).sort((a, b) => ms(a.start) - ms(b.start))[0]
    if (mt) other.push({ key: `mt-${mt.id}`, t: ms(mt.start), when: when(ms(mt.start), now), text: `${machineLabel(mt.machine_id)}${mt.title}` })
  }
  // presses are many: keep room for the shift / truck / maintenance lines
  const picked = [...press.sort((a, b) => a.t - b.t).slice(0, Math.max(2, n - other.length)), ...other]
  const end = factoryMidnight(now) + 86_400_000          // 「接下來」只列今天
  return picked.filter((u) => u.t < end).sort((a, b) => a.t - b.t).slice(0, n)
}
