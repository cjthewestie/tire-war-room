// 修復預估 (#73): on the supervisor's approval card, how the anomaly gets fixed and by when.
// - 怎麼做: the SOP 處置步驟 the field crew follows (the same list the 3D factory ticks off, #72)
// - 多久修好: the event's verification window (Expectation.within_min, e.g. 30 分內溫度回到 ≥121°C)
// - 預計完成: the approval time (or now, before approval) + that window, or the latest field-task ETA if later
// 快速維修 (GET /api/health repair_fast_s > 0, Kyle 10-21): how long and by when are wall seconds instead —
// 「約 16 秒（維修 12 秒＋回溫 4 秒）」, 「約 9 秒後」, a repair task 「約 N 秒」 from its progress (fastEta.ts); old servers
// (no repair_fast_s, or 0) keep the window wording.
// Read-only: everything comes from the event, the world's task board and the SOP; nothing is invented here.
import { useEffect, useState } from 'react'
import type { Event, Expectation } from '../contracts'
import { CH_LABEL, repairEstimate, targetText } from '../aiStatus'
import { fmtHM, fmtMD } from '../format'
import { loadSopSteps, type SopSteps } from '../sop'
import { useHealth } from '../health'
import { useWarRoom } from '../useWarRoom'
import { fastApplies, fastDoneText, fastRepairOf, fastWindowText, isRepairTask, secText, taskLeftS } from './fastEta'

function expectationText(x: Expectation): string {
  return `${x.within_min} 分內${CH_LABEL[x.channel]}回到 ${targetText(x)}`
}

export function RepairEta({ ev }: { ev: Event | null }) {
  const { state } = useWarRoom()
  const fastCfg = fastRepairOf(useHealth())
  const [sop, setSop] = useState<SopSteps | null>(null)
  const machine = ev && ev.source !== 'plant' ? ev.machine_id : null
  useEffect(() => {
    if (!ev || !machine) return
    let alive = true
    loadSopSteps(machine, ev).then((s) => { if (alive) setSop(s) }, () => alive && setSop(null))
    return () => { alive = false }
    // the SOP depends on which event and round this is (round 2 of p.11 → SOP-CP-01), not on every refresh of it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ev?.id, ev?.replans, machine])
  if (!ev) return null
  // once approved, the window runs from the decision; before that, from now (「現在核准的話」)
  const { x, tasks, lastEta, decided, doneBy } = repairEstimate(ev, state)
  const fast = fastCfg && fastApplies(ev, tasks) ? fastCfg : null
  const fastDone = fast ? fastDoneText(tasks.filter(isRepairTask), fast, !!decided, !!ev.repaired_at) : null
  if (!x && !fast && !tasks.length && !sop?.steps.length) return null
  return (
    <div className="ph-eta" data-testid="phone-eta">
      <b>修復預估</b>
      <dl>
        {fast ? <><dt>多久修好</dt><dd data-testid="phone-eta-window">{fastWindowText(fast)}</dd></>
          : x && <><dt>多久修好</dt><dd data-testid="phone-eta-window">約 {x.within_min} 分鐘（{expectationText(x)}，系統自動驗證）</dd></>}
        {fastDone ? <><dt>預計完成</dt><dd data-testid="phone-eta-done">{fastDone.lead}<b>{fastDone.text}</b></dd></>
          : doneBy && <><dt>預計完成</dt><dd data-testid="phone-eta-done">{decided ? '' : '現在核准的話，約 '}<b>{fmtMD(doneBy)} {fmtHM(doneBy)}</b>{lastEta ? '（含現場任務）' : ''}</dd></>}
        {sop && sop.steps.length > 0 && <>
          <dt>怎麼做</dt>
          <dd data-testid="phone-eta-sop">
            依 {sop.sop_id} {sop.title}，共 {sop.steps.length} 步：
            <ol>{sop.steps.slice(0, 3).map((s, i) => <li key={i}>{s.split('\n')[0]}</li>)}</ol>
            {sop.steps.length > 3 && <span className="muted small">…其餘 {sop.steps.length - 3} 步由現場人員照 SOP 勾選回報</span>}
          </dd>
        </>}
        {tasks.length > 0 && <>
          <dt>現場任務</dt>
          <dd data-testid="phone-eta-tasks"><ul>{tasks.map((t) => (
            <li key={t.id}>{t.title}・{t.worker_name ?? t.team_label}・{t.status}{t.status !== '完成' ? (fast && isRepairTask(t) ? `・${secText(taskLeftS(t, fast))}` : t.eta ? `・預計 ${fmtHM(t.eta)}` : '') : ''}{t.status === '完成' && t.done_at ? `・${fmtHM(t.done_at)} 完成` : ''}</li>
          ))}</ul></dd>
        </>}
      </dl>
    </div>
  )
}
