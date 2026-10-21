// 修復預估 (#73): on the supervisor's approval card, how the anomaly gets fixed and by when.
// - 怎麼做: the SOP 處置步驟 the field crew follows (the same list the 3D factory ticks off, #72)
// - 多久修好: the event's verification window (Expectation.within_min, e.g. 30 分內溫度回到 ≥121°C)
// - 預計完成: the approval time (or now, before approval) + that window, or the latest field-task ETA if later
// Read-only: everything comes from the event, the world's task board and the SOP; nothing is invented here.
import { useEffect, useState } from 'react'
import type { Channel, Event, Expectation } from '../contracts'
import { repairEstimate, targetText } from '../aiStatus'
import { fmtHM, fmtMD } from '../format'
import { loadSopSteps, type SopSteps } from '../sop'
import { useWarRoom } from '../useWarRoom'

const CH_LABEL: Record<Channel, string> = { temp_c: '溫度', pressure_kgf_cm2: '壓力', water_flow_lpm: '水流', steam_kgf_cm2: '蒸汽壓' }

function expectationText(x: Expectation): string {
  return `${x.within_min} 分內${CH_LABEL[x.channel]}回到 ${targetText(x)}`
}

export function RepairEta({ ev }: { ev: Event | null }) {
  const { state } = useWarRoom()
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
  if (!x && !tasks.length && !sop?.steps.length) return null
  return (
    <div className="ph-eta" data-testid="phone-eta">
      <b>修復預估</b>
      <dl>
        {x && <><dt>多久修好</dt><dd data-testid="phone-eta-window">約 {x.within_min} 分鐘（{expectationText(x)}，系統自動驗證）</dd></>}
        {doneBy && <><dt>預計完成</dt><dd data-testid="phone-eta-done">{decided ? '' : '現在核准的話，約 '}<b>{fmtMD(doneBy)} {fmtHM(doneBy)}</b>{lastEta ? '（含現場任務）' : ''}</dd></>}
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
            <li key={t.id}>{t.title}・{t.worker_name ?? t.team_label}・{t.status}{t.status !== '完成' && t.eta ? `・預計 ${fmtHM(t.eta)}` : ''}{t.status === '完成' && t.done_at ? `・${fmtHM(t.done_at)} 完成` : ''}</li>
          ))}</ul></dd>
        </>}
      </dl>
    </div>
  )
}
