import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CapacityReport, Event, EventState, ScheduleJob, ScheduleVersion, Shift, StepRun } from '../contracts'
import { CAPACITY_CONDITIONS, CAPACITY_TARGETS, CURE_PRESS_IDS, LABOR_TEAMS, LEAD_TIME_WEEKS, ML_PRESSES, SIZE_LABEL, TEAM_LABELS, machineLabel } from '../factory'
import { TOOL_LABEL, isRevoke, resultText, staffingChoice, stepLabel } from '../stepText'
import { RISK_SHORT, hypLabel, hypName, plain } from '../plain'
import { factoryMidnight, fmtHM, fmtMD, fmtWeekday, ms } from '../format'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { heldAt } from './MachineDrawer'
import { MarkedText, Panel, RISK_LABEL, STATE_LABEL, useWidth } from './common'

const H = 3_600_000

export function ScheduleTab() {
  const { state, transport } = useWarRoom()
  const [versions, setVersions] = useState<ScheduleVersion[]>([])
  const [shifts, setShifts] = useState<Shift[]>([])
  const [current, setCurrent] = useState<Shift[]>([])
  const [cap, setCap] = useState<CapacityReport | null>(null)
  const [eventId, setEventId] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)

  const loadVersions = useCallback(() => {
    transport?.scheduleVersions().then(setVersions).catch((e) => setMsg(`排程版本讀取失敗：${e}`))
  }, [transport])
  useEffect(() => { loadVersions() }, [loadVersions, state.scheduleRev])
  const day = isNaN(now) ? 0 : Math.floor(now / (6 * H))
  useEffect(() => {
    if (!transport) return
    transport.shifts().then(setShifts).catch(() => {})
    transport.currentShifts().then(setCurrent).catch(() => {})
    transport.capacity().then(setCap).catch(() => {})
  }, [transport, day])

  const active = versions.find((v) => v.status === 'active') ?? state.schedule ?? null
  const orderInfo = useMemo(() => new Map((state.world?.orders.items ?? []).map((o) => [o.id, { customer: o.customer, due: o.due, rush: o.rush }])), [state.world?.orders.items])
  const events = state.snapshot?.events ?? []
  const ev = events.find((e) => e.id === eventId) ?? primaryEvent(events)
  // presses out of service (page 11): their in-mold tire is held for QC, so its bar ends at the stop
  const outAt: Record<string, number> = {}
  for (const m of state.snapshot?.machines ?? []) {
    if (m.line !== 'CURE' || m.status !== 'out_of_service') continue
    const t = heldAt(state.snapshot?.safety_actions ?? [], events, m.machine_id)
    outAt[m.machine_id] = t ? ms(t) : now
  }
  const rollback = async () => {
    if (!transport || !active) return
    setMsg(null)
    try {
      let to: number | undefined
      if (transport.rollbackPreview) {
        // gate first: never re-activate a version that loads a press now out of service
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
      loadVersions()
    } catch (e) { setMsg(`回復失敗：${e}`) }
  }

  return (
    <div className="schedule">
      <Panel title="硫化機排程：11 台硫化機，每一條是一張訂單的一條胎" num="⑤" className="gantt-panel"
        right={<VersionBar versions={versions} active={active} onRollback={rollback} />}>
        <SchedHelp />
        {msg && <p className="note">{msg}</p>}
        {active ? <Gantt version={active} prev={versions.find((v) => v.version === active.parent_version)} shifts={shifts} now={now} focusPress={ev?.machine_id} outAt={outAt} orders={orderInfo} /> : <p className="muted">等待排程…</p>}
        {active && <NextUp jobs={active.schedule.jobs} now={now} orders={orderInfo} outAt={outAt} />}
        {active && (active.schedule.staffing_changes.length > 0 || active.schedule.warnings.length > 0) && (
          <div className="sched-notes">
            {active.schedule.staffing_changes.map((c, i) => <span key={i} className={`pill ${c.kind}`}>{c.kind === 'overtime' ? '加班' : '跨組調人'}：{c.label}</span>)}
            {active.schedule.warnings.map((w, i) => <span key={`w${i}`} className="pill warn">{w}</span>)}
          </div>
        )}
      </Panel>
      <div className="sched-row">
        <PlanPanel ev={ev} events={events} onPick={setEventId} visited={ev ? state.visited[ev.id] ?? [] : []} />
        <LaborPanel shifts={shifts} current={current} now={now} version={active} />
      </div>
      {/* UX declutter: ⑦ 事件時間軸 was removed from this page (生管 plans here; the event's timeline is on 產線監控 ③,
          「事件時間軸」 with 看全部). The other numbers stay as they are so ⑧ / ⑨ keep matching the docs. */}
      {cap && <CapacityPanel cap={cap} />}
    </div>
  )
}

function VersionBar({ versions, active, onRollback }: { versions: ScheduleVersion[]; active: ScheduleVersion | null; onRollback: () => void }) {
  return (
    <div className="versions">
      {versions.map((v) => (
        <span key={v.version} className={`ver st-${v.status}`} title={`${v.reason}（${fmtMD(v.created_at)} ${fmtHM(v.created_at)}）`}>
          第 {v.version} 版<small>{VSTATUS[v.status]}</small>
        </span>
      ))}
      <button className="btn-ghost" onClick={onRollback} disabled={!active || active.parent_version == null} title="一鍵回復上一版排程">↶ 回復上一版</button>
    </div>
  )
}
const VSTATUS: Record<ScheduleVersion['status'], string> = { draft: '草稿', proposed: '待核准', active: '生效', superseded: '已取代', rolled_back: '已回復' }

function Gantt({ version, prev, shifts, now, focusPress, outAt = {}, orders }: { version: ScheduleVersion; prev?: ScheduleVersion; shifts: Shift[]; now: number; focusPress?: string; outAt?: Record<string, number>; orders: OrderInfo }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const t0 = Math.floor((now - 6 * H) / H) * H
  const t1 = t0 + 90 * H
  const LABEL = 88, ROW = 26, TOP = 22
  // never narrower than 640 px: on a phone the chart scrolls sideways (.gantt overflow-x) instead of squeezing 90 h into 340 px
  const W = Math.max(640, width)
  const x = (t: number) => LABEL + ((t - t0) / (t1 - t0)) * (W - LABEL - 6)
  const presses: string[] = CURE_PRESS_IDS
  const Hh = TOP + presses.length * ROW + 6
  // staffed windows for de-mold / mold-in: curing team + body night shift
  const staffed = shifts.filter((s) => s.team === 'curing' || (s.team === 'body' && s.name.includes('夜')))
  const ticks: number[] = []
  const tickStep = W < 700 ? 24 : W < 1000 ? 12 : 6   // hours between labels, so labels never overlap
  for (let t = factoryMidnight(t0); t <= t1; t += tickStep * H) if (t >= t0) ticks.push(t)
  const jobs = version.schedule.jobs.filter((j) => ms(j.demold) > t0 && ms(j.mold_in) < t1)
  // jobs this version moved to another press, compared with the version it replaced
  // (paired per order by mold-in order, since a moved job may get a new id)
  const movedMap = useMemo(() => {
    const byOrder = (list: ScheduleJob[]) => {
      const m = new Map<string, ScheduleJob[]>()
      for (const j of [...list].sort((a, b) => ms(a.mold_in) - ms(b.mold_in))) m.set(j.order_id, [...(m.get(j.order_id) ?? []), j])
      return m
    }
    const out = new Map<string, string>()
    if (!prev) return out
    const before = byOrder(prev.schedule.jobs)
    for (const [order, list] of byOrder(version.schedule.jobs)) {
      const old = before.get(order) ?? []
      list.forEach((j, k) => { if (old[k] && old[k].press_id !== j.press_id) out.set(j.id, old[k].press_id) })
    }
    return out
  }, [version, prev])
  const movedFrom = (j: ScheduleJob) => movedMap.get(j.id) ?? null
  return (
    <div ref={ref} className="gantt">
      <svg width={W} height={Hh} role="img" aria-label="硫化機甘特圖">
        {staffed.map((s) => {
          const a = Math.max(ms(s.start), t0), b = Math.min(ms(s.end), t1)
          return b > a ? <rect key={s.id} x={x(a)} y={TOP - 4} width={x(b) - x(a)} height={presses.length * ROW + 4} className="g-staffed" /> : null
        })}
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={TOP - 6} y2={Hh} className={`g-tick ${Math.abs(t - factoryMidnight(t)) < 1000 ? 'day' : ''}`} />
            <text x={x(t) > W - 46 ? x(t) - 3 : x(t) + 3} y={12} textAnchor={x(t) > W - 46 ? 'end' : 'start'} className="g-tick-t">{Math.abs(t - factoryMidnight(t)) < 1000 ? (W < 700 ? fmtMD(t) : `${fmtMD(t)}（${fmtWeekday(t)}）`) : fmtHM(t)}</text>
          </g>
        ))}
        {presses.map((p, i) => (
          <g key={p}>
            <text x={4} y={TOP + i * ROW + 15} className={`g-press ${p === focusPress ? 'focus' : ''}`}>{Number(p.slice(5))} 號機{ML_PRESSES.includes(p) && <tspan className="g-ml" dx={4}>中大</tspan>}</text>
            <line x1={LABEL} x2={W} y1={TOP + (i + 1) * ROW - 0.5} y2={TOP + (i + 1) * ROW - 0.5} className="g-row" />
          </g>
        ))}
        {jobs.map((j) => {
          const i = presses.indexOf(j.press_id)
          if (i < 0) return null
          const stop = outAt[j.press_id]
          // out of service: a job not yet in the mold is not drawn; the in-mold one ends at the stop (扣模待品管)
          if (stop != null && ms(j.mold_in) >= stop) return null
          const held = stop != null && ms(j.demold) > stop
          const end = held ? stop : ms(j.demold)
          if (end <= t0) return null
          const a = Math.max(ms(j.mold_in), t0), b = Math.min(end, t1)
          const y = TOP + i * ROW + 3
          const hot = j.order_id === '#A-311'
          const from = movedFrom(j)
          const who = orders.get(j.order_id)?.customer
          const head = `訂單 ${j.order_id}${who ? `（${who}）` : ''}・${SIZE_LABEL[j.size]}・${machineLabel(j.press_id)}`
          const tip = held
            ? `${head}\n入模 ${fmtMD(j.mold_in)} ${fmtHM(j.mold_in)} → ${fmtMD(stop)} ${fmtHM(stop)} 停用\n扣模待品管，無出模時間`
            : `${head}\n入模 ${fmtMD(j.mold_in)} ${fmtHM(j.mold_in)} → 出模 ${fmtMD(j.demold)} ${fmtHM(j.demold)}（${j.cure_h} h）\n貼合 ${fmtHM(j.bond_start)}・A 線投料 ${fmtHM(j.a_feed_start)}`
          return (
            <g key={j.id}>
              <rect x={x(a)} y={y} width={Math.max(1, x(b) - x(a))} height={ROW - 7} rx={3} className={`g-job sz-${j.size} ${hot ? 'hot' : ''} ${j.frozen ? 'frozen' : ''} ${held ? 'held' : ''} ${from ? 'moved' : ''}`}>
                <title>{from ? `${tip}\n本版從 ${machineLabel(from)} 移來` : tip}</title>
              </rect>
              {x(b) - x(a) > 54 && <text x={x(a) + 4} y={y + 13} className="g-job-t">{barLabel(j, who, x(b) - x(a))}{from && x(b) - x(a) > 200 ? `・自 ${Number(from.slice(5))} 號` : ''}</text>}
              {held && stop <= t1 && (
                <g className="g-held">
                  <line x1={x(stop)} x2={x(stop)} y1={y - 2} y2={y + ROW - 5} />
                  <text x={x(stop) + 4} y={y + 12}>■ 停用・扣模待品管</text>
                  <title>{tip}</title>
                </g>
              )}
              {!held && ms(j.demold) <= t1 && <path d={`M${x(ms(j.demold))},${y - 2} l-4,-5 h8 z`} className="g-demold"><title>{`出模 ${fmtHM(j.demold)}`}</title></path>}
            </g>
          )
        })}
        {now >= t0 && now <= t1 && (
          <g>
            <line x1={x(now)} x2={x(now)} y1={TOP - 8} y2={Hh} className="g-now" />
            <text x={x(now) + 3} y={Hh - 2} className="g-now-t">現在 {fmtHM(now)}</text>
          </g>
        )}
      </svg>
      <div className="legend">
        <span><i className="lg sz-S" />小胎（硫化 24 h）</span><span><i className="lg sz-M" />中胎（28 h）</span><span><i className="lg sz-L" />大胎（34 h）</span>
        <span><i className="lg demold" />▼ 出模時刻</span><span><i className="lg now" />現在</span><span><i className="lg staffed" />有人時段（硫化機組 6–15・14–23／胎體夜班 20–05）</span>
        {prev && <span><i className="lg moved" />本版移動（虛線框，對照第 {prev.version} 版）</span>}
        <span className="muted">第 {version.version} 版・{plain(version.reason)}</span>
      </div>
    </div>
  )
}

type OrderInfo = Map<string, { customer: string; due: string; rush: boolean }>

/** What a bar says, longest first that fits: 「#A-311 台塑工業車輛・中胎」→「#A-311・中胎」→「#A-311」 (tooltip has all). */
function barLabel(j: ScheduleJob, who: string | undefined, px: number): string {
  const full = `${j.order_id} ${who ?? ''}・${SIZE_LABEL[j.size]}`
  if (who && px > full.length * 11 + 12) return full
  // ScheduleTab SchedHelp: these are order numbers, so the size goes with the code wherever it fits (about 84 px at
  // 11.5 px bold); a bar narrower than that shows the bare code and its tooltip says 訂單・客戶・胎型.
  if (px > 84) return `${j.order_id}・${SIZE_LABEL[j.size]}`
  return j.order_id
}

/** CJ, 2026-10-20: 「人力排程這個就不知道 S101 A311 這些是什麼意思…是指我們現在是只有做硫化機的排程因為它是速率決定步驟」. */
function SchedHelp() {
  return (
    <details className="sched-help" open>
      <summary>怎麼看這張圖</summary>
      <ul>
        <li><b>#S-101、#A-311、#W-0397 是訂單編號</b>，不是機台。長條上寫訂單號、客戶和胎型；把滑鼠移到長條上看入模、出模、貼合與投料時間。</li>
        <li><b>只排 11 台硫化機</b>：硫化一條胎要 24–34 小時，是全廠最慢、決定產量的一站（瓶頸、速率決定步驟）。硫化機排好了，前面的投料、貼合時間就跟著往回推，所以不另外排。</li>
        <li>每一列是一台硫化機（7–10 號可做中、大胎）；顏色是胎型；<b>▼ 是出模時刻</b>，要有人在場出模、入下一模，所以底色標出有人上班的時段。</li>
      </ul>
    </details>
  )
}

/** The next mold-ins in words, for a phone where the bars are too small to read. */
function NextUp({ jobs, now, orders, outAt }: { jobs: ScheduleJob[]; now: number; orders: OrderInfo; outAt: Record<string, number> }) {
  const next = jobs.filter((j) => ms(j.mold_in) >= now && outAt[j.press_id] == null).sort((a, b) => ms(a.mold_in) - ms(b.mold_in)).slice(0, 6)
  if (!next.length) return null
  return (
    <div className="sched-next">
      <h3 className="sub">接下來入模</h3>
      <table className="impact-t">
        <thead><tr><th>入模</th><th>硫化機</th><th>訂單</th><th>胎型</th><th>出模</th><th>交期</th></tr></thead>
        <tbody>{next.map((j) => {
          const o = orders.get(j.order_id)
          return (
            <tr key={j.id}>
              <td>{fmtMD(j.mold_in)} {fmtHM(j.mold_in)}</td><td>{Number(j.press_id.slice(5))} 號</td>
              <td><b>{j.order_id}</b>{o?.rush && <em className="tag rush">急單</em>}<small className="muted"> {o?.customer ?? ''}</small></td>
              <td>{SIZE_LABEL[j.size]}</td><td>{fmtMD(j.demold)} {fmtHM(j.demold)}</td><td>{o ? fmtMD(o.due) : '—'}</td>
            </tr>
          )
        })}</tbody>
      </table>
    </div>
  )
}

const CHAIN: EventState[] = ['detected', 'classify', 'plan', 'simulate', 'gate', 'await_approval', 'execute', 'verify', 'closed']

function PlanPanel({ ev, events, onPick, visited }: { ev: Event | null; events: Event[]; onPick: (id: string) => void; visited: EventState[] }) {
  const cur = ev?.state
  const seen = new Set(visited)
  if (cur) { const idx = CHAIN.indexOf(cur === 'replan' ? 'verify' : cur); CHAIN.slice(0, Math.max(0, idx)).forEach((s) => seen.add(s)) }
  // UX declutter: folded by default on every width (生管 reads the schedule; the steps are the AI's working detail);
  // the folded header still says which event and where it stands.
  return (
    <Panel title="指揮官步驟計畫" num="⑥" className="plan-panel" fold="always"
      summary={ev ? `${ev.source === 'supervisor' ? '主管指令' : machineLabel(ev.machine_id)}：${plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message)}・${STATE_LABEL[ev.state]}` : '目前沒有事件'}
      right={events.length > 1 ? (
        <select value={ev?.id} onChange={(e) => onPick(e.target.value)} aria-label="選擇事件">
          {events.map((e) => <option key={e.id} value={e.id}>{machineLabel(e.machine_id)}・{STATE_LABEL[e.state]}</option>)}
        </select>
      ) : undefined}>
      {!ev ? <p className="muted">目前沒有事件。指揮官（唯一的 LLM）只在事件偵測喚醒時才動。</p> : (
        <>
          <div className="chain" aria-label="狀態機">
            {CHAIN.map((s, i) => (
              <span key={s} className={`ch ${cur === s ? 'cur' : seen.has(s) ? 'seen' : ''}`}>
                {STATE_LABEL[s]}{i < CHAIN.length - 1 && <i>→</i>}
              </span>
            ))}
            {(ev.replans > 0 || cur === 'replan') && <span className={`ch replan ${cur === 'replan' ? 'cur' : 'seen'}`}>↺ 重新規劃 ×{Math.max(1, ev.replans)}</span>}
            {cur === 'safe_stop' && <span className="ch stop cur">■ 安全停止</span>}
          </div>
          {ev.goal && <p className="goal"><span className="k">目標</span>{ev.goal}</p>}
          {ev.hypotheses.length > 0 && (
            <ul className="hyps">
              {ev.hypotheses.map((h) => (
                <li key={h.id} className={`hyp h-${h.status}`}>
                  <b>{hypName(h.id)}</b> {plain(h.statement)}
                  {h.expectation && <small>預期：{h.expectation.within_min} 分內 {h.expectation.op} {h.expectation.value}°C</small>}
                  <em>{hypLabel(h.status, ev)}</em>
                </li>
              ))}
            </ul>
          )}
          {(ev.prior_rounds ?? []).map((r) => (
            <details key={r.round} className="prior-round">
              <summary>第{ROUND_NUM[r.round] ?? r.round}輪{r.hypothesis_id ? `（${r.hypothesis_id}）` : ''}・{r.steps.length} 步{r.goal ? `・${r.goal}` : ''}</summary>
              <ol className="steps">
                {r.steps.map((s) => <StepRow key={s.step.id} s={s} staffing={staffingChoice(r.steps)} />)}
              </ol>
            </details>
          ))}
          {(ev.prior_rounds ?? []).length > 0 && <p className="muted small round-now">第{ROUND_NUM[(ev.prior_rounds ?? []).length + 1] ?? (ev.prior_rounds ?? []).length + 1}輪{ev.state === 'closed' || ev.state === 'safe_stop' ? '（最後一輪）' : '（目前）'}</p>}
          <ol className="steps">
            {ev.steps.map((s) => <StepRow key={s.step.id} s={s} staffing={staffingChoice(ev.steps)} />)}
          </ol>
          {ev.outcome && <p className="outcome">結果：{ev.outcome}</p>}
        </>
      )}
    </Panel>
  )
}

const STEP_LABEL: Record<StepRun['state'], string> = { pending: '等待', running: '執行中', ok: '完成', failed: '失敗', skipped: '略過', awaiting_approval: '待核准', rejected: '駁回' }
const ROUND_NUM: Record<number, string> = { 1: '一', 2: '二', 3: '三', 4: '四', 5: '五' }

function StepRow({ s, staffing }: { s: StepRun; staffing?: string }) {
  const summary = s.result?.ok ? resultText(s.step.tool, s.step.args, s.result.data, { staffing }) : s.result?.error ? `錯誤：${s.result.error.slice(0, 60)}` : ''
  const label = stepLabel(s.step.id)
  return (
    <li className={`step ss-${s.state}`}>
      <span className="step-id" title={s.step.id}>{label}</span>
      <div className="step-b">
        <div className="step-t"><code title={s.step.tool}>{TOOL_LABEL[s.step.tool] ?? s.step.tool}</code>{s.step.depends_on.length > 0 && <small className="muted">依賴 {s.step.depends_on.map(stepLabel).join('、')}</small>}
          {s.gate?.risk && !isRevoke(s.step.id) && <small className={`risk r-${s.gate.risk}`} title={RISK_LABEL[s.gate.risk]}>{RISK_SHORT[s.gate.risk]}</small>}
          {isRevoke(s.step.id) && <small className="muted" title="主管後一輪改選，先前已執行的人力調動依此撤回並通知；不是另一項核准">因主管改選而撤回</small>}</div>
        <div className="step-p">{s.step.purpose}</div>
        {summary && <div className="step-r"><MarkedText text={summary} /></div>}
      </div>
      <span className="step-st">{STEP_LABEL[s.state]}</span>
    </li>
  )
}

function LaborPanel({ shifts, current, now, version }: { shifts: Shift[]; current: Shift[]; now: number; version: ScheduleVersion | null }) {
  const [mode, setMode] = useState<'opt' | 'cur'>('opt')
  const [ref, width] = useWidth<HTMLDivElement>()
  const d0 = isNaN(now) ? 0 : factoryMidnight(now)
  const d1 = d0 + 24 * H
  const list = mode === 'opt' ? shifts : current
  const LABEL = 96, ROW = 24, TOP = 20
  const W = Math.max(300, width)
  const x = (t: number) => LABEL + ((t - d0) / (d1 - d0)) * (W - LABEL - 8)
  const rows = ['__press', ...LABOR_TEAMS] as const
  const demolds = useMemo(() => (version?.schedule.jobs ?? []).map((j) => ms(j.demold)).filter((t) => t >= d0 && t < d1), [version, d0, d1])
  const Hh = TOP + rows.length * ROW + 4
  return (
    <Panel title="人力與班表（24 h）" num="⑧" className="labor-panel" fold="phone"
      right={<div className="seg sm"><button className={mode === 'cur' ? 'on' : ''} onClick={() => setMode('cur')}>現況</button><button className={mode === 'opt' ? 'on' : ''} onClick={() => setMode('opt')}>優化班表</button></div>}>
      <div ref={ref} className="labor">
        <svg width={W} height={Hh} role="img" aria-label="人力時間軸">
          {[0, 6, 12, 18, 24].map((h) => (
            <g key={h}><line x1={x(d0 + h * H)} x2={x(d0 + h * H)} y1={TOP - 4} y2={Hh} className="g-tick" /><text x={x(d0 + h * H)} y={12} textAnchor={h === 24 ? 'end' : 'middle'} className="g-tick-t">{h} 時</text></g>
          ))}
          {rows.map((r, i) => {
            const y = TOP + i * ROW
            if (r === '__press') return (
              <g key={r}>
                <text x={4} y={y + 15} className="l-row-t strong">硫化機（運轉）</text>
                <rect x={x(d0)} y={y + 5} width={x(d1) - x(d0)} height={ROW - 10} rx={4} className="l-run" />
                {demolds.map((t, k) => <line key={k} x1={x(t)} x2={x(t)} y1={y + 1} y2={y + ROW - 1} className="l-demold"><title>{`出模 ${fmtHM(t)}`}</title></line>)}
              </g>
            )
            const sh = list.filter((s) => s.team === r && ms(s.end) > d0 && ms(s.start) < d1)
            return (
              <g key={r}>
                <text x={4} y={y + 15} className="l-row-t">{TEAM_LABELS[r]}</text>
                <rect x={x(d0)} y={y + 5} width={x(d1) - x(d0)} height={ROW - 10} rx={4} className="l-none" />
                {sh.map((s) => {
                  const a = Math.max(ms(s.start), d0), b = Math.min(ms(s.end), d1)
                  const short = s.required != null && s.required > s.headcount
                  return (
                    <g key={s.id}>
                      <rect x={x(a)} y={y + 5} width={Math.max(1, x(b) - x(a))} height={ROW - 10} rx={4} className={`l-on ${short ? 'short' : ''}`}>
                        <title>{`${TEAM_LABELS[s.team]} ${s.name} ${fmtHM(s.start)}–${fmtHM(s.end)}・${s.headcount}${s.required != null ? `/${s.required}` : ''} 人`}</title>
                      </rect>
                      {short && x(b) - x(a) > 30 && <text x={x(a) + 4} y={y + 15} className="l-short">缺 {s.required! - s.headcount}</text>}
                    </g>
                  )
                })}
              </g>
            )
          })}
          {!isNaN(now) && <line x1={x(now)} x2={x(now)} y1={TOP - 6} y2={Hh} className="g-now" />}
        </svg>
        <div className="legend">
          <span><i className="lg run" />機台運轉</span><span><i className="lg on" />有人上班</span><span><i className="lg none" />無人</span><span><i className="lg demold" />出模</span>
          <span className="muted">{fmtMD(d0)}（{fmtWeekday(d0)}）{mode === 'cur' ? '・現況：多數組只上早班（p.4）' : '・優化：硫化機組 6–15／14–23 錯班（p.6）'}</span>
        </div>
      </div>
    </Panel>
  )
}

const CAP_SIM_NOTE = '現況：硫化機組早班 08–17 出入模；只延長班表：硫化機組 06–15、14–23 錯班，胎體／胎面膠組夜班 20–05 兼出入模，週六夜班到週日 05:00；AI 排出模時刻：同上，加週末不斷火（週六夜班送最後一輪，週一清晨 00–05 出模）。'
const near = (a: number, b: number) => Math.abs(a - b) < 0.05

function CapacityPanel({ cap }: { cap: CapacityReport }) {
  const sim = (label: string) => cap.rows.find((r) => r.label === label)
  const cell = (target: number, got: number | undefined) => (
    <td><b>{target}</b>{got != null && !near(got, target) && <small className="cap-sim" title="本機 40 週離散事件模擬">本機 {got}</small>}</td>
  )
  const pair = (t: [number, number], g: [number, number]) => (
    <>
      <b>{t[0].toFixed(1)} → {t[1].toFixed(1)} 週</b>
      {!(near(t[0], g[0]) && near(t[1], g[1])) && <small className="cap-sim">本機模擬 {g[0].toFixed(1)} → {g[1].toFixed(1)} 週</small>}
    </>
  )
  return (
    <Panel title="每台硫化機週產量與交期" num="⑨" className="cap-panel" fold="phone" right={<span className="muted small">大字＝提案 p.6・小字＝本機模擬</span>}>
      <div className="cap">
        <table>
          <thead><tr><th>方案</th><th>小胎</th><th>中胎</th><th>大胎</th></tr></thead>
          <tbody>{CAPACITY_TARGETS.map((t, i) => {
            const g = sim(t.label)
            return <tr key={t.label} className={i === CAPACITY_TARGETS.length - 1 ? 'best' : ''}><td>{t.label}</td>{cell(t.small, g?.small)}{cell(t.medium, g?.medium)}{cell(t.large, g?.large)}</tr>
          })}</tbody>
        </table>
        <div className="cap-big">
          <div><span className="muted small">中、大胎各 100 條</span>{pair(LEAD_TIME_WEEKS.ml, cap.ml_100_weeks)}</div>
          <div><span className="muted small">小胎 100 條</span>{pair(LEAD_TIME_WEEKS.small, cap.small_100_weeks)}</div>
        </div>
      </div>
      <p className="muted small">模擬條件：{CAPACITY_CONDITIONS}。{CAP_SIM_NOTE}</p>
      <p className="muted small">提案數字為 p.6 試算；本機模擬為 40 週離散事件模擬的穩態值，與提案不同處以小字並列，不改寫提案數字。數值由排程求解器與公式計算，不由 AI 推測。</p>
    </Panel>
  )
}
