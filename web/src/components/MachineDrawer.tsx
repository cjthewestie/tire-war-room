import { useEffect, useState } from 'react'
import { LINE_LABELS, type MachineDetail } from '../contracts'
import { COMPOUND_STOP_C, CURATIVE_STOP_C, machineLabel } from '../factory'
import { fmtHM, fmtHMS } from '../format'
import { useWarRoom } from '../useWarRoom'
import { STATUS_LABEL, Sparkline } from './common'

const STAGE_LABEL = { none: '無膠料', unvulcanized: '未加硫', curative_added: '已加硫' } as const

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
  const thresholds = isPress
    ? [{ v: 120, label: '120°C 保溫保壓' }]
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
              <span className="muted small">膠料：{STAGE_LABEL[d.state.compound_stage]}</span>
              {d.state.batch_id && <span className="muted small">批號 {d.state.batch_id}</span>}
              <span className="muted small">更新 {fmtHMS(d.state.last_reading?.ts ?? tick)}</span>
            </div>
            {d.state.note && <p className="note">{d.state.note}</p>}
            <div className="tiles">
              {d.tiles.map((t) => <div key={t.label} className={`tile ${t.status}`}><span className="tile-l">{t.label}</span><span className="tile-v sm">{t.value}</span></div>)}
            </div>
            {d.state.cure_job && (
              <div className="cure-job">
                <b>{d.state.cure_job.order_id}</b>・{d.state.cure_job.size === 'S' ? '小胎' : d.state.cure_job.size === 'M' ? '中胎' : '大胎'}・模具 {d.state.cure_job.mold_id}
                <div className="bar"><i style={{ width: `${Math.min(100, (100 * d.state.cure_job.cure_elapsed_h) / d.state.cure_job.cure_required_h)}%` }} /></div>
                <span className="muted small">入模 {fmtHM(d.state.cure_job.mold_in_at)}・預計出模 {fmtHM(d.state.cure_job.planned_demold_at)}・設定 {d.state.cure_job.setpoint_c}°C</span>
              </div>
            )}
            <Chart label="溫度 °C（30 分）" values={temps} thresholds={thresholds} band={isPress ? [120, 130] : undefined}
              color={d.tiles[0]?.status === 'alarm' ? 'var(--alarm)' : 'var(--accent)'} />
            {d.prediction && (
              <p className="muted small">預測 {Math.round(d.prediction.horizon_s / 60)} 分後：{d.prediction.predicted_value}°C（{d.prediction.band_lo}–{d.prediction.band_hi}）・趨勢 {d.prediction.trend_per_min > 0 ? '+' : ''}{d.prediction.trend_per_min}°C/分・{d.prediction.model}</p>
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
