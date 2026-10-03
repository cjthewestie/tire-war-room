import { useEffect, useState } from 'react'
import { LINE_LABELS, type Event, type MachineDetail } from '../contracts'
import { COMPOUND_STOP_C, CURATIVE_STOP_C, SIZE_LABEL, machineLabel } from '../factory'
import { fmtHM, fmtHMS, fmtMD, fmtNum, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { STATUS_LABEL, Sparkline } from './common'

const STAGE_LABEL = { none: '無膠料', unvulcanized: '未加硫', curative_added: '已加硫' } as const
const MODEL_LABEL: Record<string, string> = { constant: '定值模型', 'linear-trend': '線性趨勢', 'exp-approach': '指數趨近', 'int8-ts': 'INT8 時序模型', 'mock-linear': '線性趨勢（模擬）' }
const H = 3_600_000

/** total_cure_h from the newest successful cure_formula step on an event for this press. */
function latestCureFormula(events: Event[], pressId: string): number | null {
  const evs = [...events].filter((e) => e.machine_id === pressId).sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))
  for (const e of evs) {
    for (const sr of [...e.steps].reverse()) {
      const v = sr.step.tool === 'cure_formula' && sr.result?.ok ? sr.result.data.total_cure_h : null
      if (typeof v === 'number') return v
    }
  }
  return null
}

export function MachineDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const { transport, state } = useWarRoom()
  const [d, setD] = useState<MachineDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const tick = state.clock?.factory_time
  useEffect(() => {
    let alive = true
    const load = () => transport?.machineDetail(id).then((x) => { if (alive) { setD(x); setErr(null) } }).catch((e) => alive && setErr(String(e)))
    load()
    const t = setInterval(load, 2000)
    return () => { alive = false; clearInterval(t) }
  }, [id, transport])
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  const st = d?.state
  const isPress = id.startsWith('CURE')
  const h = d?.history ?? []
  const temps = h.map((r) => (r.quality === 'ok' ? r.temp_c : null))
  const job = st?.cure_job ?? null
  // cure_formula result for this press (page 11): suggested total cure; shown next to the recipe value, never replacing it
  const formula = isPress ? latestCureFormula(state.snapshot?.events ?? [], id) : null
  const suggestedH = job && formula && Math.abs(formula - job.cure_required_h) > 0.05 ? formula : null
  const thresholds = isPress
    ? [{ v: 120, label: '下限 120°C（低於即保溫保壓）' }]
    : st?.compound_stage === 'unvulcanized' ? [{ v: COMPOUND_STOP_C, label: '未加硫 120°C 停' }]
      : st?.compound_stage === 'curative_added' ? [{ v: CURATIVE_STOP_C, label: '已加硫 110°C 停' }] : []
  return (
    <div className="drawer-wrap" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-label={`${machineLabel(id)} 詳細`} onClick={(e) => e.stopPropagation()}>
        <header className="drawer-h">
          <div>
            <p className="muted small">{st ? LINE_LABELS[st.line] : ''}・{id}</p>
            <h3>{machineLabel(id)}</h3>
          </div>
          <button className="x" onClick={onClose} aria-label="關閉">✕</button>
        </header>
        {err && <p className="err">讀取失敗：{err}</p>}
        {!d ? <p className="muted">讀取中…</p> : (
          <>
            <div className="drawer-meta">
              <span className={`state-pill ms-${d.state.status}`}>{STATUS_LABEL[d.state.status]}</span>
              {isPress
                ? <span className="muted small">在模：{job ? `${job.order_id} ${SIZE_LABEL[job.size]}` : '無'}</span>
                : <span className="muted small">膠料：{STAGE_LABEL[d.state.compound_stage]}</span>}
              {d.state.batch_id && <span className="muted small">批號 {d.state.batch_id}</span>}
              <span className="muted small">更新 {fmtHMS(d.state.last_reading?.ts ?? tick)}</span>
            </div>
            {d.state.note && <p className="note">{d.state.note}</p>}
            <div className="tiles">
              {d.tiles.map((t) => <div key={t.label} className={`tile ${t.status}`}><span className="tile-l">{t.label}</span><span className="tile-v sm">{t.label === '時間' && job ? `${job.cure_elapsed_h.toFixed(1)} h` : t.value}</span></div>)}
            </div>
            {job && (
              <div className="cure-job">
                <b>{job.order_id}</b>・{SIZE_LABEL[job.size]}・模具 {job.mold_id}・已硫化 {job.cure_elapsed_h.toFixed(1)} / {fmtNum(suggestedH ?? job.cure_required_h)} h
                <div className="bar"><i style={{ width: `${Math.min(100, (100 * job.cure_elapsed_h) / (suggestedH ?? job.cure_required_h))}%` }} /></div>
                <span className="muted small">入模 {fmtMD(job.mold_in_at)} {fmtHM(job.mold_in_at)}・預計出模 {suggestedH
                  ? <>{fmtMD(ms(job.mold_in_at) + suggestedH * H)} {fmtHM(ms(job.mold_in_at) + suggestedH * H)}（硫化公式 {fmtNum(suggestedH)} h；原配方 {fmtNum(job.cure_required_h)} h 於 {fmtMD(job.planned_demold_at)} {fmtHM(job.planned_demold_at)}）</>
                  : <>{fmtMD(job.planned_demold_at)} {fmtHM(job.planned_demold_at)}</>}・設定 {job.setpoint_c}°C</span>
              </div>
            )}
            <Chart label="溫度 °C（30 分）" values={temps} thresholds={thresholds} band={isPress ? [120, 130] : undefined}
              color={d.tiles[0]?.status === 'alarm' ? 'var(--alarm)' : 'var(--accent)'} />
            {d.prediction && (
              <p className="muted small">預測 {Math.round(d.prediction.horizon_s / 60)} 分後：{d.prediction.predicted_value}°C（{d.prediction.band_lo}–{d.prediction.band_hi}）・趨勢 {d.prediction.trend_per_min > 0 ? '+' : ''}{d.prediction.trend_per_min}°C/分・<span title={d.prediction.model}>{MODEL_LABEL[d.prediction.model] ?? '時序模型'}</span></p>
            )}
            {h.some((r) => r.pressure_kgf_cm2 != null) && <Chart label="壓力 kgf/cm²" values={h.map((r) => r.pressure_kgf_cm2)} />}
            {h.some((r) => r.water_flow_lpm != null) && <Chart label="水流 L/min" values={h.map((r) => r.water_flow_lpm)} color="var(--line-b)" />}
            {h.some((r) => r.steam_kgf_cm2 != null) && <Chart label="蒸汽 kgf/cm²" values={h.map((r) => r.steam_kgf_cm2)} color="var(--warn)" />}
            {h.some((r) => r.quality !== 'ok') && <p className="note">資料品質：{h.filter((r) => r.quality !== 'ok').length} 筆被驗證層攔下（不喚醒 AI）</p>}
          </>
        )}
      </aside>
    </div>
  )
}

function Chart({ label, values, thresholds, band, color }: { label: string; values: (number | null)[]; thresholds?: { v: number; label: string }[]; band?: [number, number]; color?: string }) {
  const last = [...values].reverse().find((v) => v != null)
  return (
    <div className="chart">
      <div className="chart-h"><span>{label}</span><b>{last ?? '—'}</b></div>
      <Sparkline values={values} threshold={thresholds} band={band} color={color} width={320} height={64} />
    </div>
  )
}
