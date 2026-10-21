import { useCallback, useEffect, useState } from 'react'
import type { ApprovalRequest, ScheduleVersion, Shift } from '../contracts'
import { CAPACITY_CONDITIONS, LEAD_TIME_WEEKS, SIZE_LABEL, TEAM_LABELS, machineLabel } from '../factory'
import { plain } from '../plain'
import { factoryMidnight, fmtHM, fmtMD, fmtWeekday, isoTaipei, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { heldAt } from './MachineDrawer'
import { Panel, RISK_LABEL } from './common'
import { PeopleChart } from './schedule/PeopleChart'
import { gapOf, H, isNight, needsOf, proposalFor, staffingApproval, todayShifts, whenOf, type Need } from './schedule/model'
import './schedule.css'

// 人力排程 = the draft Kyle approved on 10-22 (.design/drafts/schedule/v2-simple.html). It answers what a supervisor opens
// it for: are there enough people today (the sentence on top, with the system's fix and where to approve it), when must
// someone be at a press (today's shifts over the de-mold / mold-in moments, then the next six in words), and which
// schedule is in force (one sentence + 回復上一版). HY: 「必要資訊就好；重複的、主管不需要知道的全部刪掉」.
// 硫化機排程 (the Gantt) moved to 產線監控 (Kyle 10-22); the 現況／優化 shift comparison and the capacity table are gone.
export function ScheduleTab() {
  const { state, transport } = useWarRoom()
  const [versions, setVersions] = useState<ScheduleVersion[]>([])
  const [shifts, setShifts] = useState<Shift[] | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const day = isNaN(now) ? NaN : factoryMidnight(now)

  const loadVersions = useCallback(() => {
    transport?.scheduleVersions().then(setVersions).catch((e) => setMsg(`排程版本讀取失敗：${e}`))
  }, [transport])
  // a proposed version arrives with its approval card, not as a schedule message: refetch when the waiting cards change too
  const pendingKey = state.pending.map((p) => p.id).join(',')
  useEffect(() => { loadVersions() }, [loadVersions, state.scheduleRev, pendingKey])
  // an approved staffing change (夜班加班／跨組調人) arrives as a new schedule version and changes the head counts
  useEffect(() => {
    transport?.shifts().then(setShifts).catch(() => setShifts((s) => s ?? []))
  }, [transport, day, state.scheduleRev])

  const active = versions.find((v) => v.status === 'active') ?? state.schedule ?? null
  const events = state.snapshot?.events ?? []
  // presses out of service (page 11): their in-mold tire is held for QC, so it has no de-mold and the next one no mold-in
  const outAt: Record<string, number> = {}
  for (const m of state.snapshot?.machines ?? []) {
    if (m.line !== 'CURE' || m.status !== 'out_of_service') continue
    const t = heldAt(state.snapshot?.safety_actions ?? [], events, m.machine_id)
    outAt[m.machine_id] = t ? ms(t) : now
  }
  const needs = needsOf(active?.schedule.jobs ?? [], outAt)
  const today = shifts && !isNaN(day) ? todayShifts(shifts, day) : []
  const proposed = versions.filter((v) => v.status === 'proposed').length

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
    <div className="schedule sp">
      {shifts != null && !isNaN(now) && <Answer today={today} needs={needs} now={now} day={day} versions={versions} active={active} pending={state.pending} />}
      <div className="sched-row">
        <Panel title="今天誰上班" testId="sched-people">
          <p className="sp-sub">每一條是一班，紅色＝人不夠。最下面一列是要有人在硫化機旁的時間。</p>
          {shifts == null || isNaN(day) ? <p className="muted">讀取班表中…</p>
            : today.length ? <PeopleChart shifts={today} needs={needs} day={day} now={now} />
              : <p className="muted">今天（{fmtMD(day)}）沒有排班。</p>}
        </Panel>
        <NextUp needs={needs} now={now} day={day} />
      </div>
      <section className="panel sp-line" aria-label="排程版本" data-testid="sched-version">
        <p>{active ? <>現在用<b>第 {active.version} 版</b>排程{state.pending.length > 0 && proposed > 0
          ? <>，另有 {proposed} 版<a href="#agent" className="sp-link" onClick={() => window.scrollTo(0, 0)}>等你到主控台核准 →</a></> : '。'}</>
          : '等待排程…'}</p>
        {active?.parent_version != null && <button className="btn-ghost" onClick={rollback} title="回復上一版排程；先檢查會不會排到停用的機台">↶ 回復上一版</button>}
        <a href="#monitor" className="sp-link quiet" onClick={() => window.scrollTo(0, 0)}>硫化機排程在產線監控 →</a>
        {msg && <p className="note sp-msg" role="status">{msg}</p>}
      </section>
      <section className="panel sp-line" aria-label="排程效益" data-testid="sched-benefit">
        <p><span className="sp-dim">AI 排出模時刻</span>中大胎 100 條交期 <b>{LEAD_TIME_WEEKS.ml[0].toFixed(1)} → {LEAD_TIME_WEEKS.ml[1].toFixed(1)} 週</b></p>
        <details className="sp-why"><summary>計算條件</summary><p>提案 p.6 試算：{CAPACITY_CONDITIONS}。數值由公式計算，不由 AI 推測。</p></details>
      </section>
    </div>
  )
}

/** The answer first: which shift is short and how the system would cover it — or that today is fine. */
function Answer({ today, needs, now, day, versions, active, pending }: {
  today: Shift[]; needs: Need[]; now: number; day: number; versions: ScheduleVersion[]; active: ScheduleVersion | null; pending: ApprovalRequest[]
}) {
  const short = today.filter((s) => gapOf(s) > 0 && ms(s.end) > now)
  const count = (a: number, b: number) => {
    const n = needs.filter((x) => x.t >= a && x.t < b)
    return { dm: n.filter((x) => x.kind === 'demold').length, mi: n.filter((x) => x.kind === 'mold_in').length }
  }
  if (!short.length) {
    const c = count(now, now + 24 * H)
    const date = isoTaipei(day).slice(0, 10)
    const done = (active?.schedule.staffing_changes ?? []).filter((x) => x.shift_date === date)
    return (
      <section className="panel sp-answer ok" aria-label="今天的結論" data-testid="sched-answer">
        <div>
          <span className="sp-k">{fmtMD(day)}（{fmtWeekday(day)}）</span>
          <h1>今天每一班人都夠</h1>
          <p>{c.dm + c.mi > 0 ? <>接下來 24 小時有 <b>{c.dm} 次出模、{c.mi} 次入模</b>，每次都要有人在硫化機旁。</> : '接下來 24 小時沒有出模、入模。'}
            {done.map((x) => `已核准：${x.shift_name}${x.label}。`).join('')}</p>
        </div>
      </section>
    )
  }
  const first = short[0]
  const total = short.reduce((n, s) => n + gapOf(s), 0)
  const c = count(ms(first.start), ms(first.end))
  const ask = staffingApproval(pending)
  const prop = proposalFor(versions, first)
  const fix = ask?.label ?? prop?.c.label ?? null
  return (
    <section className="panel sp-answer" aria-label="今天的結論" data-testid="sched-answer">
      <div>
        <span className="sp-k">今天要你處理 {short.length} 件</span>
        <h1>{short.length === 1 ? `${whenOf(first, now)}${first.name}缺 ${total} 人` : `今天 ${short.length} 班缺人，共缺 ${total} 人`}</h1>
        {short.length === 1
          ? <p>{TEAM_LABELS[first.team] ?? first.team} {fmtHM(first.start)}–{fmtHM(first.end)} 只有 <b>{first.headcount}／{first.required} 人</b>。
            {c.dm + c.mi > 0 ? <>這段時間有 <b>{c.dm} 次出模、{c.mi} 次入模</b>，每次都要有人在硫化機旁。</> : '這段時間沒有出模、入模。'}</p>
          : <p>{short.map((s) => `${TEAM_LABELS[s.team] ?? s.team}${s.name} ${fmtHM(s.start)}–${fmtHM(s.end)} 只有 ${s.headcount}／${s.required} 人`).join('；')}。</p>}
      </div>
      {fix && (
        <div className="sp-fix">
          <span className="sp-k">系統建議{prop ? `（第 ${prop.v.version} 版排程）` : ''}</span>
          <b>{fix}</b>
          {ask && <a className="sp-go" href="#agent" onClick={() => window.scrollTo(0, 0)} data-testid="sched-approve">到主控台核准 →</a>}
        </div>
      )}
    </section>
  )
}

/** The next six de-molds / mold-ins in words: what the time chart shows, readable on a phone. */
function NextUp({ needs, now, day }: { needs: Need[]; now: number; day: number }) {
  const next = isNaN(now) ? [] : needs.filter((n) => n.t >= now && n.t < now + 24 * H).slice(0, 6)
  return (
    <Panel title="接下來要有人在場" testId="sched-next">
      <p className="sp-sub">出模要有人把胎取出，入模要有人放下一條胎。</p>
      {next.length ? (
        <ul className="sp-next">
          {next.map((n, i) => (
            <li key={i}>
              <b className="t">{n.t >= day + 30 * H && <small>明天</small>}{fmtHM(n.t)}</b>
              <span className={`sp-kind ${n.kind === 'demold' ? 'dm' : 'mi'}`}>{n.kind === 'demold' ? '出模' : '入模'}</span>
              <span className="what">{Number(n.job.press_id.slice(5))} 號機・{n.job.order_id}（{SIZE_LABEL[n.job.size]}）</span>
              {isNight(n.t) && <em>夜班</em>}
            </li>
          ))}
        </ul>
      ) : <p className="muted">接下來 24 小時沒有出模、入模。</p>}
    </Panel>
  )
}
