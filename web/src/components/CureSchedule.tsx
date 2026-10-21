// 硫化機排程, moved from 人力排程 into 產線監控 (issue #102; Kyle, 2026-10-22: 「把硫化機排程移進去產線監控」). Laid out like the
// approved 人力排程 draft: today + tomorrow from 06:00, one row per press, one bar per tyre with its order number and
// coloured by size, one line for the version in use. Kept from the old Gantt: the jobs the version in use moved to
// another press (dashed outline, compared with the version it replaced) and presses out of service (the tyre in the mold
// is held for QC, so its bar ends at the stop and nothing new is drawn on that press). Its neighbour 「接下來要有人在場」
// lists the next de-molds and mold-ins in words: each one needs a person at the press.
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { DashboardSnapshot, ScheduleJob, ScheduleVersion } from '../contracts'
import { CURE_PRESS_IDS, ML_PRESSES, ML_RANGE, SIZE_LABEL, machineLabel } from '../factory'
import { factoryMidnight, fmtHM, fmtMD, ms } from '../format'
import { plain } from '../plain'
import { isAbnormal } from '../store'
import { useWarRoom } from '../useWarRoom'
import { heldAt } from './MachineDrawer'
import { Panel, RISK_LABEL, useWidth } from './common'

const H = 3_600_000

/** Where the 48 h window starts: the 00/06/12/18 o'clock mark 2–8 h before now, so the past stays a thin strip and the
 *  rest of today and all of tomorrow show (at 10:24 → 06:00, the draft's 「今天＋明天」; at 03:39 → 00:00). */
function windowStart(now: number): number {
  const t = now - 2 * H
  const m = factoryMidnight(t)
  return m + Math.floor((t - m) / (6 * H)) * 6 * H
}

/** Presses out of service (page 11) → the time they stopped; their in-mold tyre is held for QC. */
function outOfService(s: DashboardSnapshot | null, now: number): Record<string, number> {
  const out: Record<string, number> = {}
  for (const m of s?.machines ?? []) {
    if (m.line !== 'CURE' || m.status !== 'out_of_service') continue
    const t = heldAt(s?.safety_actions ?? [], s?.events ?? [], m.machine_id)
    out[m.machine_id] = t ? ms(t) : now
  }
  return out
}

/** 「10/04 01:00」 when not today, else 「01:00」. */
function when(t: number, now: number): string {
  return factoryMidnight(t) === factoryMidnight(now) ? fmtHM(t) : `${fmtMD(t)} ${fmtHM(t)}`
}

export function CureSchedule({ onAgent }: { onAgent?: (eventId?: string) => void }) {
  const { state, transport } = useWarRoom()
  const [versions, setVersions] = useState<ScheduleVersion[]>([])
  const [msg, setMsg] = useState<string | null>(null)
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const load = useCallback(() => {
    transport?.scheduleVersions().then(setVersions).catch((e) => setMsg(`排程讀取失敗：${e}`))
  }, [transport])
  useEffect(() => { load() }, [load, state.scheduleRev])

  const active = versions.find((v) => v.status === 'active') ?? state.schedule ?? null
  const prev = active ? versions.find((v) => v.version === active.parent_version) : undefined
  // proposed versions wait on an approval; once none is pending (approved, rejected or expired) they wait for nobody
  const proposed = state.pending.length ? versions.filter((v) => v.status === 'proposed') : []
  const rush = useMemo(() => new Set((state.world?.orders.items ?? []).filter((o) => o.rush).map((o) => o.id)), [state.world?.orders.items])
  const out = outOfService(state.snapshot, now)
  const bad = new Set((state.snapshot?.machines ?? []).filter((m) => m.line === 'CURE' && isAbnormal(m.status)).map((m) => m.machine_id))

  // one-click rollback (copied from 人力排程): the server previews it first and never re-activates a version that loads
  // a press now out of service; the supervisor confirms, which is recorded as an approval
  const rollback = async () => {
    if (!transport || !active) return
    setMsg(null)
    try {
      let to: number | undefined
      if (transport.rollbackPreview) {
        const pv = await transport.rollbackPreview()
        if (!pv.feasible) { setMsg(`無法回復到第 ${pv.to_version} 版：${pv.reasons.join('；')}`); return }
        const moved = pv.moved.slice(0, 5).map((m) => `・${m.order_id} ${machineLabel(m.from_press)} → ${machineLabel(m.to_press)}`)
        const text = [
          `回復排程 第 ${pv.from_version} 版 → 第 ${pv.to_version} 版（${RISK_LABEL[pv.risk]}）`,
          `第 ${pv.to_version} 版：${plain(pv.target_reason)}`,
          `異動 ${pv.moved_count} 筆入模${pv.added || pv.removed ? `，新增 ${pv.added}／移除 ${pv.removed}` : ''}`,
          ...moved,
          ...(pv.staffing_changes.length ? [`人力：${pv.staffing_changes.join('、')}`] : []),
          '', '確認由主管核准並回復？',
        ].join('\n')
        if (!window.confirm(text)) { setMsg('已取消回復'); return }
        to = pv.to_version
      } else if (!window.confirm('回復上一版排程？')) { setMsg('已取消回復'); return }
      const v = await transport.rollback(to, true)
      setMsg(v ? `已回復到第 ${v.version} 版` : '沒有可回復的上一版')
      load()
    } catch (e) { setMsg(`回復失敗：${e}`) }
  }

  const ver = active && (
    <span className="cs-ver" data-testid="cs-version">
      <span title={`${plain(active.reason)}（${fmtMD(active.created_at)} ${fmtHM(active.created_at)}）`}>使用中：<b>第 {active.version} 版</b></span>
      {proposed.length > 0 && (
        <button type="button" className="cs-wait" onClick={() => onAgent?.(state.pending[0]?.event_id)} data-testid="cs-proposed">
          另有 {proposed.length} 版等你核准 →
        </button>
      )}
      {active.parent_version != null && <button type="button" className="btn-ghost cs-back" onClick={rollback} title="一鍵回復上一版排程">↶ 回復上一版</button>}
    </span>
  )
  return (
    <Panel title="硫化機排程（今天＋明天）" className="cs-panel" fold="phone" testId="cure-schedule"
      summary={active ? `使用中第 ${active.version} 版${proposed.length ? `・另有 ${proposed.length} 版等你核准` : ''}` : undefined} right={ver}>
      <p className="mon-sub">一條＝一條胎，上面是訂單編號；顏色是胎型。</p>
      {msg && <p className="note">{msg}</p>}
      {active && !isNaN(now)
        ? <Gantt version={active} prev={prev} now={now} out={out} bad={bad} rush={rush} />
        : <p className="muted">等待排程…</p>}
    </Panel>
  )
}

function Gantt({ version, prev, now, out, bad, rush }: { version: ScheduleVersion; prev?: ScheduleVersion; now: number; out: Record<string, number>; bad: Set<string>; rush: Set<string> }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const t0 = windowStart(now)
  const t1 = t0 + 48 * H
  const LABEL = 92, ROW = 30, TOP = 24
  // never narrower than 640 px: on a phone the chart scrolls sideways (.gantt) instead of squeezing 48 h into 340 px
  const W = Math.max(640, width)
  const x = (t: number) => LABEL + ((t - t0) / (t1 - t0)) * (W - LABEL - 6)
  const presses: string[] = CURE_PRESS_IDS
  const Hh = TOP + presses.length * ROW + 6
  const step = W < 760 ? 12 : 6   // hours between labels, so they never overlap
  const ticks: number[] = []
  for (let t = t0; t < t1; t += step * H) ticks.push(t)
  // jobs this version moved to another press, compared with the version it replaced
  // (paired per order by mold-in order, since a moved job may get a new id)
  const moved = useMemo(() => {
    const byOrder = (list: ScheduleJob[]) => {
      const m = new Map<string, ScheduleJob[]>()
      for (const j of [...list].sort((a, b) => ms(a.mold_in) - ms(b.mold_in))) m.set(j.order_id, [...(m.get(j.order_id) ?? []), j])
      return m
    }
    const res = new Map<string, string>()
    if (!prev) return res
    const before = byOrder(prev.schedule.jobs)
    for (const [order, list] of byOrder(version.schedule.jobs)) {
      const old = before.get(order) ?? []
      list.forEach((j, k) => { if (old[k] && old[k].press_id !== j.press_id) res.set(j.id, old[k].press_id) })
    }
    return res
  }, [version, prev])
  const jobs = version.schedule.jobs.filter((j) => ms(j.demold) > t0 && ms(j.mold_in) < t1)
  const anyMoved = jobs.some((j) => moved.has(j.id))
  const anyOut = presses.some((p) => out[p] != null)
  return (
    <div ref={ref} className="gantt cs-gantt">
      <svg width={W} height={Hh} role="img" aria-label="硫化機排程：今天＋明天">
        {ticks.map((t) => {
          const day = Math.abs(t - factoryMidnight(t)) < 1000
          return (
            <g key={t}>
              <line x1={x(t)} x2={x(t)} y1={TOP - 4} y2={Hh - 4} className={`cs-tick ${day ? 'day' : ''}`} />
              <text x={x(t) + 3} y={14} className="cs-tick-t">{day ? fmtMD(t) : fmtHM(t)}</text>
            </g>
          )
        })}
        {presses.map((p, i) => {
          const y = TOP + i * ROW
          return (
            <g key={p}>
              <text x={0} y={y + 19} className={`cs-press ${bad.has(p) ? 'bad' : ''}`}>
                {Number(p.slice(5))} 號機{out[p] != null ? <tspan className="cs-out"> 停用</tspan> : ML_PRESSES.includes(p) && <tspan className="cs-ml"> 中大</tspan>}
              </text>
              <line x1={LABEL} x2={W} y1={y + ROW - 0.5} y2={y + ROW - 0.5} className="cs-row" />
            </g>
          )
        })}
        {jobs.map((j) => {
          const i = presses.indexOf(j.press_id)
          if (i < 0) return null
          const stop = out[j.press_id]
          // out of service: a job not yet in the mold is not drawn; the one in the mold ends at the stop (扣模待品管)
          if (stop != null && ms(j.mold_in) >= stop) return null
          const held = stop != null && ms(j.demold) > stop
          const end = held ? stop : ms(j.demold)
          if (end <= t0) return null
          const a = Math.max(ms(j.mold_in), t0), b = Math.min(end, t1)
          const y = TOP + i * ROW + 4
          const from = moved.get(j.id) ?? null
          const isRush = rush.has(j.order_id)
          const head = `訂單 ${j.order_id}${isRush ? '（急單）' : ''}・${SIZE_LABEL[j.size]}・${machineLabel(j.press_id)}`
          const tip = held
            ? `${head}\n入模 ${fmtMD(j.mold_in)} ${fmtHM(j.mold_in)} → ${fmtMD(stop)} ${fmtHM(stop)} 停用\n扣模待品管，沒有出模時間`
            : `${head}\n入模 ${fmtMD(j.mold_in)} ${fmtHM(j.mold_in)} → 出模 ${fmtMD(j.demold)} ${fmtHM(j.demold)}（硫化 ${j.cure_h} 小時）`
          const px = x(b) - x(a)
          const label = `${j.order_id}${isRush ? '・急單' : ''}`
          return (
            <g key={j.id}>
              <rect x={x(a)} y={y} width={Math.max(1, px)} height={ROW - 8} rx={4}
                className={`cs-job sz-${j.size}${held ? ' held' : ''}${from ? ' moved' : ''}`}>
                <title>{from ? `${tip}\n這一版從 ${machineLabel(from)} 移過來` : tip}</title>
              </rect>
              {px > 64 && <text x={x(a) + 6} y={y + 15} className="cs-job-t">{label}{from && px > 150 ? `・自 ${Number(from.slice(5))} 號` : ''}</text>}
              {held && stop <= t1 && (
                <g className="cs-held">
                  <line x1={x(stop)} x2={x(stop)} y1={y - 2} y2={y + ROW - 6} />
                  <text x={x(stop) + 5} y={y + 15}>停用・扣模待品管</text>
                  <title>{tip}</title>
                </g>
              )}
            </g>
          )
        })}
        {now >= t0 && now <= t1 && <line x1={x(now)} x2={x(now)} y1={TOP - 6} y2={Hh - 4} className="cs-now"><title>{`現在 ${fmtHM(now)}`}</title></line>}
      </svg>
      <div className="mon-legend">
        <span><i className="cs-lg sz-S" />小胎</span><span><i className="cs-lg sz-M" />中胎</span><span><i className="cs-lg sz-L" />大胎</span>
        <span><i className="cs-lg now" />現在 {fmtHM(now)}</span>
        {anyMoved && <span><i className="cs-lg moved" />虛線框＝這一版換了機台（對照第 {prev?.version} 版）</span>}
        {anyOut && <span><i className="cs-lg out" />停用的機台不再排入</span>}
        <span>{ML_RANGE} 號機可做中、大胎</span>
      </div>
    </div>
  )
}

/** The next de-molds and mold-ins of the version in use: each one needs a person at the press, night or day.
 *  `inline` gives just the first one in words, for 產線監控's headline when nothing else needs the supervisor. */
export function NextAtPress({ limit = 6, inline = false }: { limit?: number; inline?: boolean }) {
  const { state } = useWarRoom()
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const out = outOfService(state.snapshot, now)
  const jobs = state.schedule?.schedule.jobs ?? []
  const items: { t: number; kind: '出模' | '入模'; j: ScheduleJob }[] = []
  if (!isNaN(now)) {
    for (const j of jobs) {
      const stop = out[j.press_id]
      if (stop != null && ms(j.mold_in) >= stop) continue   // not going into a press that is out of service
      if (ms(j.mold_in) >= now) items.push({ t: ms(j.mold_in), kind: '入模', j })
      if (ms(j.demold) >= now && !(stop != null && ms(j.demold) > stop)) items.push({ t: ms(j.demold), kind: '出模', j })
    }
  }
  items.sort((a, b) => a.t - b.t || (a.kind === b.kind ? 0 : a.kind === '出模' ? -1 : 1))
  const list = items.slice(0, limit)
  if (inline) {
    const n = list[0]
    return n ? <>{when(n.t, now)} {Number(n.j.press_id.slice(5))} 號機{n.kind}</> : null
  }
  return (
    <Panel title="接下來要有人在場" className="cs-next" testId="next-at-press">
      <p className="mon-sub">出模要有人把胎取出，入模要有人放下一條胎。</p>
      {list.length === 0 ? <p className="muted">排程裡沒有接下來的出模、入模。</p> : (
        <ul className="cs-next-list">
          {list.map(({ t, kind, j }) => (
            <li key={`${j.id}-${kind}`}>
              <b className="t">{when(t, now)}</b>
              <span className={`cs-k ${kind === '出模' ? 'dm' : 'mi'}`}>{kind}</span>
              <span>{Number(j.press_id.slice(5))} 號機・{j.order_id}（{SIZE_LABEL[j.size]}）</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}
