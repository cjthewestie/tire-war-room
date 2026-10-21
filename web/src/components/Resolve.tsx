// Resolution paths (CJ, 2026-10-22: 「安全核心要設計可以被解決 安全停止也要有可以走向解決的按鈕」), warroom/api/resolve.py.
// SafetyCaseCard: a Safety Core stop / hold / alarm with its checklist ① 派現場檢查 → ② 讀值回到安全範圍 (code) → ③ 確認恢復運轉
// (or, for a cure-low hold, 「QC 合格，放行」 / 「報廢」). The release is the supervisor's (PIN like approvals); the AI has no
// button here or anywhere. StopFollowUp: a SAFE_STOP's plain reason and its three buttons 請 AI 重新規劃 / 交給現場處理 / 確認結案.
// Used on the 主控台 (red banner + the selected item's plan area) and on the phone.
import { useState } from 'react'
import type { Event, SafetyCase, SafetyDecision } from '../contracts'
import { fmtHM, fmtHMS } from '../format'
import { plain } from '../plain'
import { useWarRoom } from '../useWarRoom'
import { DECISION, caseTitle, openCases } from '../resolveText'
import './resolve.css'

function errText(e: unknown): string {
  const s = String(e).replace(/^Error: /, '')
  const m = /"detail"\s*:\s*"([^"]+)"/.exec(s)
  return m ? m[1] : s
}

/** One Safety Core case with its checklist. `compact`: the phone layout. */
export function SafetyCaseCard({ c, compact = false, disabled = false }: { c: SafetyCase; compact?: boolean; disabled?: boolean }) {
  const { state, transport } = useWarRoom()
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [reason, setReason] = useState('')
  const tasks = (state.world?.tasks.items ?? []).filter((t) => c.task_ids.includes(t.id))
  // a task gone from the board was trimmed, and the board trims only finished ones
  const done = c.task_ids.filter((id) => (tasks.find((t) => t.id === id)?.status ?? '完成') === '完成').length
  const tasksOk = c.task_ids.length > 0 && done === c.task_ids.length
  const tile = state.snapshot?.machines.find((m) => m.machine_id === c.machine_id)
  const now = c.reading ?? tile?.temp_c ?? null
  const fieldOk = tasksOk || (confirmed && reason.trim().length > 0)
  const resolved = c.status === 'resolved'
  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key); setErr(null)
    try { await fn() } catch (e) { setErr(errText(e)) } finally { setBusy(null) }
  }
  const release = (decision: SafetyDecision) => run(decision, () => transport!.safetyRelease!(c.id, { decision, reason: reason.trim(), field_confirmed: confirmed && !tasksOk }))
  const canRelease = (d: SafetyDecision) => !resolved && !disabled && !busy && !!transport?.safetyRelease && fieldOk && (d === 'scrap' || c.reading_ok)
  return (
    <div className={`rs-card ${compact ? 'rs-compact' : ''} ${resolved ? 'rs-done' : ''}`} role={resolved ? undefined : 'alert'} data-testid="safety-case" data-case={c.id}>
      <div className="rs-head">
        <b className="rs-title">{caseTitle(c)}</b>
        {resolved && <span className="rs-pill ok" data-testid="safety-case-resolved">已解除・{DECISION[c.resolution ?? 'resume']}</span>}
      </div>
      <p className="rs-meta">
        <span>規則 <code>{c.rule_ids.join('、')}</code></span>
        <span>讀值 <b>{c.value.toFixed(1)}°C</b>（限值 {c.threshold.toFixed(0)}°C）</span>
        <span>{fmtHMS(c.ts)}</span>
        <span>反應 {c.latency_ms < 1 ? c.latency_ms.toFixed(2) : c.latency_ms.toFixed(0)} ms</span>
      </p>
      <ol className="rs-steps">
        <li className={c.task_ids.length ? (tasksOk ? 'ok' : 'run') : ''} data-testid="safety-step-1">
          <span className="rs-n">①</span>
          <div className="rs-body">
            <b>派現場檢查</b>
            {c.task_ids.length ? (
              <div className="rs-sub" data-testid="safety-step-1-status">{c.sop_ids.join('・')}・{done}/{c.task_ids.length} 完成
                {!compact && <ul className="rs-tasks">{tasks.map((t) => <li key={t.id}>{t.title}・{t.worker_name ?? t.team_label}・{t.status}</li>)}</ul>}
              </div>
            ) : <span className="rs-sub">依 {c.sop_ids.join('・')} 派維修與品管到現場</span>}
          </div>
          {!c.task_ids.length && !resolved && (
            <button className="rs-btn" disabled={disabled || !!busy || !transport?.safetyDispatch} data-testid="safety-dispatch"
              onClick={() => void run('dispatch', () => transport!.safetyDispatch!(c.id))}>{busy === 'dispatch' ? '派工中…' : '派現場檢查'}</button>
          )}
        </li>
        <li className={c.reading_ok ? 'ok' : 'run'} data-testid="safety-step-2">
          <span className="rs-n">②</span>
          <div className="rs-body">
            <b>{c.reading_ok ? '讀值已回到安全範圍' : '等讀值回到安全範圍'}</b>
            <span className="rs-sub" data-testid="safety-step-2-status">
              {now != null ? `現在 ${now.toFixed(1)}°C・` : ''}連續 {c.safe_samples}/{c.safe_needed} 筆在安全範圍（程式判定，與安全核心重新上鎖同一標準）
            </span>
          </div>
        </li>
        <li className={resolved ? 'ok' : ''} data-testid="safety-step-3">
          <span className="rs-n">③</span>
          <div className="rs-body">
            <b>{c.decisions.includes('qc_pass') ? 'QC 判定：放行或報廢' : '確認恢復運轉'}</b>
            {resolved ? (
              <span className="rs-sub">{c.resolved_by}・{fmtHM(c.resolved_at)}{c.reason ? `・${c.reason}` : ''}</span>
            ) : (
              <>
                {!tasksOk && (
                  <label className="rs-confirm">
                    <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} data-testid="safety-confirmed" />
                    已現場確認
                    {confirmed && <input className="rs-reason" value={reason} maxLength={200} placeholder="理由（必填）" onChange={(e) => setReason(e.target.value)} data-testid="safety-reason" />}
                  </label>
                )}
                <span className="rs-sub">{!fieldOk ? '需先完成 ①（或勾選已現場確認並寫理由）' : !c.reading_ok ? '需等 ② 讀值穩定' : '條件都已滿足；按下後寫入稽核紀錄'}</span>
              </>
            )}
          </div>
          {!resolved && (
            <div className="rs-acts">
              {c.decisions.map((d) => (
                <button key={d} className={`rs-btn ${d === 'scrap' ? 'danger' : 'go'}`} disabled={!canRelease(d)} data-testid={`safety-release-${d}`}
                  onClick={() => void release(d)}>{busy === d ? '送出中…' : DECISION[d]}</button>
              ))}
            </div>
          )}
        </li>
      </ol>
      {err && <p className="rs-err" role="alert">{err}</p>}
      {!resolved && <p className="rs-foot">只有主管能解除；AI 不能呼叫或覆寫安全核心。</p>}
    </div>
  )
}

/** The 主控台's red banner: every open Safety Core case, each with its checklist. */
export function SafetyBanner() {
  const { state, connected } = useWarRoom()
  const cases = openCases(state.snapshot?.safety_cases)
  if (!cases.length) return null
  return (
    <section className="rs-banner" aria-label="安全核心處置" data-testid="safety-banner">
      {cases.map((c) => <SafetyCaseCard key={c.id} c={c} disabled={!connected} />)}
    </section>
  )
}

const FOLLOW_LABEL = { replanned: '已請 AI 重新規劃', manual: '人工處理中', closed: '已確認結案' } as const

/** A SAFE_STOP's way out: why it stopped (plain words), then 請 AI 重新規劃 / 交給現場處理 / 確認結案. */
export function StopFollowUp({ ev, compact = false, disabled = false, onReplanned }: {
  ev: Event; compact?: boolean; disabled?: boolean; onReplanned?: (newId: string) => void
}) {
  const { state, transport } = useWarRoom()
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const fu = ev.follow_up ?? null
  const tasks = (state.world?.tasks.items ?? []).filter((t) => fu?.task_ids.includes(t.id))
  const done = (fu?.task_ids ?? []).filter((id) => (tasks.find((t) => t.id === id)?.status ?? '完成') === '完成').length
  const tasksLeft = fu?.status === 'manual' && done < fu.task_ids.length
  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key); setErr(null)
    try { await fn() } catch (e) { setErr(errText(e)) } finally { setBusy(null) }
  }
  const off = disabled || !!busy
  const why = ev.stop_reason_text ?? ev.stop_reason ?? plain(ev.outcome ?? '')
  return (
    <div className={`rs-stop ${compact ? 'rs-compact' : ''}`} data-testid="stop-follow-up" data-event={ev.id}>
      <p className="rs-why"><b>為什麼停：</b><span data-testid="stop-reason">{why || '（沒有記錄原因）'}</span></p>
      {fu && (
        <p className="rs-sub" data-testid="stop-status">
          <span className={`rs-pill ${fu.status === 'closed' ? 'ok' : 'run'}`}>{FOLLOW_LABEL[fu.status]}</span>
          {fu.status === 'manual' && ` ${fu.sop_ids.join('・')}・任務 ${done}/${fu.task_ids.length} 完成`}
          {fu.status === 'replanned' && fu.new_event_id && ` → ${fu.new_event_id}`}
          {fu.status === 'closed' && ` ${fu.closed_by ?? ''}・${fmtHM(fu.closed_at)}`}
        </p>
      )}
      {fu?.status === 'manual' && !compact && tasks.length > 0 && (
        <ul className="rs-tasks">{tasks.map((t) => <li key={t.id}>{t.title}・{t.worker_name ?? t.team_label}・{t.status}</li>)}</ul>
      )}
      {(!fu || fu.status === 'manual') && (
        <div className="rs-acts">
          <button className="rs-btn go" disabled={off || !transport?.stopReplan} data-testid="stop-replan"
            onClick={() => void run('replan', async () => { const n = await transport!.stopReplan!(ev.id); onReplanned?.(n.id) })}>
            {busy === 'replan' ? '送出中…' : '請 AI 重新規劃'}</button>
          {!fu && (
            <button className="rs-btn" disabled={off || !transport?.stopHandoff} data-testid="stop-handoff"
              onClick={() => void run('handoff', () => transport!.stopHandoff!(ev.id))}>{busy === 'handoff' ? '派工中…' : '交給現場處理'}</button>
          )}
          <button className="rs-btn" disabled={off || tasksLeft || !transport?.stopClose} data-testid="stop-close"
            title={tasksLeft ? '現場任務完成後才能結案' : undefined}
            onClick={() => void run('close', () => transport!.stopClose!(ev.id))}>{busy === 'close' ? '送出中…' : '確認結案'}</button>
        </div>
      )}
      {err && <p className="rs-err" role="alert">{err}</p>}
      {(!fu || fu.status === 'manual') && <p className="rs-foot">重新規劃會開新事件，照常經過動作把關與核准；結案寫入稽核與每日事件紀錄。</p>}
    </div>
  )
}
