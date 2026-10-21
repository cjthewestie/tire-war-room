// 記憶時間軸 (CJ, 2026-10-22: 「不是說要跟claudemem一樣」): the claude-mem viewer look over the plant's observations
// (warroom/data/memory_log.py ObservationStore, OB-<audit seq>, OB-<chain>.<seq> after a chain break). Newest first, grouped by day with that day's summary as a
// session card on top; type chips, machine and a search box (memory_search with its no-answer gate); a row opens its
// detail (memory_get). New observations appear as they are captured (polled). Read-only: GET /api/admin/memory/*.
import { useCallback, useEffect, useRef, useState } from 'react'
import { MACHINES, machineLabel } from '../factory'
import { fmtHM } from '../format'
import { plain } from '../plain'
import { adminFetch } from '../transport'
import { Err } from './adminCommon'
import './memory.css'

export interface Observation {
  id: string; seq: number; chain?: number; k?: number; ts: string; day: string; type: string; title: string
  machines: string[]; orders: string[]; ref: string | null; facts: Record<string, unknown>
}
interface SummaryCard { name: string; label: string; sentences: string[]; source: string | null; handled?: string; learned?: string; completed?: string; todo?: string }
interface Page { items: Observation[]; summaries: Record<string, SummaryCard>; next: number | null; last_seq: number; types: Record<string, [string, string]> }
interface SearchHit { id: string; level: string; date: string; score: number; text: string }

// the types memory_log.py writes (ICON / TYPE_LABEL), labelled for the supervisor
const OB_TYPES: Record<string, [string, string]> = {
  order: ['📦', '訂單'], event: ['🛠', '事件'], approve: ['✅', '核准'], reject: ['❌', '否決'], safety: ['⛔', '安全核心'],
  task: ['👷', '員工'], ship: ['🚚', '出貨'], note: ['📌', '主管指示'], chat: ['💬', '對話'],
}
const POLL_MS = 4000
// (chain, seq) as one number, as the server's cursor: OB-14.0026 (after an audit chain break) is newer than any OB-<seq>
const keyOf = (o: Observation) => o.k ?? (o.chain ?? 0) * 10_000_000 + o.seq
const FACT_LABEL: Record<string, string> = {
  message: '訊息', kind: '類別', command: '指令', version: '版本', reason: '原因', actions: '動作', delivery_impact: '交期影響',
  decision: '決定', option: '方案', request_id: '核准單', by: '由', hypothesis: '假設', verdict: '驗證', observed: '觀察值',
  state: '狀態', action: '動作', rule: '規則', value: '數值', msg: '訊息', task_id: '任務', worker: '員工', where: '地點',
  qty: '數量', on_time: '準時', order_id: '訂單', note_id: '筆記', text: '內容', pinned: '指示',
}

function factText(v: unknown): string {
  if (v === true) return '是'
  if (v === false) return '否'
  if (Array.isArray(v)) return v.map(factText).join('、')
  if (v && typeof v === 'object') return JSON.stringify(v)
  return plain(String(v))
}

/** One observation in full (memory_get): what the chat chips and the timeline rows open. */
export function ObservationDetail({ o }: { o: Observation }) {
  return (
    <dl className="ob-detail" data-testid="ob-detail">
      <dt>時間</dt><dd>{o.day}・{fmtHM(o.ts)}（稽核{o.chain ? `第 ${o.chain} 段` : ''}第 {o.seq} 筆）</dd>
      {!!o.machines.length && <><dt>機台</dt><dd>{o.machines.map(machineLabel).join('、')}</dd></>}
      {!!o.orders.length && <><dt>訂單</dt><dd>{o.orders.join('、')}</dd></>}
      {o.ref && <><dt>關聯</dt><dd>{o.ref}</dd></>}
      {Object.entries(o.facts).map(([k, v]) => <FactRow key={k} k={k} v={v} />)}
    </dl>
  )
}
function FactRow({ k, v }: { k: string; v: unknown }) {
  return <><dt>{FACT_LABEL[k] ?? k}</dt><dd>{factText(v)}</dd></>
}

/** OB-0042 chips under an overview reply (progressive disclosure: index → search → detail). Click opens memory_get. */
export function ObChips({ ids, label = '讀了記憶' }: { ids: string[]; label?: string }) {
  const [open, setOpen] = useState<string | null>(null)
  const [got, setGot] = useState<Record<string, Observation | null>>({})
  const [err, setErr] = useState<string | null>(null)
  const show = (id: string) => {
    if (open === id) { setOpen(null); return }
    setOpen(id); setErr(null)
    if (id in got) return
    adminFetch<{ items: Observation[] }>(`/api/admin/memory/observations/get?ids=${encodeURIComponent(id)}`)
      .then((r) => setGot((g) => ({ ...g, [id]: r.items[0] ?? null })))
      .catch((e) => setErr(String(e).replace(/^Error: /, '')))
  }
  const o = open ? got[open] : undefined
  return (
    <div className="ob-chips" data-testid="ob-chips">
      <span className="ob-chips-k">{label}</span>
      {ids.map((id) => <button key={id} type="button" className={`ob-chip ${open === id ? 'on' : ''}`} aria-expanded={open === id} onClick={() => show(id)} data-testid="ob-chip">{id}</button>)}
      {open && (
        <div className="ob-pop" role="region" aria-label={`記憶 ${open}`}>
          {err ? <p className="muted small">讀不到：{err}</p> : o === undefined ? <p className="muted small">讀取中…</p> : o === null ? <p className="muted small">沒有這筆紀錄</p>
            : <><p className="ob-pop-t"><span aria-hidden>{OB_TYPES[o.type]?.[0]}</span> {plain(o.title)}</p><ObservationDetail o={o} /></>}
        </div>
      )}
    </div>
  )
}

export function MemoryTimeline() {
  const [types, setTypes] = useState<string[]>([])
  const [machine, setMachine] = useState('')
  const [items, setItems] = useState<Observation[]>([])
  const [cards, setCards] = useState<Record<string, SummaryCard>>({})
  const [next, setNext] = useState<number | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [fresh, setFresh] = useState<Set<string>>(new Set())
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [q, setQ] = useState('')
  const [search, setSearch] = useState<{ q: string; found: boolean; hits: SearchHit[]; obs: Observation[] } | null>(null)
  const top = useRef(0)
  const qs = `types=${types.join(',')}&machine=${encodeURIComponent(machine)}`

  const merge = useCallback((p: Page, mode: 'reset' | 'older' | 'newer') => {
    setCards((c) => ({ ...(mode === 'reset' ? {} : c), ...p.summaries }))
    setItems((cur) => {
      const all = mode === 'reset' ? p.items : mode === 'older' ? [...cur, ...p.items] : [...p.items, ...cur]
      const seen = new Set<string>()
      return all.filter((o) => (seen.has(o.id) ? false : (seen.add(o.id), true))).sort((a, b) => keyOf(b) - keyOf(a))
    })
    if (mode !== 'newer') setNext(p.next)
    if (p.items.length) top.current = Math.max(top.current, keyOf(p.items[0]))
  }, [])

  useEffect(() => {
    let off = false
    top.current = 0
    adminFetch<Page>(`/api/admin/memory/observations?limit=60&${qs}`)
      .then((p) => { if (!off) { merge(p, 'reset'); setErr(null); setLoaded(true) } })
      .catch((e) => { if (!off) setErr(String(e).replace(/^Error: /, '')) })
    const t = setInterval(() => {
      adminFetch<Page>(`/api/admin/memory/observations?after=${top.current}&limit=200&${qs}`).then((p) => {
        if (off || !p.items.length) return
        merge(p, 'newer')
        setFresh(new Set(p.items.map((o) => o.id)))
      }).catch(() => {})
    }, POLL_MS)
    return () => { off = true; clearInterval(t) }
  }, [qs, merge])

  const older = () => {
    if (!next) return
    adminFetch<Page>(`/api/admin/memory/observations?before=${next}&limit=60&${qs}`).then((p) => merge(p, 'older')).catch((e) => setErr(String(e).replace(/^Error: /, '')))
  }
  const runSearch = async () => {
    const text = q.trim()
    if (!text) { setSearch(null); return }
    try {
      const r = await adminFetch<{ found: boolean; hits?: SearchHit[] }>(`/api/admin/memory/search?q=${encodeURIComponent(text)}`)
      const hits = r.found ? r.hits ?? [] : []
      const ids = hits.filter((h) => h.id.startsWith('OB-')).map((h) => h.id)
      const obs = ids.length ? (await adminFetch<{ items: Observation[] }>(`/api/admin/memory/observations/get?ids=${ids.join(',')}`)).items : []
      setSearch({ q: text, found: r.found, hits, obs })
    } catch (e) { setErr(String(e).replace(/^Error: /, '')) }
  }
  const toggle = (id: string) => setOpen((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })
  const flip = (t: string) => setTypes((ts) => (ts.includes(t) ? ts.filter((x) => x !== t) : [...ts, t]))

  const days: { day: string; obs: Observation[] }[] = []
  for (const o of items) {
    const last = days[days.length - 1]
    if (last && last.day === o.day) last.obs.push(o)
    else days.push({ day: o.day, obs: [o] })
  }
  const row = (o: Observation) => (
    <li key={o.id} className={`ob-row t-${o.type} ${fresh.has(o.id) ? 'fresh' : ''}`}>
      <button type="button" className="ob-line" aria-expanded={open.has(o.id)} onClick={() => toggle(o.id)} data-testid="ob-row">
        <span className="ob-ic" aria-hidden>{OB_TYPES[o.type]?.[0] ?? '•'}</span>
        <span className="ob-id">{o.id}</span>
        <time className="ob-t">{fmtHM(o.ts)}</time>
        <span className="ob-title">{plain(o.title)}</span>
        <span className="ob-type">{OB_TYPES[o.type]?.[1] ?? o.type}</span>
      </button>
      {open.has(o.id) && <ObservationDetail o={o} />}
    </li>
  )
  return (
    <section className="panel ob-tl" data-testid="memory-timeline">
      <header className="panel-h"><h2>記憶時間軸</h2>
        <span className="muted small">每筆稽核紀錄裡有意義的一行 → 一筆記憶（OB-編號＝稽核序號），新的在上面・唯讀・自動更新</span></header>
      <div className="ob-filters">
        <div className="ob-types" role="group" aria-label="類型">
          {Object.entries(OB_TYPES).map(([k, [ic, label]]) => (
            <button key={k} type="button" className={`ob-tchip ${types.includes(k) ? 'on' : ''}`} aria-pressed={types.includes(k)} onClick={() => flip(k)} data-testid={`ob-type-${k}`}>
              <span aria-hidden>{ic}</span>{label}
            </button>
          ))}
        </div>
        <div className="ob-tools">
          <select value={machine} onChange={(e) => setMachine(e.target.value)} aria-label="機台" data-testid="ob-machine">
            <option value="">全部機台</option>
            {MACHINES.map((x) => <option key={x.id} value={x.id}>{machineLabel(x.id)}</option>)}
          </select>
          <form className="ob-search" role="search" onSubmit={(e) => { e.preventDefault(); void runSearch() }}>
            <input value={q} onChange={(e) => { setQ(e.target.value); if (!e.target.value.trim()) setSearch(null) }} placeholder="搜尋記憶，例如 7 號機 溫度、SO-004" aria-label="搜尋記憶時間軸" data-testid="ob-q" />
            <button type="submit" className="btn-ghost">搜尋</button>
          </form>
        </div>
      </div>
      <Err err={err} />
      {search && (
        <div className="ob-results" data-testid="ob-search-results">
          <p className="small"><b>「{search.q}」</b>{search.found ? `：找到 ${search.hits.length} 筆` : '：找不到（相符程度太低，不算查到）'}
            <button type="button" className="btn-ghost small" onClick={() => { setQ(''); setSearch(null) }}>清除</button></p>
          <ul className="ob-list">
            {search.obs.map(row)}
            {search.hits.filter((h) => !h.id.startsWith('OB-')).map((h) => (
              <li key={h.id} className="ob-row t-summary"><div className="ob-line static"><span className="ob-ic" aria-hidden>🗓</span><span className="ob-id">{h.level === 'daily' ? '每日摘要' : h.level === 'weekly' ? '每週摘要' : '每月摘要'}</span><span className="ob-title">{plain(h.text)}</span></div></li>
            ))}
          </ul>
        </div>
      )}
      {!search && loaded && !items.length && <p className="muted small">還沒有記憶{types.length || machine ? '符合這些條件' : '：工廠跑起來後，訂單、事件、核准、出貨都會一筆一筆出現在這裡'}。</p>}
      {!search && days.map((d) => {
        const c = cards[d.day]
        return (
          <section key={d.day} className="ob-day" data-testid="ob-day">
            <h3 className="ob-day-h">{c?.label ?? d.day.replace(/-loop(\d+)$/, '（循環 $1）')}<span className="muted small">{d.obs.length} 筆</span></h3>
            {c && (
              <div className="ob-card" data-testid="ob-summary">
                <p className="ob-card-k">🗓 每日摘要<span className="muted small">{c.source === 'model' ? 'AI 依事實寫成' : '程式整理'}</span></p>
                {(c.handled || c.learned || c.completed || c.todo) ? (
                  <dl className="ob-card-g">
                    {c.handled && <><dt>處理了</dt><dd>{c.handled}</dd></>}
                    {c.learned && <><dt>學到</dt><dd>{c.learned}</dd></>}
                    {c.completed && <><dt>完成</dt><dd>{c.completed}</dd></>}
                    {c.todo && <><dt>待辦</dt><dd>{c.todo}</dd></>}
                  </dl>
                ) : null}
                {!!c.sentences.length && <p className="small ob-card-s">{c.sentences.join('')}</p>}
              </div>
            )}
            <ul className="ob-list">{d.obs.map(row)}</ul>
          </section>
        )
      })}
      {!search && next && <button type="button" className="btn-ghost ob-more" onClick={older} data-testid="ob-older">載入更早的</button>}
    </section>
  )
}
