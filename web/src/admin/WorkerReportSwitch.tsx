// 後台: 人員任務回報模式 (CJ 2026-10-22: 「加回報」). 自動（展示） = the simulator finishes worker tasks on its timers
// (the demos rely on it); 人員回報 = each assigned worker taps 到達・開始 / 完成 on /worker, code then checks the reading.
// GET/POST /api/tasks/mode (warroom/api/tasks.py); every switch is audited.
import { useEffect, useState } from 'react'
import { adminFetch } from '../transport'

interface ModeView { mode: 'auto' | 'report'; remote_pins: boolean }

export function WorkerReportSwitch() {
  const [m, setM] = useState<ModeView | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { adminFetch<ModeView>('/api/tasks/mode').then(setM).catch((e) => setErr(String(e))) }, [])
  const set = async (on: boolean) => {
    setErr(null)
    try { setM(await adminFetch<ModeView>('/api/tasks/mode', { method: 'POST', body: { on } })) } catch (e) { setErr(String(e).replace(/^Error: /, '')) }
  }
  return (
    <div className="admin-card t-muted" data-testid="worker-report-mode">
      <span className="admin-card-k">人員任務回報</span>
      <div className="seg" role="group" aria-label="人員任務回報模式">
        <button className={m?.mode === 'auto' ? 'on' : ''} aria-pressed={m?.mode === 'auto'} onClick={() => set(false)}>自動（展示）</button>
        <button className={m?.mode === 'report' ? 'on' : ''} aria-pressed={m?.mode === 'report'} onClick={() => set(true)}>人員回報</button>
      </div>
      <span>{m?.mode === 'report'
        ? `現場人員在 /worker 按「到達・開始」「完成」，程式確認讀值後才結案${m.remote_pins ? '' : '・手機回報需設定 WARROOM_WORKER_PINS（現場平板免 PIN）'}`
        : '模擬依時間自動完成任務（展示用）'}</span>
      {err && <span className="admin-err" role="alert">{err}</span>}
    </div>
  )
}
