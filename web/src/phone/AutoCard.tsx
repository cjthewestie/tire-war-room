// 自動播放 Demo 的手機卡：同一張短卡片，依狀態原地更新；沒有任何按鈕（核准由 Demo 自動完成）。Hotfix 2026-10-22.
import { useWarRoom } from '../useWarRoom'

const MACHINE = 'CURE-07'
const num = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1))

export function AutoCard() {
  const { state } = useWarRoom()
  const events = (state.snapshot?.events ?? []).filter((e) => e.machine_id === MACHINE)
  const ev = events.slice().sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))[0]
  const card = ev ? state.pending.find((p) => p.event_id === ev.id) : undefined
  const approved = ev ? state.resolved.find((r) => r.event_id === ev.id && r.status === 'approved') : undefined
  const done = !!ev && ev.state === 'closed' && !!approved

  return (
    <div className="phone-wrap">
      <div className="phone">
        <div className="ph-card" data-testid="auto-card">
          {!ev ? (
            <p className="muted">監看中，目前沒有異常</p>
          ) : done ? (
            <>
              <h2 className="ph-title"><b>{MACHINE}｜處理完成</b></h2>
              <p><b>核准狀態</b>：Demo 自動核准</p>
              {ev.outcome && <p><b>處理結果</b>：{ev.outcome}</p>}
            </>
          ) : (
            <>
              <h2 className="ph-title"><b>{MACHINE} 溫度異常｜排程調整</b></h2>
              <p><b>原因</b>：7 號硫化機溫度由 {num(ev.anomaly.baseline)}°C 降至 {num(ev.anomaly.value)}°C。</p>
              <p><b>建議處置</b>：{card ? card.title : 'Agent 規劃中…'}</p>
              {card?.delivery_impact && <p><b>預期影響</b>：{card.delivery_impact}</p>}
              {card && <p className="muted small">Demo 自動核准中…</p>}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
