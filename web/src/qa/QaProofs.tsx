// 「評審疑問 → 展示」: the 10 live proofs, each runnable here or from an answer card's 「▶ 現場展示」 button.
// A proof that changes the sim asks first when the plant is mid-demo (open events or pending approvals).
import type { Proof, ProofResult } from './qaDemo'
import type { ProofsApi } from './useProofs'



const STATUS: Record<string, string> = { ok: '展示成功', failed: '有一步沒過', pending: '待補', error: '出錯' }

function detailText(d: unknown): string | null {
  if (d == null) return null
  if (typeof d === 'string') return d
  return JSON.stringify(d, null, 1)
}

export function ProofResultView({ r }: { r: ProofResult }) {
  const external = r.page.startsWith('/')
  return (
    <div className={`qa-proof-res ${r.status}`} data-testid="qa-proof-result">
      <div className="qa-proof-res-h">
        <b>{r.title}</b><span className="qa-pill">{STATUS[r.status] ?? r.status}</span>
        <span className="qa-dim">{(r.ms / 1000).toFixed(1)} 秒</span>
        {r.page && r.page !== '#qa' && <a className="qa-live" href={r.page} target={external ? '_blank' : undefined} rel="noopener">去看 →</a>}
      </div>
      {r.error && <p className="qa-err">{r.error}</p>}
      <ol className="qa-proof-steps">
        {r.steps.map((s, i) => {
          const d = detailText(s.detail)
          return (
            <li key={i} className={s.ok ? 'ok' : 'no'}>
              <span className="qa-tick" aria-label={s.ok ? '通過' : '未通過'}>{s.ok ? '✓' : '✗'}</span>
              <span>{s.label}</span>
              {d && (d.length > 140 ? <details><summary>細節</summary><pre>{d}</pre></details> : <q>{d}</q>)}
            </li>
          )
        })}
      </ol>
    </div>
  )
}

export function ProofButton({ p, api }: { p: Proof; api: ProofsApi }) {
  return (
    <button className="qa-proof-btn" disabled={!!api.running || p.status === 'pending'} onClick={() => void api.run(p)}
      data-testid="qa-proof-run" title={p.expected}>
      {api.running === p.id ? '展示中…' : p.status === 'pending' ? `${p.title}（待補）` : `▶ 現場展示：${p.title}`}
    </button>
  )
}

export function ProofIndex({ api }: { api: ProofsApi }) {
  const { reg, results, err } = api
  return (
    <section className="qa-sec" aria-labelledby="qa-proofs-h" data-testid="qa-proofs">
      <h3 id="qa-proofs-h">評審疑問 → 展示</h3>
      <p className="qa-dim">每一項都在現在的模擬裡實際跑一次，標記「QA 展示」；不會替主管核准任何東西，跑完會還原改過的設定。</p>
      {err && <p className="qa-err">{err}</p>}
      {!reg ? <p className="qa-dim">載入中…</p> : (
        <ol className="qa-proof-list">
          {reg.proofs.map((p) => (
            <li key={p.id} className="qa-proof" data-testid="qa-proof">
              <div className="qa-proof-top">
                <div>
                  <b>「{p.doubt}」</b>
                  <p className="qa-dim">{p.steps.join(' → ')}</p>
                  <p className="qa-proof-exp">會看到：{p.expected}</p>
                </div>
                <ProofButton p={p} api={api} />
              </div>
              {results[p.id] && <ProofResultView r={results[p.id]} />}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
