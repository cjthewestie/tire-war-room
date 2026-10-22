// 後台 效能與追蹤 (issue #199). CJ, 2026-10-22: 「trace and load 測試的一些數據要放在後台」.
// 實測數據: the page-9/10 metrics from the result files in bench/results/ (GET /api/admin/bench, warroom/api/admin_perf.py),
// one row per metric with a DGX Spark and a Mac cell — measured, PASS / FAIL / 未量, date, source file. Nothing typed by hand.
// 即時追蹤: the running app's own model calls, tool calls and model-queue waits from the audit log (GET /api/admin/trace),
// this factory day or the newest 200 calls; refreshed every 10 s. 跑一次負載測試: bench.concurrent in the background.
import { useEffect, useState } from 'react'
import { fmtHM, fmtMD } from '../format'
import { machineLabel } from '../factory'
import { adminFetch } from '../transport'
import { Err } from './adminCommon'
import { useLoad } from './adminData'
import './perf.css'

type Verdict = 'PASS' | 'FAIL' | '未量' | '參考' | '未判定' | '過期'
interface Cell { measured: number | string | null; verdict: Verdict; date: string | null; source: string; model: string | null; code: string | null; note: string; detail?: string }
interface BenchRow { key: string; label: string; unit: string; target: string; cells: Record<string, Cell | null> }
interface ModelRun { run: string; host: string; date: string | null; source: string; main_calls: number | null; main_p50_s: number | null; main_in_p50: number | null; commander_calls: number | null; commander_ttft_s: number | null; commander_tps: number | null; commander_total_s: number | null }
interface LoadJob { state: 'idle' | 'running' | 'done' | 'failed'; elapsed_s: number | null; error: string | null; file: string | null; verdicts: { name: string; measured: number | string | null; target: string; verdict: Verdict }[]; by: string | null }
interface Bench { rows: BenchRow[]; counts: Record<string, Record<Verdict, number>>; models: ModelRun[]; files: number; summary: string; targets_from: string; note: string; load: LoadJob }

interface Stat { n: number; p50: number | null; p90: number | null; max: number | null }
interface Phase { key: string; label: string; calls: number; errors: number; skipped: number; ms: Stat; ttft_ms: Stat; tokens_in: number; tokens_out: number; in_p50: number | null; out_p50: number | null }
interface Call { seq: number; ts: string; label: string; ms: number | null; ttft_ms: number | null; tokens_in: number | null; tokens_out: number | null; model: string | null; event_id: string | null; session: string | null; tool_calls: number | null; error: string | null; skipped: boolean }
interface QItem { who: string; label: string; event_id: string | null; priority_label: string; since: string; state: string }
interface Trace {
  scope: 'day' | 'recent'; day: string | null; limit: number
  totals: { calls: number; errors: number; tokens_in: number; tokens_out: number; model_s: number; ms: Stat; first_ts: string | null; last_ts: string | null; tool_calls: number }
  phases: Phase[]; tools: ({ tool: string; errors: number } & Stat)[]; last: Call[]
  queue: Stat & { over_1s: number; by_who: ({ who: string } & Stat)[] }
  now: { queue: { slots: number; running: QItem[]; waiting: QItem[] } | null; open_events: number; open: { id: string; machine_id: string; state: string }[]; factory_time: string; model: string; llm_mode: string }
}

const HOSTS = ['DGX Spark', 'Mac'] as const
const V_CLASS: Record<Verdict, string> = { PASS: 'ok', FAIL: 'bad', 未量: 'none', 參考: 'info', 未判定: 'warn', 過期: 'warn' }
const num = (v: number | null | undefined, d = 2) => (v == null ? '—' : v.toLocaleString('zh-TW', { maximumFractionDigits: d }))
const ms = (v: number | null | undefined) => (v == null ? '—' : v < 1 ? '<1 ms' : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`)
const tok = (v: number | null | undefined) => (v == null ? '—' : v >= 10000 ? `${(v / 1000).toFixed(1)}k` : String(Math.round(v)))
const fileName = (p: string) => p.split('/').pop() ?? p
const shown = (c: Cell, unit: string) => (c.measured == null ? '—' : typeof c.measured === 'number' ? `${num(c.measured, c.measured >= 100 ? 1 : 2)}${unit && unit !== '次' && unit !== '輪' ? ` ${unit}` : ''}` : String(c.measured))

export function PerfPage() {
  return (
    <section className="admin-body perf" data-testid="perf-page">
      <BenchPart />
      <TracePart />
    </section>
  )
}

// ---------------------------------------------------------------- A. 實測數據
function BenchPart() {
  const { data: b, err, reload } = useLoad<Bench>('/api/admin/bench')
  return (
    <section className="perf-part" aria-labelledby="perf-bench-h">
      <header className="perf-h"><h3 id="perf-bench-h">實測數據（bench）</h3>
        <span className="muted small">{b ? `${b.files} 個結果檔・目標取自 ${b.targets_from}` : ''}</span></header>
      <Err err={err} />
      {b && (
        <>
          <p className="note">{b.note}</p>
          <div className="admin-cards">
            {HOSTS.map((h) => {
              const c = b.counts[h]
              return (
                <div key={h} className={`admin-card t-${c.FAIL ? 'warn' : 'ok'}`} data-testid={`perf-count-${h === 'Mac' ? 'mac' : 'spark'}`}>
                  <span className="admin-card-k">{h}{h === 'Mac' ? '（代跑）' : '（正式機）'}</span>
                  <b className="perf-num">PASS {c.PASS}・FAIL {c.FAIL}・未量 {c['未量']}</b>
                  <span>參考 {c['參考']}{c['未判定'] ? `・未判定 ${c['未判定']}` : ''}{c['過期'] ? `・過期 ${c['過期']}` : ''}</span>
                </div>
              )
            })}
          </div>
          <div className="admin-table-wrap">
            <table className="admin-table perf-table perf-bench" data-testid="perf-bench">
              <thead><tr><th>指標</th><th>目標</th>{HOSTS.map((h) => <th key={h}>{h}</th>)}</tr></thead>
              <tbody>{b.rows.map((r) => (
                <tr key={r.key} data-key={r.key}>
                  <td className="perf-metric"><b>{r.label}</b></td>
                  <td className="muted" data-label="目標">{r.target}</td>
                  {HOSTS.map((h) => <BenchCell key={h} host={h} c={r.cells[h]} unit={r.unit} />)}
                </tr>
              ))}</tbody>
            </table>
          </div>
          {b.models.length > 0 && <Models rows={b.models} />}
          <LoadButton initial={b.load} onDone={reload} />
        </>
      )}
    </section>
  )
}

function BenchCell({ host, c, unit }: { host: string; c: Cell | null; unit: string }) {
  if (!c) return <td data-label={host} className="perf-cell"><span className="perf-v v-none">未量</span><small className="muted">沒有結果檔</small></td>
  return (
    <td data-label={host} className="perf-cell" title={[c.source, c.model, c.code && `code ${c.code}`, c.detail, c.note].filter(Boolean).join('\n')}>
      <span className="perf-val">{shown(c, unit)}</span>
      <span className={`perf-v v-${V_CLASS[c.verdict]}`}>{c.verdict}</span>
      <small className="muted">{c.date ? c.date.slice(5).replace('-', '/') : '—'}・<code>{fileName(c.source)}</code></small>
      {c.note && <small className="perf-note">{c.note}</small>}
    </td>
  )
}

function Models({ rows }: { rows: ModelRun[] }) {
  return (
    <details className="admin-team perf-models" open>
      <summary>同一套主 agent，換模型（DGX Spark，app 實際的 prompt）</summary>
      <div className="admin-table-wrap">
        <table className="admin-table perf-table" data-testid="perf-models">
          <thead><tr><th>跑法</th><th>主 agent 呼叫</th><th>每次 p50</th><th>輸入 tok p50</th><th>指揮官首字</th><th>指揮官解碼</th><th>指揮官整次</th><th>來源</th></tr></thead>
          <tbody>{rows.map((m) => (
            <tr key={`${m.source}-${m.run}`}>
              <td><b>{m.run}</b></td><td data-label="呼叫">{m.main_calls ?? '—'}</td><td data-label="每次 p50">{m.main_p50_s == null ? '—' : `${num(m.main_p50_s)} s`}</td>
              <td data-label="輸入 tok">{tok(m.main_in_p50)}</td><td data-label="指揮官首字">{m.commander_ttft_s == null ? '—' : `${num(m.commander_ttft_s)} s`}</td>
              <td data-label="解碼">{m.commander_tps == null ? '—' : `${num(m.commander_tps, 1)} tok/s`}</td><td data-label="整次">{m.commander_total_s == null ? '—' : `${num(m.commander_total_s, 1)} s`}</td>
              <td className="muted" data-label="來源">{m.date?.slice(5).replace('-', '/')}・<code>{m.source.split('/').slice(-2).join('/')}</code></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </details>
  )
}

function LoadButton({ initial, onDone }: { initial: LoadJob; onDone: () => void }) {
  const [job, setJob] = useState<LoadJob>(initial)
  const [msg, setMsg] = useState<string | null>(null)
  const running = job.state === 'running'
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => {
      adminFetch<LoadJob>('/api/admin/bench/load').then((j) => { setJob(j); if (j.state !== 'running') onDone() }).catch(() => {})
    }, 2000)
    return () => clearInterval(t)
  }, [running, onDone])
  const start = async () => {
    if (!window.confirm('跑一次負載測試？\n3 件異常同時打本機模型（bench.concurrent，3 輪），約 1–2 分鐘。期間戰情室的 AI 回應會變慢；不會動到機台或排程。結果存到 bench/results/<日期>-spark-load/，並記在稽核紀錄。')) return
    setMsg(null)
    try { setJob(await adminFetch<LoadJob>('/api/admin/bench/load', { method: 'POST' })) } catch (e) { setMsg(String(e).replace(/^Error: /, '').replace(/^409\s*/, '')) }
  }
  return (
    <div className="perf-load" data-testid="perf-load">
      <button className="btn-ghost" onClick={() => void start()} disabled={running} data-testid="perf-load-start">{running ? '負載測試中…' : '跑一次負載測試'}</button>
      <span className="muted small">3 件同時打本機模型；模型佇列有任務時不能跑</span>
      {msg && <span className="warn small" role="alert">不能跑：{msg}</span>}
      {job.state !== 'idle' && (
        <span className={`admin-job s-${job.state}`} role="status" aria-live="polite">
          {running && <i className="admin-prog" aria-hidden />}
          {running ? `跑測試中（${job.elapsed_s ?? 0} 秒）` : job.state === 'done'
            ? `完成（${job.elapsed_s} 秒）：${job.verdicts.map((v) => `${v.name === 'concurrent_3_worst_alert' ? '最慢一件' : v.name === 'concurrent_batched' ? '批次' : v.name} ${typeof v.measured === 'number' ? `${num(v.measured)} s` : v.measured ?? '—'} ${v.verdict}`).join('・')}・${job.file ? fileName(job.file) : ''}`
            : `失敗：${job.error}`}
        </span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- B. 即時追蹤
function TracePart() {
  const [scope, setScope] = useState<'day' | 'recent'>('day')
  const [tick, setTick] = useState(0)
  const { data: t, err } = useLoad<Trace>(`/api/admin/trace?scope=${scope}&limit=200&t=${tick}`)
  useEffect(() => {
    const id = setInterval(() => setTick((x) => x + 1), 10_000)
    return () => clearInterval(id)
  }, [])
  const q = t?.now.queue
  return (
    <section className="perf-part" aria-labelledby="perf-trace-h">
      <header className="perf-h"><h3 id="perf-trace-h">即時追蹤（這台戰情室的實際呼叫）</h3>
        <div className="seg sm" role="group" aria-label="範圍">
          <button className={scope === 'day' ? 'on' : ''} aria-pressed={scope === 'day'} onClick={() => setScope('day')}>今天（工廠日{t?.day ? ` ${t.day.slice(5).replace('-', '/')}` : ''}）</button>
          <button className={scope === 'recent' ? 'on' : ''} aria-pressed={scope === 'recent'} onClick={() => setScope('recent')}>最近 200 次</button>
        </div>
        <span className="muted small">每 10 秒更新・來源：稽核紀錄 llm_call／tool_result／模型佇列</span>
      </header>
      <Err err={err} />
      {t && (
        <>
          <div className="admin-cards" data-testid="perf-trace-cards">
            <div className="admin-card t-muted"><span className="admin-card-k">模型呼叫</span><b className="perf-num">{t.totals.calls} 次{t.totals.errors ? `・失敗 ${t.totals.errors}` : ''}</b><span>{t.now.model}・共 {num(t.totals.model_s, 1)} 秒</span></div>
            <div className="admin-card t-muted"><span className="admin-card-k">每次耗時</span><b className="perf-num">p50 {ms(t.totals.ms.p50)}・p90 {ms(t.totals.ms.p90)}</b><span>最慢 {ms(t.totals.ms.max)}</span></div>
            <div className="admin-card t-muted"><span className="admin-card-k">Token</span><b className="perf-num">輸入 {tok(t.totals.tokens_in)}・輸出 {tok(t.totals.tokens_out)}</b><span>工具呼叫 {t.totals.tool_calls} 次</span></div>
            <div className={`admin-card t-${q && q.waiting.length ? 'warn' : 'ok'}`} data-testid="perf-now"><span className="admin-card-k">現在的負載</span>
              <b className="perf-num">同時事件 {t.now.open_events}・模型{q ? (q.running.length ? `忙（${q.running.length}/${q.slots}）` : '閒') : ' —'}{q?.waiting.length ? `・排隊 ${q.waiting.length}` : ''}</b>
              <span>{q && (q.running.length || q.waiting.length) ? [...q.running, ...q.waiting].map((x) => `${x.state === 'running' ? '▶' : '⏳'} ${x.label || x.who}`).join('、') : '佇列是空的'}</span></div>
          </div>
          {!t.totals.calls && <p className="muted">{t.scope === 'day' ? '今天（工廠日）還沒有模型呼叫；可切到「最近 200 次」。' : '稽核紀錄裡還沒有模型呼叫。'}</p>}
          {t.phases.length > 0 && (
            <div className="admin-table-wrap">
              <table className="admin-table perf-table" data-testid="perf-phases">
                <thead><tr><th>階段</th><th>次數</th><th>p50</th><th>p90</th><th>最慢</th><th>首字 p50</th><th>輸入 tok</th><th>輸出 tok</th><th>失敗</th></tr></thead>
                <tbody>{t.phases.map((p) => (
                  <tr key={p.key}>
                    <td><b>{p.label}</b>{p.skipped ? <small className="muted"> 略過 {p.skipped}</small> : null}</td>
                    <td data-label="次數">{p.calls}</td><td data-label="p50">{ms(p.ms.p50)}</td><td data-label="p90">{ms(p.ms.p90)}</td><td data-label="最慢">{ms(p.ms.max)}</td>
                    <td data-label="首字 p50">{ms(p.ttft_ms.p50)}</td><td data-label="輸入 tok">{tok(p.tokens_in)}<small className="muted"> p50 {tok(p.in_p50)}</small></td>
                    <td data-label="輸出 tok">{tok(p.tokens_out)}<small className="muted"> p50 {tok(p.out_p50)}</small></td><td data-label="失敗" className={p.errors ? 'warn' : ''}>{p.errors}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <div className="perf-two">
            <section aria-label="工具耗時">
              <h4 className="perf-sub">工具耗時</h4>
              <div className="admin-table-wrap">
                <table className="admin-table perf-table" data-testid="perf-tools">
                  <thead><tr><th>工具</th><th>次數</th><th>p50</th><th>p90</th><th>最慢</th><th>失敗</th></tr></thead>
                  <tbody>{t.tools.map((x) => (
                    <tr key={x.tool}><td><code>{x.tool}</code></td><td data-label="次數">{x.n}</td><td data-label="p50">{ms(x.p50)}</td><td data-label="p90">{ms(x.p90)}</td><td data-label="最慢">{ms(x.max)}</td><td data-label="失敗" className={x.errors ? 'warn' : ''}>{x.errors}</td></tr>
                  ))}</tbody>
                </table>
              </div>
              {!t.tools.length && <p className="muted small">沒有工具呼叫。</p>}
            </section>
            <section aria-label="負載">
              <h4 className="perf-sub">負載：模型佇列等待</h4>
              <p className="perf-q perf-num" data-testid="perf-queue">{t.queue.n} 次排隊・p50 {ms(t.queue.p50)}・p90 {ms(t.queue.p90)}・最久 {ms(t.queue.max)}・超過 1 秒 {t.queue.over_1s} 次</p>
              <div className="admin-table-wrap">
                <table className="admin-table perf-table">
                  <thead><tr><th>誰在等</th><th>次數</th><th>p50</th><th>p90</th><th>最久</th></tr></thead>
                  <tbody>{t.queue.by_who.map((w) => <tr key={w.who}><td>{w.who}</td><td data-label="次數">{w.n}</td><td data-label="p50">{ms(w.p50)}</td><td data-label="p90">{ms(w.p90)}</td><td data-label="最久">{ms(w.max)}</td></tr>)}</tbody>
                </table>
              </div>
              {t.now.open.length > 0 && <p className="muted small">進行中：{t.now.open.map((e) => `${e.machine_id && e.machine_id !== 'PLANT' ? machineLabel(e.machine_id) : e.id}（${e.state}）`).join('、')}</p>}
            </section>
          </div>
          <h4 className="perf-sub">最近 {t.last.length} 次模型呼叫</h4>
          <div className="admin-table-wrap">
            <table className="admin-table perf-table perf-last" data-testid="perf-last">
              <thead><tr><th>時間</th><th>階段</th><th>耗時</th><th>首字</th><th>tok 入／出</th><th>任務</th><th>備註</th></tr></thead>
              <tbody>{t.last.map((c) => (
                <tr key={c.seq}>
                  <td className="muted">{fmtMD(c.ts)} {fmtHM(c.ts)}</td><td>{c.label}</td><td data-label="耗時">{c.skipped ? '略過' : ms(c.ms)}</td><td data-label="首字">{ms(c.ttft_ms)}</td>
                  <td data-label="tok">{tok(c.tokens_in)}／{tok(c.tokens_out)}</td>
                  <td data-label="任務">{c.event_id ? <a href={`#console?event=${encodeURIComponent(c.event_id)}`} title={c.event_id}>{c.session ?? c.event_id}</a> : <span className="muted">—</span>}</td>
                  <td className={c.error ? 'warn' : 'muted'} data-label="備註">{c.error ?? (c.tool_calls ? `叫工具 ${c.tool_calls} 個` : '')}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}
