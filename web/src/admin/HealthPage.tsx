// 後台 系統健康 (was 狀態): one card per subsystem — a light, what it means in plain words, the key numbers and what
// to do when it is red. The words come from the server (warroom/api/admin_params.py health()); this page only lays
// them out. CJ, 2026-10-22: 「狀態欄有點意義不明可以怎麼修改」.
import { useState } from 'react'
import { adminFetch } from '../transport'
import { Err } from './adminCommon'
import { MetricsPanel } from './MetricsPanel'
import { useLoad, verifyText, type Verify } from './adminData'

type Light = 'ok' | 'warn' | 'bad' | 'off'
interface HealthCard { id: string; title: string; light: Light; headline: string; meaning: string; facts: string[]; fix: string; verify?: boolean; degraded?: { name: string; why: string }[] }
const LIGHT: Record<Light, string> = { ok: '正常', warn: '注意', bad: '異常', off: '未使用' }

export function HealthPage() {
  const { data, err, reload } = useLoad<{ health: HealthCard[]; factory_time: string }>('/api/admin/status')
  const [verify, setVerify] = useState<Verify | null>(null)
  const [busy, setBusy] = useState(false)
  const runVerify = async () => {
    setBusy(true)
    try { setVerify(await adminFetch<Verify>('/api/admin/audit/verify')) } catch (e) { setVerify({ ok: false, checked: null, last_seq: null, broken_at: null, problem: String(e) }) } finally { setBusy(false) }
  }
  const cards = data?.health ?? []
  const bad = cards.filter((c) => c.light === 'bad').length
  const warn = cards.filter((c) => c.light === 'warn').length
  return (
    <section className="admin-body">
      <MetricsPanel />
      <Err err={err} />
      {data && (
        <p className={`health-sum ${bad ? 'bad' : warn ? 'warn' : 'ok'}`} role="status">
          {bad ? `${bad} 項異常，請先看紅燈卡片的「紅燈時怎麼辦」` : warn ? `${warn} 項需要注意，其餘正常` : '全部正常'}
        </p>
      )}
      <div className="health-grid" data-testid="health-cards">
        {cards.map((c) => (
          <article key={c.id} className={`health-card l-${c.light}`} data-testid={`health-${c.id}`}>
            <header>
              <i className="health-light" aria-hidden />
              <h3>{c.title}</h3>
              <span className="health-tag">{LIGHT[c.light]}</span>
            </header>
            <b className="health-head" data-testid={c.id === 'audit' ? 'audit-chain' : undefined}>{c.headline}</b>
            <p className="health-mean">{c.meaning}</p>
            {c.facts.length > 0 && <ul className="health-facts">{c.facts.map((f) => <li key={f}>{f}</li>)}</ul>}
            {!!c.degraded?.length && (
              <details className="health-deg"><summary>哪些元件在備援</summary>
                <ul>{c.degraded.map((d) => <li key={d.name}><b>{d.name}</b> <span className="muted">{d.why}</span></li>)}</ul></details>
            )}
            {c.verify && (
              <div className="health-verify">
                <button className="btn-ghost" onClick={() => void runVerify()} disabled={busy} data-testid="verify-now">{busy ? '驗證中…' : '立即驗證'}</button>
                {verify && <span className={verify.ok ? 'ok' : 'bad'} role="status">{verifyText(verify)}</span>}
              </div>
            )}
            <details className="health-fix" open={c.light === 'bad'}><summary>紅燈時怎麼辦</summary><p>{c.fix}</p></details>
          </article>
        ))}
      </div>
      <button className="btn-ghost" onClick={reload}>重新整理</button>
    </section>
  )
}
