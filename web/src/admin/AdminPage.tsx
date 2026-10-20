// 後台 (#admin, issue #37), first version. CJ, 2026-10-20: 「後台的話我是希望可以存取一些 memory 或者是 RAG 資料庫的維護」.
// Four pages: 狀態 (health, model, audit chain), RAG 知識庫 (SOP files + team chunks, upload, re-index, search
// tester, delete with confirm), 記憶 (past incidents + closed events per machine, memory notes the agent does not
// read yet) and 執行紀錄 (events, tasks and questions). Server: warroom/api/admin.py — supervisor PIN, factory
// network only, every write audited.
import { useCallback, useEffect, useMemo, useState } from 'react'
import { MACHINES, machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { plain } from '../plain'
import { STATE_LABEL } from '../components/common'
import { adminFetch } from '../transport'
import { useWarRoom } from '../useWarRoom'
import './admin.css'

type Page = 'status' | 'rag' | 'memory' | 'runs'
const PAGES: { id: Page; label: string }[] = [
  { id: 'status', label: '狀態' }, { id: 'rag', label: 'RAG 知識庫' }, { id: 'memory', label: '記憶' }, { id: 'runs', label: '執行紀錄' },
]

interface Job { state: 'idle' | 'running' | 'done' | 'failed'; phase: string; files: number; chunks: number | null; team_chunks: number | null; mode: string | null; error: string | null; elapsed_s: number | null }
interface Status {
  llm_mode: string; model: string; llm_host: string | null; llm_reachable: boolean; kb_ok: boolean; kb_mode: string; team_mode: string
  audit_chain_ok: boolean; audit_entries: number; audit_last_seq: number; degraded: Record<string, string>; scenario: string
  factory_time: string; uptime_s: number; network: string; memory_notes: number; reindex: Job
}
interface Doc { file: string; sop_id: string; title: string; chunks: number; bytes: number; modified: string; sections: string[]; error: string | null }
interface Rag {
  folder: string; docs: Doc[]; team: { sop_id: string; title: string; chunks: number }[]; kb_mode: string; team_mode: string
  indexed_chunks: number; folder_chunks: number; qdrant: Record<string, number>; reindex: Job
}
interface Hit { sop_id: string; title: string; source: string; score: number; text: string }
interface Incident { id: string; date: string; machine_id: string | null; equipment: string; symptom: string; action: string; downtime_min: number; outcome: string }
interface Closed { id: string; machine_id: string | null; closed_at: string; state: string; what: string | null; outcome: string | null }
interface Note { id: string; machine_id: string; text: string; author: string; created_at: string }
interface Memory { incidents: Incident[]; closed_events: Closed[]; notes: Note[]; agent_reads_notes: boolean }
interface Run { id: string; kind: 'event' | 'command' | 'ask' | 'chat'; ts: string; by?: string; machine_id?: string; state?: string; title?: string | null; question?: string | null; answer?: string | null; outcome?: string | null; tools?: number; replans?: number }

export function AdminPage() {
  const { transport } = useWarRoom()
  const [page, setPage] = useState<Page>(() => (/#admin\/(\w+)/.exec(location.hash)?.[1] as Page) || 'status')
  const go = (p: Page) => { setPage(p); history.replaceState(null, '', `${location.pathname}${location.search}#admin/${p}`) }
  if (transport?.mode === 'mock') return <div className="loading">後台需要連線後端（瀏覽器模擬模式沒有知識庫與稽核紀錄）。</div>
  return (
    <div className="admin" data-testid="admin-page">
      <header className="admin-h">
        <h2>後台</h2>
        <nav className="seg" aria-label="後台頁面">
          {PAGES.map((p) => <button key={p.id} className={page === p.id ? 'on' : ''} aria-pressed={page === p.id} onClick={() => go(p.id)} data-testid={`admin-${p.id}`}>{p.label}</button>)}
        </nav>
        <span className="muted small">只限廠內網路・需主管 PIN（現場螢幕免輸入）・每次寫入都記稽核</span>
      </header>
      {page === 'status' ? <StatusPage /> : page === 'rag' ? <RagPage /> : page === 'memory' ? <MemoryPage /> : <RunsPage />}
    </div>
  )
}

function useLoad<T>(path: string): { data: T | null; err: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [n, setN] = useState(0)
  useEffect(() => {
    let off = false
    adminFetch<T>(path).then((d) => { if (!off) { setData(d); setErr(null) } }).catch((e) => { if (!off) setErr(String(e).replace(/^Error: /, '')) })
    return () => { off = true }
  }, [path, n])
  return { data, err, reload: useCallback(() => setN((x) => x + 1), []) }
}

function Err({ err }: { err: string | null }) {
  if (!err) return null
  return <p className="note admin-err" role="alert">{/^40[13]/.test(err) ? `沒有權限：${err}。後台只限廠內網路，遠端需輸入主管 PIN。` : `讀取失敗：${err}`}</p>
}

// ---------------------------------------------------------------- 狀態
function StatusPage() {
  const { data: s, err, reload } = useLoad<Status>('/api/admin/status')
  return (
    <section className="admin-body">
      <Err err={err} />
      {s && (
        <div className="admin-cards">
          <Card title="模型" tone={s.llm_mode === 'mock' ? 'muted' : s.llm_reachable ? 'ok' : 'bad'}>
            <b>{s.llm_mode === 'llm' ? `Nemotron・${s.model}` : '規則模式（未呼叫模型）'}</b>
            <span>{s.llm_mode === 'mock' ? '指揮官用規則產生計畫' : `${s.llm_host ?? ''}・${s.llm_reachable ? '連得上' : '連不上'}`}</span>
          </Card>
          <Card title="SOP 知識庫" tone={s.kb_ok ? 'ok' : 'bad'}>
            <b>{s.kb_mode === 'vector' ? '向量＋字詞混合搜尋' : s.kb_mode === 'lexical' ? '只有字詞搜尋（嵌入模型未載入）' : s.kb_mode}</b>
            <span>團隊資料集：{s.team_mode === 'off' ? '未載入' : s.team_mode === 'vector' ? '向量' : '字詞'}・{s.kb_ok ? '可用' : '不可用'}</span>
          </Card>
          <Card title="稽核紀錄" tone={s.audit_chain_ok ? 'ok' : 'bad'}>
            <b data-testid="audit-chain">{s.audit_chain_ok ? '雜湊鏈完整' : '雜湊鏈有問題'}</b>
            <span>{s.audit_entries} 筆・最新 #{s.audit_last_seq}</span>
          </Card>
          <Card title="系統" tone={Object.keys(s.degraded).length ? 'warn' : 'ok'}>
            <b>已運轉 {Math.floor(s.uptime_s / 3600)} 小時 {Math.floor((s.uptime_s % 3600) / 60)} 分</b>
            <span>情境 {s.scenario}・網路 {s.network}・工廠時間 {fmtMD(s.factory_time)} {fmtHM(s.factory_time)}</span>
          </Card>
          <Card title="記憶筆記" tone="muted"><b>{s.memory_notes} 則</b><span>Agent 目前不讀（唯讀、未接進提示詞）</span></Card>
          <Card title="上次重新索引" tone={s.reindex.state === 'failed' ? 'bad' : 'muted'}>
            <b>{JOB_TEXT[s.reindex.state]}</b><span>{s.reindex.chunks != null ? `${s.reindex.chunks} 段・${s.reindex.elapsed_s} 秒` : '這次啟動後還沒做'}</span>
          </Card>
        </div>
      )}
      {s && Object.keys(s.degraded).length > 0 && (
        <details className="admin-deg"><summary>降級中的元件（{Object.keys(s.degraded).length}）</summary>
          <ul>{Object.entries(s.degraded).map(([k, v]) => <li key={k}><b>{k}</b> <span className="muted">{v}</span></li>)}</ul></details>
      )}
      <button className="btn-ghost" onClick={reload}>重新整理</button>
    </section>
  )
}
const JOB_TEXT: Record<Job['state'], string> = { idle: '尚未執行', running: '進行中', done: '完成', failed: '失敗' }

function Card({ title, tone, children }: { title: string; tone: 'ok' | 'bad' | 'warn' | 'muted'; children: React.ReactNode }) {
  return <div className={`admin-card t-${tone}`}><span className="admin-card-k"><i className={`dot ${tone === 'ok' ? 'ok' : tone === 'bad' ? 'bad' : tone === 'warn' ? 'data' : 'idle'}`} />{title}</span>{children}</div>
}

// ---------------------------------------------------------------- RAG
function RagPage() {
  const { data: r, err, reload } = useLoad<Rag>('/api/admin/rag')
  const [job, setJob] = useState<Job | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const running = (job ?? r?.reindex)?.state === 'running'
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => {
      adminFetch<Job>('/api/admin/rag/reindex').then((j) => { setJob(j); if (j.state !== 'running') reload() }).catch(() => {})
    }, 700)
    return () => clearInterval(t)
  }, [running, reload])
  const reindex = async () => {
    setMsg(null)
    try { setJob(await adminFetch<Job>('/api/admin/rag/reindex', { method: 'POST' })) } catch (e) { setMsg(`重新索引失敗：${String(e).replace(/^Error: /, '')}`) }
  }
  const del = async (d: Doc) => {
    if (!window.confirm(`刪除 ${d.file}（${d.sop_id} ${d.title}，${d.chunks} 段）？\n刪除後重新索引，Agent 就查不到這份 SOP。這個動作會記在稽核紀錄。`)) return
    try {
      await adminFetch(`/api/admin/rag/docs/${encodeURIComponent(d.file)}?confirm=${encodeURIComponent(d.file)}`, { method: 'DELETE' })
      setMsg(`已刪除 ${d.file}。請按「重新索引」讓 Agent 更新。`); reload()
    } catch (e) { setMsg(`刪除失敗：${String(e).replace(/^Error: /, '')}`) }
  }
  const j = job ?? r?.reindex
  const stale = !!r && r.indexed_chunks !== r.folder_chunks
  return (
    <section className="admin-body">
      <Err err={err} />
      {msg && <p className="note">{msg}</p>}
      {r && (
        <>
          <div className="admin-cards">
            <Card title="SOP 文件" tone="muted"><b>{r.docs.length} 份・{r.folder_chunks} 段</b><span>warroom/kb/{r.folder}/</span></Card>
            <Card title="團隊資料集" tone="muted"><b>{r.team.length} 份・{r.team.reduce((a, t) => a + t.chunks, 0)} 段</b><span>data/team_dataset/sop_chunks.jsonl（唯讀）</span></Card>
            <Card title="Qdrant 索引" tone={stale ? 'warn' : 'ok'}>
              <b>{r.indexed_chunks} 段已索引{stale ? '・資料夾有變動' : ''}</b>
              <span>{Object.keys(r.qdrant).length ? Object.entries(r.qdrant).map(([c, n]) => `${c} ${n} 筆`).join('・') : r.kb_mode === 'vector' ? '—' : '目前是字詞搜尋，沒有向量索引'}</span>
            </Card>
          </div>
          <div className="admin-row">
            <button className="btn-approve" onClick={reindex} disabled={running} data-testid="reindex">{running ? '重新索引中…' : '重新索引'}</button>
            {j && j.state !== 'idle' && (
              <span className={`admin-job s-${j.state}`} role="status" aria-live="polite">
                {j.state === 'running' && <i className="admin-prog" aria-hidden />}
                {j.state === 'running' ? `${j.phase}（${j.files} 份文件・${j.elapsed_s ?? 0} 秒）` : j.state === 'done' ? `完成：${j.chunks} 段＋團隊 ${j.team_chunks} 段・${j.mode === 'vector' ? '向量' : '字詞'}・${j.elapsed_s} 秒` : `失敗：${j.error}`}
              </span>
            )}
            {stale && !running && <span className="muted small">資料夾和索引不一致（{r.folder_chunks} vs {r.indexed_chunks} 段），請重新索引</span>}
          </div>
          <Upload onDone={(m) => { setMsg(m); reload() }} />
          <h3 className="sub">SOP 文件</h3>
          <div className="admin-table-wrap">
            <table className="admin-table" data-testid="sop-docs">
              <thead><tr><th>檔案</th><th>SOP</th><th>段數</th><th>大小</th><th>更新</th><th aria-label="動作" /></tr></thead>
              <tbody>{r.docs.map((d) => (
                <tr key={d.file}>
                  <td><code>{d.file}</code>{d.error && <small className="warn"> 讀不到：{d.error}</small>}</td>
                  <td><b>{d.sop_id}</b> {d.title}</td><td>{d.chunks}</td><td>{(d.bytes / 1024).toFixed(1)} KB</td>
                  <td className="muted">{d.modified.slice(5, 16).replace('T', ' ')}</td>
                  <td><button className="btn-ghost danger" onClick={() => void del(d)} aria-label={`刪除 ${d.file}`}>刪除</button></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <details className="admin-team"><summary>團隊資料集 SOP（{r.team.length} 份，唯讀）</summary>
            <ul>{r.team.map((t) => <li key={t.sop_id}><b>{t.sop_id}</b> {t.title} <span className="muted">{t.chunks} 段</span></li>)}</ul>
          </details>
          <SearchTester />
        </>
      )}
    </section>
  )
}

function Upload({ onDone }: { onDone: (msg: string) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [overwrite, setOverwrite] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const send = async () => {
    if (!file) return
    setBusy(true); setErr(null)
    try {
      const buf = new Uint8Array(await file.arrayBuffer())
      let bin = ''
      for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000))
      await adminFetch('/api/admin/rag/docs', { method: 'POST', body: { name: file.name, content_b64: btoa(bin), overwrite } })
      onDone(`已上傳 ${file.name}。請按「重新索引」讓 Agent 查得到。`); setFile(null)
    } catch (e) { setErr(String(e).replace(/^Error: /, '')) } finally { setBusy(false) }
  }
  return (
    <div className="admin-upload">
      <label className="admin-file"><span>上傳 SOP</span>
        <input type="file" accept=".md,.txt,.pdf" onChange={(e) => setFile(e.target.files?.[0] ?? null)} data-testid="sop-file" /></label>
      <label className="admin-check"><input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />同名就覆蓋</label>
      <button className="btn-ghost" onClick={() => void send()} disabled={!file || busy}>{busy ? '上傳中…' : '上傳'}</button>
      <span className="muted small">.md／.txt（UTF-8）或 .pdf，2 MB 以內；Markdown 第一行寫「# SOP-XXX-001 標題」，用「## 小節」分段</span>
      {err && <p className="err">{err}</p>}
    </div>
  )
}

function SearchTester() {
  const [q, setQ] = useState('')
  const [k, setK] = useState(5)
  const [res, setRes] = useState<{ hits: Hit[]; team_hits: Hit[]; mode: string; ms: number } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const run = async (text: string) => {
    if (!text.trim()) return
    setErr(null)
    try { setRes(await adminFetch(`/api/admin/rag/search?q=${encodeURIComponent(text)}&k=${k}`)) } catch (e) { setErr(String(e).replace(/^Error: /, '')) }
  }
  return (
    <section className="admin-search" aria-label="搜尋測試">
      <h3 className="sub">搜尋測試：Agent 問這句會查到什麼</h3>
      <form className="order-search" onSubmit={(e) => { e.preventDefault(); void run(q) }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="例如：硫化中斷處置、蒸汽壓力低" aria-label="搜尋測試" data-testid="rag-q" />
        <select value={k} onChange={(e) => setK(Number(e.target.value))} aria-label="筆數">{[3, 5, 10].map((n) => <option key={n} value={n}>前 {n} 筆</option>)}</select>
        <button type="submit" className="btn-approve">查</button>
      </form>
      <div className="admin-ex">{['硫化中斷處置', '7 號機溫度掉', '夜班缺人'].map((x) => <button key={x} type="button" onClick={() => { setQ(x); void run(x) }}>{x}</button>)}</div>
      {err && <p className="err">{err}</p>}
      {res && (
        <div className="admin-hits" data-testid="rag-hits">
          <p className="muted small">{res.mode === 'vector' ? '向量＋字詞混合分數' : '字詞分數'}・{res.ms} ms・每份 SOP 只列最相近的一段</p>
          {[{ k: 'SOP 文件', list: res.hits }, { k: '團隊資料集', list: res.team_hits }].map((g) => (
            <div key={g.k}>
              <h4>{g.k}（{g.list.length}）</h4>
              <ol>{g.list.map((h, i) => (
                <li key={i}><div className="hit-h"><b>{h.sop_id}</b> {h.title}<span className="hit-score">{h.score.toFixed(3)}</span><small className="muted">{h.source}</small></div>
                  <p className="hit-t">{h.text.slice(0, 220)}{h.text.length > 220 ? '…' : ''}</p></li>
              ))}</ol>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- 記憶
function MemoryPage() {
  const { data: m, err, reload } = useLoad<Memory>('/api/admin/memory')
  const [q, setQ] = useState('')
  const [mid, setMid] = useState<string>('')
  const query = q.trim().toLowerCase()
  const groups = useMemo(() => {
    if (!m) return []
    const has = (s: string) => !query || s.toLowerCase().includes(query)
    const by = new Map<string, { inc: Incident[]; ev: Closed[]; notes: Note[] }>()
    const g = (id: string) => { if (!by.has(id)) by.set(id, { inc: [], ev: [], notes: [] }); return by.get(id)! }
    for (const i of m.incidents) if (has(`${i.equipment} ${i.symptom} ${i.action} ${i.outcome} ${i.machine_id ?? ''}`)) g(i.machine_id ?? `__${i.equipment}`).inc.push(i)
    for (const e of m.closed_events) if (has(`${e.what ?? ''} ${e.outcome ?? ''} ${e.id}`)) g(e.machine_id ?? '__other').ev.push(e)
    for (const n of m.notes) if (has(n.text)) g(n.machine_id).notes.push(n)
    return [...by.entries()].filter(([id]) => !mid || id === mid).sort((a, b) => (a[0] < b[0] ? -1 : 1))
  }, [m, query, mid])
  return (
    <section className="admin-body">
      <Err err={err} />
      <p className="note">Agent 目前<b>沒有記憶</b>：這裡是給人看的「事件記憶」——團隊資料集的歷史異常、稽核紀錄裡已結案的事件，加上主管寫的機台筆記。筆記<b>Agent 目前不讀</b>（唯讀、還沒接進提示詞）。</p>
      <NoteForm onDone={reload} />
      <div className="order-tools">
        <form className="order-search" role="search" onSubmit={(e) => e.preventDefault()}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜尋症狀、處置、結果，例如 蒸汽、溫度" aria-label="搜尋記憶" data-testid="memory-q" />
        </form>
        <select value={mid} onChange={(e) => setMid(e.target.value)} aria-label="機台">
          <option value="">全部機台</option>
          {MACHINES.map((x) => <option key={x.id} value={x.id}>{machineLabel(x.id)}</option>)}
        </select>
      </div>
      {m && !groups.length && <p className="muted">沒有符合的記憶。</p>}
      <div className="mem-groups" data-testid="memory-groups">
        {groups.map(([id, g]) => (
          <section key={id} className="panel mem-g">
            <header className="panel-h"><h2>{id.startsWith('__') ? (id === '__other' ? '其他' : id.slice(2)) : machineLabel(id)}</h2>
              <span className="muted small">歷史異常 {g.inc.length}・結案事件 {g.ev.length}・筆記 {g.notes.length}</span></header>
            <ul className="mem-list">
              {g.notes.map((n) => <li key={n.id} className="k-note"><span className="mem-k">筆記</span><span>{n.text}<small className="muted">{fmtMD(n.created_at)} {fmtHM(n.created_at)}・{n.author}</small></span></li>)}
              {g.inc.map((i) => <li key={i.id} className="k-inc"><span className="mem-k">歷史異常</span><span>{i.date}・{i.symptom} → {i.action}（停 {i.downtime_min} 分・{i.outcome}）<small className="muted">{i.id}・團隊資料集</small></span></li>)}
              {g.ev.map((e) => <li key={e.id} className="k-ev"><span className="mem-k">{STATE_LABEL[e.state as keyof typeof STATE_LABEL] ?? e.state}</span><span>{plain(e.what ?? e.id)}{e.outcome ? `：${plain(e.outcome)}` : ''}<small className="muted">{fmtMD(e.closed_at)} {fmtHM(e.closed_at)}・稽核紀錄</small></span></li>)}
            </ul>
          </section>
        ))}
      </div>
    </section>
  )
}

function NoteForm({ onDone }: { onDone: () => void }) {
  const [mid, setMid] = useState('CURE-07')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const send = async () => {
    if (!text.trim()) return
    setBusy(true); setMsg(null)
    try { await adminFetch('/api/admin/memory/notes', { method: 'POST', body: { machine_id: mid, text } }); setText(''); setMsg('已新增，並記在稽核紀錄'); onDone() }
    catch (e) { setMsg(`新增失敗：${String(e).replace(/^Error: /, '')}`) } finally { setBusy(false) }
  }
  return (
    <form className="mem-form" onSubmit={(e) => { e.preventDefault(); void send() }}>
      <select value={mid} onChange={(e) => setMid(e.target.value)} aria-label="筆記的機台">{MACHINES.map((x) => <option key={x.id} value={x.id}>{machineLabel(x.id)}</option>)}</select>
      <input value={text} onChange={(e) => setText(e.target.value)} maxLength={500} placeholder="新增機台筆記，例如：上次溫度掉是蒸汽閥卡住，先看閥" aria-label="筆記內容" data-testid="note-text" />
      <button type="submit" className="btn-approve" disabled={busy || !text.trim()}>新增筆記</button>
      {msg && <span className="muted small">{msg}</span>}
    </form>
  )
}

// ---------------------------------------------------------------- 執行紀錄
const RUN_KIND: Record<Run['kind'], string> = { event: '事件', command: '主管指令', ask: '提問', chat: '閒聊' }
function RunsPage() {
  const { data, err, reload } = useLoad<{ runs: Run[]; counts: Record<string, number> }>('/api/admin/runs?limit=300')
  const [kind, setKind] = useState<'all' | Run['kind']>('all')
  const runs = (data?.runs ?? []).filter((r) => kind === 'all' || r.kind === kind)
  return (
    <section className="admin-body">
      <Err err={err} />
      <div className="admin-row">
        <div className="seg sm" role="group" aria-label="種類">
          {(['all', 'event', 'command', 'ask', 'chat'] as const).map((k) => (
            <button key={k} className={kind === k ? 'on' : ''} aria-pressed={kind === k} onClick={() => setKind(k)}>{k === 'all' ? '全部' : RUN_KIND[k]}<small> {k === 'all' ? data?.runs.length ?? 0 : (data?.runs ?? []).filter((r) => r.kind === k).length}</small></button>
          ))}
        </div>
        <button className="btn-ghost" onClick={reload}>重新整理</button>
      </div>
      <div className="admin-table-wrap">
        <table className="admin-table runs" data-testid="runs">
          <thead><tr><th>時間</th><th>種類</th><th>內容</th><th>狀態</th><th>工具</th><th>結果</th></tr></thead>
          <tbody>{runs.map((r) => (
            <tr key={r.id}>
              <td className="muted">{fmtMD(r.ts)} {fmtHM(r.ts)}</td>
              <td>{RUN_KIND[r.kind]}</td>
              <td>{r.kind === 'event' || r.kind === 'command'
                ? <a href={`#console?event=${encodeURIComponent(r.id)}`}>{r.machine_id && r.kind === 'event' ? `${machineLabel(r.machine_id)}：` : ''}{plain(r.title ?? r.id)}</a>
                : <>{r.question}<small className="muted"> {r.id}</small></>}</td>
              <td>{r.state ? STATE_LABEL[r.state as keyof typeof STATE_LABEL] ?? r.state : r.answer ? '已回答' : '—'}</td>
              <td>{r.tools ?? '—'}</td>
              <td className="muted">{plain(r.outcome ?? r.answer ?? '')}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {!runs.length && data && <p className="muted">沒有紀錄。</p>}
    </section>
  )
}
