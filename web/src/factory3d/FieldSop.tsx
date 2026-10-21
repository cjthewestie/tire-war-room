// 現場處置 (#72): in the 3D factory, F at a machine with an anomaly shows its SOP 處置步驟 as a checklist. When every
// step is ticked, 回報處置完成 removes the injected fault on that machine (POST /api/sim/clear, the demo route the
// dashboard's reset already uses). Nothing here moves a machine: readings recover on their own, and the state machine
// verifies and closes the event as usual. Ticks are kept per event while the page is open.
// A machine taken out of service (p.11 round 2: 7 號 停用) has a closed event and nothing to tick: the panel shows the
// 停用檢修 task instead of a temperature-drop checklist.
import { useEffect, useState } from 'react'
import { AiStatusLine } from '../AiStatusLine'
import { CH_LABEL, expectationOf } from '../aiStatus'
import { DEMO_MODE } from '../components/DemoStrip'
import { STATE_LABEL } from '../components/common'
import type { Event, MetricTile, WorkTask } from '../contracts'
import { fmtHM } from '../format'
import { loadSopSteps, openEventFor, type SopSteps } from '../sop'
import { useWarRoom } from '../useWarRoom'

const ticks = new Map<string, Set<number>>()        // event (or machine) id -> ticked step indexes
const reported = new Map<string, string>()          // same key -> factory time the fix was reported

export function FieldSop({ machineId, tiles }: { machineId: string; tiles: MetricTile[] }) {
  const { state } = useWarRoom()
  const ev = openEventFor(state.snapshot?.events, machineId)
  const out = !ev && state.snapshot?.machines.find((m) => m.machine_id === machineId)?.status === 'out_of_service'
  if (out) return <OutOfService machineId={machineId} />
  return <FieldSopSteps machineId={machineId} tiles={tiles} ev={ev} />
}

/** 停用中: the machine's last event and its 停用檢修 task (warroom/sim/work.py), nothing to tick or report here. */
function OutOfService({ machineId }: { machineId: string }) {
  const { state } = useWarRoom()
  const ev: Event | null = [...(state.snapshot?.events ?? [])].filter((e) => e.machine_id === machineId)
    .sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))[0] ?? null
  const task: WorkTask | undefined = (state.world?.tasks.items ?? [])
    .filter((t) => t.machine_id === machineId && t.kind === '停用檢修')
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0]
  return (
    <section className="field-sop" data-testid="field-sop">
      <h4>現場處置<small>已停用・交維修</small></h4>
      {ev && <AiStatusLine ev={ev} className="small" />}
      <p className="small" data-testid="field-sop-out">
        {task
          ? <>停用檢修：{task.title}・{task.worker_name ?? task.team_label}・{task.status}{task.status === '完成' ? (task.done_at ? `・${fmtHM(task.done_at)} 完成` : '') : task.eta ? `・預計 ${fmtHM(task.eta)}` : ''}</>
          : '已停用，等維修排除。'}
      </p>
      <p className="muted small">{machineId.startsWith('CURE-') ? '停用的硫化機要維修簽回並試溫穩定後才恢復排程（SOP-CP-01）；' : ''}這裡不用勾選回報。</p>
    </section>
  )
}

function FieldSopSteps({ machineId, tiles, ev }: { machineId: string; tiles: MetricTile[]; ev: Event | null }) {
  const { state, transport } = useWarRoom()
  const abnormal = tiles.some((t) => t.status !== 'normal')
  const key = ev?.id ?? machineId
  const [sop, setSop] = useState<SopSteps | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [, redraw] = useState(0)
  const [busy, setBusy] = useState(false)
  const show = !!ev || abnormal
  const sopKey = `${machineId}|${ev?.id ?? ''}|${ev?.replans ?? 0}`
  useEffect(() => {
    if (!show) return
    let alive = true
    loadSopSteps(machineId, ev, tiles).then((s) => { if (alive) { setSop(s); setErr(null) } }, (e) => alive && setErr(String(e)))
    return () => { alive = false }
    // the SOP only depends on which machine / event this is, not on every reading refresh
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sopKey, show])
  if (!show) return null
  const done = ticks.get(key) ?? new Set<number>()
  const steps = sop?.steps ?? []
  const all = steps.length > 0 && steps.every((_, i) => done.has(i))
  const sent = reported.get(key)
  const toggle = (i: number) => {
    const s = new Set(done)
    if (s.has(i)) s.delete(i); else s.add(i)
    ticks.set(key, s)
    redraw((n) => n + 1)
  }
  const finish = async () => {
    if (!transport?.clearFault) return
    setBusy(true)
    try {
      await transport.clearFault(machineId)
      reported.set(key, state.clock?.factory_time ?? new Date().toISOString())
      setErr(null)
    } catch (e) { setErr(`回報沒有成功：${String(e)}`) } finally { setBusy(false); redraw((n) => n + 1) }
  }
  return (
    <section className="field-sop" data-testid="field-sop">
      <h4>現場處置 SOP{sop ? <small>{sop.sop_id}・{sop.title}</small> : null}</h4>
      {ev && <AiStatusLine ev={ev} className="small" />}
      {!sop && !err && <p className="muted small">查 SOP 中…</p>}
      {sop && !steps.length && <p className="muted small">這份 SOP 沒有找到「處置步驟」段落，請依 {sop.sop_id} 現場處置。</p>}
      {steps.length > 0 && (
        <ol className="field-sop-steps">
          {steps.map((s, i) => (
            <li key={i} className={done.has(i) ? 'on' : ''}>
              <label><input type="checkbox" checked={done.has(i)} onChange={() => toggle(i)} disabled={!!sent} data-testid={`field-sop-step-${i}`} />
                <span>{s}</span></label>
            </li>
          ))}
        </ol>
      )}
      {sent
        ? <p className="field-sop-ok" data-testid="field-sop-done">{DEMO_MODE
          ? `已回報。${CH_LABEL[expectationOf(ev)?.channel ?? 'temp_c']}回到正常就會自動結束這件異常。`
          : `✓ 已回報現場處置完成。讀數回穩後，系統會驗證並結案${ev ? `（目前：${STATE_LABEL[ev.state]}）` : ''}。`}</p>
        : steps.length > 0 && (
          <button className="field-sop-go" disabled={!all || busy || !transport?.clearFault} onClick={() => void finish()} data-testid="field-sop-finish">
            {busy ? '回報中…' : all ? '回報處置完成' : DEMO_MODE ? `還有 ${steps.length - done.size} 步` : `勾完 ${steps.length} 步才能回報（已完成 ${done.size}）`}
          </button>
        )}
      {!transport?.clearFault && <p className="muted small">{DEMO_MODE ? '示範資料不能回報。' : '模擬資料模式不能回報；接上伺服器才能排除異常。'}</p>}
      {err && <p className="err">{err}</p>}
    </section>
  )
}
