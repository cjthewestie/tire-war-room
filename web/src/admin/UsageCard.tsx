// 後台 模型用量 (治理層：預算上限; CJ, 2026-10-22: 「補治理」): today's model tokens and seconds against the caps, by kind,
// the top events and conversations, and which degradation step is on. Server: GET /api/admin/usage
// (warroom/agent/budget.py). Standalone so it drops into the 系統健康 tab (HealthPage.tsx, PR #111) as one card.
import { useEffect, useState } from 'react'
import { adminFetch } from '../transport'

interface KindUse { calls: number; tokens_in: number; tokens_out: number; seconds: number; estimated: number }
export interface Usage {
  day: string; level: 'normal' | 'casual_code' | 'preplan_paused' | 'chat_rules'; share: number
  totals: { tokens: number; seconds: number; calls: number }
  by_kind: Record<string, KindUse>
  top_events: { event_id: string; tokens: number; pct: number | null }[]
  top_conversations: { conversation: string; tokens: number; pct: number | null }[]
  caps: { daily_tokens: number; daily_model_s: number; per_event_tokens: number; per_conversation_tokens: number }
  used_pct: { daily_tokens: number | null; daily_model_s: number | null }
  degradation: { casual_code: boolean; preplan_paused: boolean; chat_rules: boolean; events_cut: string[]; conversations_cut: string[] }
}

const KIND: Record<string, string> = {
  incident: '事件規劃', task: '主管任務規劃', preplan: '預先規劃', chat: '對話', casual: '閒聊', summary: '對話摘要',
  discuss: '計劃討論', qa: '問戰情室', dispatch: '派發',
}
const LEVEL: Record<Usage['level'], string> = {
  normal: '正常：全部用模型', casual_code: '已達 80%：閒聊與摘要改用程式回覆', preplan_paused: '已達 90%：預先規劃暫停',
  chat_rules: '已達上限：對話改用規則模式（事件規劃照常）',
}
const n = (x: number) => x.toLocaleString('zh-TW')

export function UsageCard() {
  const [u, setU] = useState<Usage | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let off = false
    adminFetch<Usage>('/api/admin/usage').then((d) => { if (!off) { setU(d); setErr(null) } })
      .catch((e) => { if (!off) setErr(String(e).replace(/^Error: /, '')) })
    const t = setTimeout(() => setTick((x) => x + 1), 10_000)
    return () => { off = true; clearTimeout(t) }
  }, [tick])
  if (err) return <p className="note admin-err" role="alert">模型用量讀取失敗：{err}</p>
  if (!u) return null
  const tone = u.level === 'normal' ? 'ok' : u.level === 'chat_rules' ? 'bad' : 'warn'
  const pct = Math.min(100, Math.round(u.share * 100))
  return (
    <article className={`usage-card t-${tone}`} data-testid="usage-card" aria-label="模型用量">
      <header>
        <i className={`dot ${tone === 'ok' ? 'ok' : tone === 'bad' ? 'bad' : 'data'}`} aria-hidden />
        <h3>模型用量</h3>
        <span className="muted small">{u.day}・每天（實際日期）歸零</span>
      </header>
      <b className="usage-level" data-testid="usage-level">{LEVEL[u.level]}</b>
      <div className="usage-bar" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label="今日用量">
        <i style={{ width: `${pct}%` }} /><em style={{ left: '80%' }} /><em style={{ left: '90%' }} />
      </div>
      <p className="usage-sum">
        <span>token {n(u.totals.tokens)} / {n(u.caps.daily_tokens)}（{u.used_pct.daily_tokens ?? 0}%）</span>
        <span>模型時間 {n(Math.round(u.totals.seconds))} / {n(u.caps.daily_model_s)} 秒（{u.used_pct.daily_model_s ?? 0}%）</span>
        <span>{n(u.totals.calls)} 次呼叫</span>
      </p>
      <table className="admin-table usage-kinds" data-testid="usage-kinds">
        <thead><tr><th>用途</th><th>次數</th><th>輸入</th><th>輸出</th><th>秒</th></tr></thead>
        <tbody>
          {Object.entries(u.by_kind).map(([k, v]) => (
            <tr key={k}><td>{KIND[k] ?? k}</td><td>{v.calls}</td><td>{n(v.tokens_in)}</td><td>{n(v.tokens_out)}</td><td>{v.seconds}</td></tr>
          ))}
          {!Object.keys(u.by_kind).length && <tr><td colSpan={5} className="muted">今天還沒有呼叫模型</td></tr>}
        </tbody>
      </table>
      <div className="usage-top">
        <div><h4>用量最多的事件（單一事件上限 {n(u.caps.per_event_tokens)}）</h4>
          <ul>{u.top_events.map((e) => <li key={e.event_id}><code>{e.event_id}</code> {n(e.tokens)}（{e.pct}%）{u.degradation.events_cut.includes(e.event_id) && <b className="bad"> 已停止重新規劃呼叫模型</b>}</li>)}
            {!u.top_events.length && <li className="muted">無</li>}</ul></div>
        <div><h4>用量最多的對話（單段上限 {n(u.caps.per_conversation_tokens)}）</h4>
          <ul>{u.top_conversations.map((c) => <li key={c.conversation}><code>{c.conversation}</code> {n(c.tokens)}（{c.pct}%）</li>)}
            {!u.top_conversations.length && <li className="muted">無</li>}</ul></div>
      </div>
      <p className="muted small">降級順序：80% 閒聊與摘要改程式回覆 → 90% 暫停預先規劃 → 100% 對話改規則模式。事件規劃永遠呼叫模型（安全優先），超過只記警告。</p>
    </article>
  )
}
