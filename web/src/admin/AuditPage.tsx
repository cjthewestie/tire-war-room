// 後台 稽核紀錄: a read-only viewer over the audit log (GET /api/admin/audit, warroom/api/admin_audit.py). Newest first,
// 100 a page, filters by kind / actor / event / time / free text; a row opens to its full payload with hash and
// prev_hash. The top bar verifies the whole chain and exports the filtered range as JSONL (the export is audited).
// Nothing here can edit or delete an entry. CJ, 2026-10-22: 「稽核紀錄跟系統運作基本參數需要在這裡」.
import { useEffect, useState } from 'react'
import { adminDownload, adminFetch } from '../transport'
import { Err } from './adminCommon'
import { verifyText, type Verify } from './adminData'

interface Row {
  seq: number; ts: string; kind: string; kind_label: string; actor: string; event_id: string | null; summary: string
  payload: Record<string, unknown>; hash: string; prev_hash: string
  llm?: { model: string | null; prompt_tokens: number | null; completion_tokens: number | null; total_ms: number | null; plant_sha256: string | null }
  filter?: { category: string | null; matched: string | null; side: string | null }
}
interface PageData { rows: Row[]; next_cursor: number | null; kinds: { id: string; label: string }[]; last_seq: number }
interface Filters { kind: string; actor: string; event: string; q: string; from: string; to: string }
const EMPTY: Filters = { kind: '', actor: '', event: '', q: '', from: '', to: '' }

function query(f: Filters, cursor?: number | null): string {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(f)) if (v.trim()) p.set(k, v.trim())
  if (cursor) p.set('cursor', String(cursor))
  const s = p.toString()
  return s ? `?${s}` : ''
}
const hm = (ts: string) => ts.slice(5, 19).replace('T', ' ')
const KIND_TONE: Record<string, string> = { llm_call: 'k-llm', safety_action: 'k-bad', intercepted: 'k-bad', gate: 'k-gate', approval_request: 'k-gate', approval_decision: 'k-gate', anomaly: 'k-warn' }

export function AuditPage() {
  const [draft, setDraft] = useState<Filters>(EMPTY)
  const [f, setF] = useState<Filters>(EMPTY)
  const [pages, setPages] = useState<PageData[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState<Set<number>>(new Set())
  const [verify, setVerify] = useState<Verify | null>(null)
  const [vBusy, setVBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  const [loadedFor, setLoadedFor] = useState<Filters | null>(null)
  useEffect(() => {
    let off = false
    adminFetch<PageData>(`/api/admin/audit${query(f)}`)
      .then((d) => { if (!off) { setPages([d]); setOpen(new Set()); setErr(null); setLoadedFor(f) } })
      .catch((e) => { if (!off) { setErr(String(e).replace(/^Error: /, '')); setLoadedFor(f) } })
    return () => { off = true }
  }, [f])
  useEffect(() => {
    let off = false
    adminFetch<Verify>('/api/admin/audit/verify').then((v) => { if (!off) setVerify(v) }).catch(() => {})
    return () => { off = true }
  }, [])
  const loading = busy || loadedFor !== f

  const runVerify = async () => {
    setVBusy(true)
    try { setVerify(await adminFetch<Verify>('/api/admin/audit/verify')) } catch (e) { setErr(String(e).replace(/^Error: /, '')) } finally { setVBusy(false) }
  }
  const more = async () => {
    const last = pages[pages.length - 1]
    if (!last?.next_cursor) return
    setBusy(true)
    try { const d = await adminFetch<PageData>(`/api/admin/audit${query(f, last.next_cursor)}`); setPages((p) => [...p, d]) }
    catch (e) { setErr(String(e).replace(/^Error: /, '')) } finally { setBusy(false) }
  }
  const exportJsonl = async () => {
    setMsg(null)
    try { const n = await adminDownload(`/api/admin/audit/export${query(f)}`); setMsg(`已匯出 ${n} 筆（這次匯出也記在稽核紀錄）`) }
    catch (e) { setMsg(`匯出失敗：${String(e).replace(/^Error: /, '')}`) }
  }
  const toggle = (seq: number) => setOpen((s) => { const n = new Set(s); if (n.has(seq)) n.delete(seq); else n.add(seq); return n })
  const rows = pages.flatMap((p) => p.rows)
  const kinds = pages[0]?.kinds ?? []
  const next = pages[pages.length - 1]?.next_cursor
  const active = Object.values(f).some((v) => v.trim())

  return (
    <section className="admin-body" data-testid="audit-page">
      <div className="audit-bar">
        <span className={`audit-chain ${verify ? (verify.ok ? 'ok' : 'bad') : ''}`} data-testid="audit-chain-status">
          雜湊鏈 {verify ? verifyText(verify) : '驗證中…'}{verify && !verify.ok && verify.problem ? <small className="muted">（{verify.problem}）</small> : null}
        </span>
        <span className="muted small">共 {pages[0]?.last_seq ?? '—'} 筆・唯讀，不能修改或刪除</span>
        <span className="audit-bar-act">
          <button className="btn-ghost" onClick={() => void runVerify()} disabled={vBusy} data-testid="audit-verify">{vBusy ? '驗證中…' : '驗證整條鏈'}</button>
          <button className="btn-ghost" onClick={() => void exportJsonl()} data-testid="audit-export">匯出 JSONL{active ? '（目前篩選）' : '（全部）'}</button>
        </span>
      </div>
      {msg && <p className="note">{msg}</p>}
      <form className="audit-filters" onSubmit={(e) => { e.preventDefault(); setF({ ...draft }) }} aria-label="篩選稽核紀錄">
        <select value={draft.kind} onChange={(e) => { const d = { ...draft, kind: e.target.value }; setDraft(d); setF(d) }} aria-label="種類" data-testid="audit-kind">
          <option value="">全部種類</option>
          {kinds.map((k) => <option key={k.id} value={k.id}>{k.label}（{k.id}）</option>)}
        </select>
        <input value={draft.actor} onChange={(e) => setDraft({ ...draft, actor: e.target.value })} placeholder="執行者，例如 commander" aria-label="執行者" />
        <input value={draft.event} onChange={(e) => setDraft({ ...draft, event: e.target.value })} placeholder="事件編號 EV-…" aria-label="事件編號" />
        <label className="audit-time"><span>從</span><input type="datetime-local" value={draft.from} onChange={(e) => setDraft({ ...draft, from: e.target.value })} aria-label="開始時間" /></label>
        <label className="audit-time"><span>到</span><input type="datetime-local" value={draft.to} onChange={(e) => setDraft({ ...draft, to: e.target.value })} aria-label="結束時間" /></label>
        <input className="audit-q" value={draft.q} onChange={(e) => setDraft({ ...draft, q: e.target.value })} placeholder="搜尋任何文字" aria-label="搜尋文字" data-testid="audit-q" />
        <button type="submit" className="btn-approve">篩選</button>
        {active && <button type="button" className="btn-ghost" onClick={() => { setDraft(EMPTY); setF(EMPTY) }}>清除</button>}
      </form>
      <Err err={err} />
      <div className="admin-table-wrap">
        <table className="admin-table audit-table" data-testid="audit-rows">
          <thead><tr><th>#</th><th>時間（工廠）</th><th>種類</th><th>執行者</th><th>事件</th><th>摘要</th></tr></thead>
          <tbody>{rows.map((r) => {
            const isOpen = open.has(r.seq)
            return [
              <tr key={r.seq} className={`audit-row ${isOpen ? 'open' : ''}`} onClick={() => toggle(r.seq)} aria-expanded={isOpen} tabIndex={0}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(r.seq) } }} data-kind={r.kind}>
                <td className="num">{r.seq}</td>
                <td className="muted nowrap">{hm(r.ts)}</td>
                <td><span className={`audit-kind ${KIND_TONE[r.kind] ?? ''}`}>{r.kind_label}</span></td>
                <td className="audit-actor">{r.actor}</td>
                <td className="audit-ev">{r.event_id ?? '—'}</td>
                <td className="audit-sum"><span className="audit-caret" aria-hidden>{isOpen ? '▾' : '▸'}</span>{r.summary}</td>
              </tr>,
              isOpen && (
                <tr key={`${r.seq}-d`} className="audit-detail"><td colSpan={6}>
                  {r.llm && (
                    <dl className="audit-llm">
                      <div><dt>模型</dt><dd>{r.llm.model ?? '—'}</dd></div>
                      <div><dt>Tokens（輸入→輸出）</dt><dd>{r.llm.prompt_tokens ?? '—'} → {r.llm.completion_tokens ?? '—'}</dd></div>
                      <div><dt>耗時</dt><dd>{r.llm.total_ms != null ? `${(r.llm.total_ms / 1000).toFixed(1)} 秒` : '—'}</dd></div>
                      <div><dt>PLANT.md 版本</dt><dd><code>{r.llm.plant_sha256 ? r.llm.plant_sha256.slice(0, 16) : '—'}</code></dd></div>
                    </dl>
                  )}
                  {r.filter && (
                    <dl className="audit-llm">
                      <div><dt>類別</dt><dd>{r.filter.category ?? '—'}</dd></div>
                      <div><dt>被擋的字（遮蔽）</dt><dd>{r.filter.matched ?? '—'}</dd></div>
                      <div><dt>方向</dt><dd>{r.filter.side === 'input' ? '主管輸入' : r.filter.side === 'output' ? '模型輸出' : r.filter.side ?? '—'}</dd></div>
                    </dl>
                  )}
                  <pre className="audit-json">{JSON.stringify(r.payload, null, 2)}</pre>
                  <p className="audit-hash"><span>hash</span><code>{r.hash}</code></p>
                  <p className="audit-hash"><span>prev_hash</span><code>{r.prev_hash}</code></p>
                </td></tr>
              ),
            ]
          })}</tbody>
        </table>
      </div>
      {!rows.length && !loading && !err && <p className="muted">沒有符合的紀錄。</p>}
      <div className="admin-row">
        {next ? <button className="btn-ghost" onClick={() => void more()} disabled={loading} data-testid="audit-more">{loading ? '讀取中…' : '再載入 100 筆'}</button>
          : rows.length > 0 && <span className="muted small">已到最舊的一筆</span>}
        <span className="muted small">顯示 {rows.length} 筆</span>
      </div>
    </section>
  )
}
