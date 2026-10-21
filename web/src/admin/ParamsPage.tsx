// 後台 系統參數: the live operating parameters, grouped by area, read-only (GET /api/admin/params,
// warroom/api/admin_params.py). Each row: the value now, what it means, where it comes from, and whether it can be
// changed while running (no, for now). A value this build does not have reads 「未啟用」.
import { useState } from 'react'
import { Err } from './adminCommon'
import { useLoad } from './adminData'

interface Param { key: string; value: unknown; enabled: boolean; meaning: string; source: string; runtime: boolean }
interface Params { groups: { id: string; label: string; rows: Param[] }[]; note: string }

function show(v: unknown): string {
  if (v === true) return '是'
  if (v === false) return '否'
  if (v == null) return '—'
  if (Array.isArray(v)) return v.join(' → ')
  if (typeof v === 'object') return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k}: ${Array.isArray(x) ? x.join('、') : String(x)}`).join('\n')
  return String(v)
}

export function ParamsPage() {
  const { data, err } = useLoad<Params>('/api/admin/params')
  const [q, setQ] = useState('')
  const s = q.trim().toLowerCase()
  const groups = (data?.groups ?? []).map((g) => ({ ...g, rows: g.rows.filter((r) => !s || `${r.key} ${r.meaning} ${show(r.value)} ${r.source}`.toLowerCase().includes(s)) })).filter((g) => g.rows.length)
  return (
    <section className="admin-body" data-testid="params-page">
      <Err err={err} />
      {data && <p className="note">{data.note}</p>}
      <div className="admin-row">
        <form className="order-search" role="search" onSubmit={(e) => e.preventDefault()}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜尋參數，例如 timeout、溫度、Nemo" aria-label="搜尋參數" />
        </form>
        <nav className="params-jump" aria-label="跳到分類">{(data?.groups ?? []).map((g) => <a key={g.id} href={`#admin/params`} onClick={(e) => { e.preventDefault(); document.getElementById(`pg-${g.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}>{g.label}</a>)}</nav>
      </div>
      {groups.map((g) => (
        <section key={g.id} id={`pg-${g.id}`} className="params-group">
          <h3 className="sub">{g.label}<small className="muted">{g.rows.length} 項</small></h3>
          <div className="admin-table-wrap">
            <table className="admin-table params-table" data-testid={`params-${g.id}`}>
              <thead><tr><th>參數</th><th>目前值</th><th>意思</th><th>來源</th><th>執行中可改</th></tr></thead>
              <tbody>{g.rows.map((r) => (
                <tr key={r.key} className={r.enabled ? '' : 'off'}>
                  <td><code>{r.key}</code></td>
                  <td className="params-val">{r.enabled ? <b>{show(r.value)}</b> : <em className="tag">未啟用</em>}</td>
                  <td>{r.meaning}</td>
                  <td className="muted params-src"><code>{r.source}</code></td>
                  <td className="muted">{r.runtime ? '可以' : '不行'}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </section>
      ))}
      {data && !groups.length && <p className="muted">沒有符合的參數。</p>}
    </section>
  )
}
