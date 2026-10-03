import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CapacityReport, Event, EventState, ScheduleVersion, Shift, StepRun } from '../contracts'
import { CAPACITY_CONDITIONS, CAPACITY_TARGETS, CURE_PRESS_IDS, LABOR_TEAMS, LEAD_TIME_WEEKS, ML_PRESSES, SIZE_LABEL, TEAM_LABELS, machineLabel } from '../factory'
import { TOOL_LABEL, resultText, staffingChoice, stepLabel } from '../stepText'
import { factoryMidnight, fmtHM, fmtMD, fmtWeekday, ms } from '../format'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { MarkedText, Panel, RISK_LABEL, STATE_LABEL, TimelineTable, useWidth } from './common'

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
  const events = state.snapshot?.events ?? []
  const ev = events.find((e) => e.id === eventId) ?? primaryEvent(events)
  const rollback = async () => {
    if (!transport || !active) return
    setMsg(null)
    try {
      let to: number | undefined
      if (transport.rollbackPreview) {
        // gate first: never re-activate a version that loads a press now out of service
        const pv = await transport.rollbackPreview()
        if (!pv.feasible) { setMsg(`無法回復到 v${pv.to_version}：${pv.reasons.join('；')}`); return }
        const moved = pv.moved.slice(0, 5).map((m) => `・${m.order_id} ${machineLabel(m.from_press)} → ${machineLabel(m.to_press)}`)
        const text = [
          `回復排程 v${pv.from_version} → v${pv.to_version}（${RISK_LABEL[pv.risk]}）`,
          `v${pv.to_version}：${pv.target_reason}`,
          `異動 ${pv.moved_count} 筆入模${pv.added || pv.removed ? `，新增 ${pv.added}／移除 ${pv.removed}` : ''}`,
          ...moved,
          ...(pv.staffing_changes.length ? [`人力：${pv.staffing_changes.join('、')}`] : []),
          '', '確認由主管核准並回復？',
        ].join('\n')
        if (!window.confirm(text)) { setMsg('已取消回復'); return }
        to = pv.to_version
      } else if (!window.confirm('回復上一版排程？')) { setMsg('已取消回復'); return }
      const v = await transport.rollback(to, true)
      setMsg(v ? `已回復到 v${v.version}` : '沒有可回復的上一版')
      loadVersions()
    } catch (e) { setMsg(`回復失敗：${e}`) }
  }

  return (
    <div className="schedule">
      <Panel title="硫化機排程（以出模時刻為中心）" num="⑤" className="gantt-panel"
        right={<VersionBar versions={versions} active={active} onRollback={rollback} />}>
        {msg && <p className="note">{msg}</p>}
        {active ? <Gantt version={active} shifts={shifts} now={now} focusPress={ev?.machine_id} /> : <p className="muted">等待排程…</p>}
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
      <Panel title="事件時間軸" num="⑦" className="tl-panel" right={ev ? <span className="muted small">{ev.id}</span> : undefined}>
        <TimelineTable items={ev?.timeline ?? []} />
      </Panel>
      {cap && <CapacityPanel cap={cap} />}
    </div>
  )
}

function VersionBar({ versions, active, onRollback }: { versions: ScheduleVersion[]; active: ScheduleVersion | null; onRollback: () => void }) {
  return (
    <div className="versions">
      {versions.map((v) => (
        <span key={v.version} className={`ver st-${v.status}`} title={`${v.reason}（${fmtMD(v.created_at)} ${fmtHM(v.created_at)}）`}>
          v{v.version}<small>{VSTATUS[v.status]}</small>
        </span>
      ))}
      <button className="btn-ghost" onClick={onRollback} disabled={!active || active.parent_version == null} title="一鍵回復上一版排程">↶ 回復上一版</button>
    </div>
  )
}
const VSTATUS: Record<ScheduleVersion['status'], string> = { draft: '草稿', proposed: '待核准', active: '生效', superseded: '已取代', rolled_back: '已回復' }

function Gantt({ version, shifts, now, focusPress }: { version: ScheduleVersion; shifts: Shift[]; now: number; focusPress?: string }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const t0 = Math.floor((now - 6 * H) / H) * H
  const t1 = t0 + 90 * H
  const LABEL = 64, ROW = 24, TOP = 22
  const W = Math.max(320, width)
  const x = (t: number) => LABEL + ((t - t0) / (t1 - t0)) * (W - LABEL - 6)
  const presses: string[] = CURE_PRESS_IDS
  const Hh = TOP + presses.length * ROW + 6
  // staffed windows for de-mold / mold-in: curing team + body night shift
  const staffed = shifts.filter((s) => s.team === 'curing' || (s.team === 'body' && s.name.includes('夜')))
  const ticks: number[] = []
  for (let t = factoryMidnight(t0); t <= t1; t += 6 * H) if (t >= t0) ticks.push(t)
  const jobs = version.schedule.jobs.filter((j) => ms(j.demold) > t0 && ms(j.mold_in) < t1)
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
            <text x={x(t) + 3} y={12} className="g-tick-t">{Math.abs(t - factoryMidnight(t)) < 1000 ? `${fmtMD(t)}（${fmtWeekday(t)}）` : fmtHM(t)}</text>
          </g>
        ))}
        {presses.map((p, i) => (
          <g key={p}>
            <text x={4} y={TOP + i * ROW + 15} className={`g-press ${p === focusPress ? 'focus' : ''}`}>{Number(p.slice(5))} 號{ML_PRESSES.includes(p) ? ' M/L' : ''}</text>
            <line x1={LABEL} x2={W} y1={TOP + (i + 1) * ROW - 0.5} y2={TOP + (i + 1) * ROW - 0.5} className="g-row" />
          </g>
        ))}
        {jobs.map((j) => {
          const i = presses.indexOf(j.press_id)
          if (i < 0) return null
          const a = Math.max(ms(j.mold_in), t0), b = Math.min(ms(j.demold), t1)
          const y = TOP + i * ROW + 3
          const hot = j.order_id === '#A-311'
          return (
            <g key={j.id}>
              <rect x={x(a)} y={y} width={Math.max(1, x(b) - x(a))} height={ROW - 7} rx={3} className={`g-job sz-${j.size} ${hot ? 'hot' : ''} ${j.frozen ? 'frozen' : ''}`}>
                <title>{`${j.order_id}・${SIZE_LABEL[j.size]}・${machineLabel(j.press_id)}\n入模 ${fmtMD(j.mold_in)} ${fmtHM(j.mold_in)} → 出模 ${fmtMD(j.demold)} ${fmtHM(j.demold)}（${j.cure_h} h）\n貼合 ${fmtHM(j.bond_start)}・A 線投料 ${fmtHM(j.a_feed_start)}`}</title>
              </rect>
              {x(b) - x(a) > 54 && <text x={x(a) + 4} y={y + 12} className="g-job-t">{j.order_id}</text>}
              {ms(j.demold) <= t1 && <path d={`M${x(ms(j.demold))},${y - 2} l-4,-5 h8 z`} className="g-demold"><title>{`出模 ${fmtHM(j.demold)}`}</title></path>}
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
        <span><i className="lg sz-S" />小胎 24 h</span><span><i className="lg sz-M" />中胎 28 h</span><span><i className="lg sz-L" />大胎 34 h</span>
        <span><i className="lg demold" />出模</span><span><i className="lg staffed" />有人時段（硫化機組 6–15・14–23／胎體夜班 20–05）</span>
        <span className="muted">版本 v{version.version}・{version.reason}</span>
      </div>
    </div>
  )
}

const CHAIN: EventState[] = ['detected', 'classify', 'plan', 'simulate', 'gate', 'await_approval', 'execute', 'verify', 'closed']

function PlanPanel({ ev, events, onPick, visited }: { ev: Event | null; events: Event[]; onPick: (id: string) => void; visited: EventState[] }) {
  const cur = ev?.state
  const seen = new Set(visited)
  if (cur) { const idx = CHAIN.indexOf(cur === 'replan' ? 'verify' : cur); CHAIN.slice(0, Math.max(0, idx)).forEach((s) => seen.add(s)) }
  return (
    <Panel title="指揮官步驟計畫" num="⑥" className="plan-panel"
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
            {(ev.replans > 0 || cur === 'replan') && <span className={`ch replan ${cur === 'replan' ? 'cur' : 'seen'}`}>↺ 換假設 ×{Math.max(1, ev.replans)}</span>}
            {cur === 'safe_stop' && <span className="ch stop cur">■ 安全停止</span>}
          </div>
          {ev.goal && <p className="goal"><span className="k">目標</span>{ev.goal}</p>}
          {ev.hypotheses.length > 0 && (
            <ul className="hyps">
              {ev.hypotheses.map((h) => (
                <li key={h.id} className={`hyp h-${h.status}`}>
                  <b>{h.id}</b> {h.statement}
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

/** A hypothesis never tested by a reading stays 驗證中 only while the event is open. On close it is
 *  handed to maintenance when the event took a machine out or called maintenance (page 11 H2). */
function hypLabel(status: string, ev: Event): string {
  if (status === 'rejected') return '不成立'
  if (status === 'confirmed') return '成立'
  if (ev.state !== 'closed' && ev.state !== 'safe_stop') return '驗證中'
  const all = [...(ev.prior_rounds ?? []).flatMap((r) => r.steps), ...ev.steps]
  const handed = all.some((s) => s.state === 'ok' && s.step.tool === 'notify_teams' && ['machine_out', 'maintenance'].includes(String(s.step.args.kind)))
  return handed ? '未驗證・交維修' : '未驗證'
}

function StepRow({ s, staffing }: { s: StepRun; staffing?: string }) {
  const summary = s.result?.ok ? resultText(s.step.tool, s.step.args, s.result.data, { staffing }) : s.result?.error ? `錯誤：${s.result.error.slice(0, 60)}` : ''
  const label = stepLabel(s.step.id)
  return (
    <li className={`step ss-${s.state}`}>
      <span className="step-id" title={s.step.id}>{label}</span>
      <div className="step-b">
        <div className="step-t"><code title={s.step.tool}>{TOOL_LABEL[s.step.tool] ?? s.step.tool}</code>{s.step.depends_on.length > 0 && <small className="muted">依賴 {s.step.depends_on.map(stepLabel).join('、')}</small>}
          {s.gate?.risk && <small className={`risk r-${s.gate.risk}`} title={RISK_LABEL[s.gate.risk]}>{s.gate.risk}</small>}</div>
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
    <Panel title="人力與班表（24 h）" num="⑧" className="labor-panel"
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
    <Panel title="每台硫化機週產量與交期" num="⑨" className="cap-panel" right={<span className="muted small">大字＝提案 p.6・小字＝本機模擬</span>}>
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
