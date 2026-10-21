// 狀態機 (#sm, issue #37; CJ, 2026-10-20: 「新增一個 page 列出狀態機分類的實時狀態、事件偵測、訂單分類，就是列出狀態機的工作狀況」
// and 「狀態機也需要記錄每天的事件」). Two views, read-only:
//   即時看板 — header counts; category chips; one column per state with a card per run (time in state, round,
//              waiting on), live; then 事件偵測 (per line: machines watched, last reading; recent anomalies not yet events).
//   每日紀錄 — every event / task / question of a factory day (or demo loop) from GET /api/daylog, with a summary and CSV.
import { useEffect, useMemo, useState } from 'react'
import type { Anomaly, CompoundStage, Event, EventState, Line, MachineState } from '../contracts'
import { COMPOUND_STOP_C, CURATIVE_STOP_C, LINES, LINE_SHORT, machineLabel } from '../factory'
import { fmtHM, fmtHMS, fmtMD, ms } from '../format'
import { plain } from '../plain'
import { STATE_LABEL } from '../components/common'
import { STATE_CODE } from '../agent/stateGraph'
import { apiGet } from '../transport'
import { useWarRoom } from '../useWarRoom'
import { BOARD, CATEGORY_LABEL, categoryOf, enteredAt, waitingOn, type Category, type Day, type DayRun } from './runs'
import '../admin/admin.css'
import './statemachine.css'

type View = 'live' | 'day'
const CHANNEL: Record<string, string> = { temp_c: '溫度', pressure_kgf_cm2: '壓力', water_flow_lpm: '水流', steam_kgf_cm2: '蒸汽' }
const OUTCOME: Record<string, string> = { closed: '結案', safe_stop: '安全停止', open: '處理中', answered: '已回答' }
const TYPE: Record<string, string> = { event: '機台異常', command: '主管任務', plant: '工廠事件', ask: '問戰情室', main: 'NemoClaw 對話' }

export function StateMachinePage() {
  const { transport } = useWarRoom()
  const [view, setView] = useState<View>(() => (/#sm\/day/.test(location.hash) ? 'day' : 'live'))
  const go = (v: View) => { setView(v); history.replaceState(null, '', `${location.pathname}${location.search}#sm${v === 'day' ? '/day' : ''}`) }
  useEffect(() => {
    const f = () => { if (/^#sm/.test(location.hash)) setView(/#sm\/day/.test(location.hash) ? 'day' : 'live') }
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  return (
    <div className="smp" data-testid="sm-page">
      <header className="admin-h">
        <h2>狀態機</h2>
        <nav className="seg" aria-label="狀態機頁面">
          <button className={view === 'live' ? 'on' : ''} aria-pressed={view === 'live'} onClick={() => go('live')} data-testid="sm-view-live">即時看板</button>
          <button className={view === 'day' ? 'on' : ''} aria-pressed={view === 'day'} onClick={() => go('day')} data-testid="sm-view-day">每日事件紀錄</button>
        </nav>
        <span className="muted small">唯讀・狀態名稱同 warroom/contracts.py（EventState）</span>
      </header>
      {transport?.mode === 'mock' && view === 'day' ? <p className="note">每日事件紀錄來自稽核紀錄，需要連線後端。</p> : view === 'live' ? <Live /> : <DayLog />}
    </div>
  )
}

/** GET /api/daylog/current, refreshed when the events change and every 10 s. */
function useCurrentDay(): Day | null {
  const { state, transport } = useWarRoom()
  const [day, setDay] = useState<Day | null>(null)
  const sig = (state.snapshot?.events ?? []).map((e) => `${e.id}:${e.state}:${e.replans}`).join(',')
  const [tick, setTick] = useState(0)
  useEffect(() => { const t = setInterval(() => setTick((x) => x + 1), 10_000); return () => clearInterval(t) }, [])
  useEffect(() => {
    if (transport?.mode !== 'live') return
    let off = false
    apiGet<Day>('/api/daylog/current').then((d) => { if (!off) setDay(d) }).catch(() => {})
    return () => { off = true }
  }, [transport, sig, tick])
  return day
}

function usePoll<T>(path: string, every = 5000): T | null {
  const { transport } = useWarRoom()
  const [data, setData] = useState<T | null>(null)
  useEffect(() => {
    if (transport?.mode !== 'live') return
    let off = false
    const load = () => apiGet<T>(path).then((d) => { if (!off) setData(d) }).catch(() => {})
    load()
    const t = setInterval(load, every)
    return () => { off = true; clearInterval(t) }
  }, [transport, path, every])
  return data
}

// ---------------------------------------------------------------- 即時看板
function Live() {
  const { state } = useWarRoom()
  const events = useMemo(() => state.snapshot?.events ?? [], [state.snapshot?.events])
  const day = useCurrentDay()
  const runOf = useMemo(() => new Map((day?.runs ?? []).map((r) => [r.id, r])), [day])
  const todayIds = new Set(day?.runs.map((r) => r.id) ?? [])
  const tasks = state.world?.tasks?.items ?? []
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const [cat, setCat] = useState<Category | 'all'>('all')
  const [showDone, setShowDone] = useState(false)
  const asks = (day?.runs ?? []).filter((r) => r.type === 'ask')
  const cats = Object.keys(CATEGORY_LABEL) as Category[]
  const count = (c: Category) => (c === 'ask' ? asks.length : events.filter((e) => categoryOf(e) === c).length)
  const shown = events.filter((e) => cat === 'all' || categoryOf(e) === cat)
  const isToday = (e: Event) => !day || todayIds.has(e.id)
  // a state with no runs is a narrow strip on desktop and hidden on a phone; all 11 columns stay in the DOM
  const onBoard = (s: EventState) => shown.filter((e) => e.state === s && ((s !== 'closed' && s !== 'safe_stop') || isToday(e)))
  const open = events.filter((e) => e.state !== 'closed' && e.state !== 'safe_stop')
  const waitTasks = open.filter((e) => waitingOn(e, tasks) === '人員任務').length
  const head = {
    running: open.filter((e) => e.state !== 'await_approval' && waitingOn(e, tasks) !== '人員任務').length,
    approval: open.filter((e) => e.state === 'await_approval').length,
    tasks: waitTasks,
    closed: events.filter((e) => e.state === 'closed' && isToday(e)).length,
    stopped: events.filter((e) => e.state === 'safe_stop' && isToday(e)).length,
  }
  return (
    <>
      <section className="kpis sm-kpis" aria-label="狀態機重點" data-testid="sm-counts">
        <div className={`kpi ${head.running ? 'is-live' : ''}`}><span className="kpi-l">處理中</span><span className="kpi-v">{head.running}</span><span className="kpi-s">AI 規劃、工具、驗證</span></div>
        <div className={`kpi ${head.approval ? 'is-warn' : ''}`}><span className="kpi-l">等主管核准</span><span className="kpi-v">{head.approval}</span><span className="kpi-s">待核准卡</span></div>
        <div className="kpi"><span className="kpi-l">等人員任務</span><span className="kpi-v">{head.tasks}</span><span className="kpi-s">核准後派工中</span></div>
        <div className="kpi"><span className="kpi-l">今日結案</span><span className="kpi-v">{head.closed}</span><span className="kpi-s">{day ? day.label : '—'}</span></div>
        <div className={`kpi ${head.stopped ? 'is-alarm' : ''}`}><span className="kpi-l">今日安全停止</span><span className="kpi-v">{head.stopped}</span><span className="kpi-s">安全核心或連續失敗</span></div>
      </section>
      <div className="sm-cats" role="group" aria-label="分類" data-testid="sm-cats">
        <button className={cat === 'all' ? 'on' : ''} aria-pressed={cat === 'all'} onClick={() => setCat('all')}>全部<small>{events.length}</small></button>
        {cats.map((c) => <button key={c} className={cat === c ? 'on' : ''} aria-pressed={cat === c} onClick={() => setCat(c)} data-cat={c}>{CATEGORY_LABEL[c]}<small>{count(c)}</small></button>)}
        <label className="trace-open"><input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} />展開今日結案／安全停止</label>
      </div>
      {cat === 'ask' ? <AskList asks={asks} /> : (
        <div className="sm-board" data-testid="sm-board">
          {BOARD.map((s) => {
            const done = s === 'closed' || s === 'safe_stop'
            const cards = onBoard(s)
            return (
              <section key={s} className={`sm-col ${cards.length ? '' : 'empty'} ${done ? 'done' : ''}`} data-state={s} aria-label={`${STATE_LABEL[s]} ${cards.length} 件`}>
                <header><b>{STATE_LABEL[s]}</b><small>{STATE_CODE[s]}</small><span className="sm-n">{cards.length}</span></header>
                {(!done || showDone) && cards.map((e) => <Card key={e.id} ev={e} run={runOf.get(e.id)} now={now} />)}
                {done && !showDone && cards.length > 0 && <button className="btn-ghost sm-more" onClick={() => setShowDone(true)}>顯示 {cards.length} 件</button>}
              </section>
            )
          })}
          {BOARD.every((s) => !onBoard(s).length) && <p className="muted sm-none">目前沒有事件。</p>}
        </div>
      )}
      <p className="muted small">每張卡點進去是 Agent 頁的完整過程與狀態機圖。結案／安全停止只列今天（{day?.label ?? '目前這一天'}）。</p>
      <Detection events={events} today={events.filter(isToday).length} />
      <PreClassPanel />
    </>
  )
}

function Card({ ev, run, now }: { ev: Event; run: DayRun | undefined; now: number }) {
  const { state } = useWarRoom()
  const since = enteredAt(ev, run)
  const mins = Math.max(0, Math.round((now - ms(since)) / 60_000))
  const wait = waitingOn(ev, state.world?.tasks?.items ?? [])
  const cat = categoryOf(ev)
  return (
    <a className={`sm-card c-${cat} w-${wait}`} href={`#console?event=${encodeURIComponent(ev.id)}`} data-testid="sm-card" data-event={ev.id}>
      <span className="sm-card-t"><b>{ev.source === 'supervisor' ? '主管指令' : ev.source === 'plant' ? '工廠事件' : machineLabel(ev.machine_id)}</b><em className="tag">{CATEGORY_LABEL[cat]}</em></span>
      <span className="sm-card-w">{plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message)}</span>
      <span className="sm-card-m">
        <span title={`進入此狀態 ${fmtHMS(since)}`}>{mins >= 60 ? `${Math.floor(mins / 60)} h ${mins % 60} 分` : `${mins} 分`}</span>
        <span>第 {(ev.prior_rounds?.length ?? 0) + 1} 輪</span>
        {wait !== '—' && <span className="sm-wait">等{wait}</span>}
      </span>
      <small className="muted sm-id">{ev.id}</small>
    </a>
  )
}

function AskList({ asks }: { asks: DayRun[] }) {
  if (!asks.length) return <p className="muted">今天沒有人問戰情室。</p>
  return (
    <ul className="sm-asks" data-testid="sm-asks">
      {asks.map((a) => <li key={a.id}><span className="muted">{a.opened_at ? fmtHM(a.opened_at) : ''}</span><b>{a.title}</b><span className="tag">{OUTCOME[a.outcome] ?? a.outcome}</span><small className="muted">{a.id}・只查不改</small></li>)}
    </ul>
  )
}

/** 預測 column: a rising reading with an upper limit reads 「約 N 分後達上限 120°C」 (the detector's own words when its
 *  message says so, else from the trend and the compound stage's stop line), not a linear extrapolation past the limit
 *  (B 線開煉機 229.7°C（186.3–273.2）). */
function forecastText(a: Anomaly, stage: CompoundStage | undefined): string {
  const p = a.prediction
  if (!p) return '—'
  const r1 = (n: number) => Math.round(n * 10) / 10
  const said = a.message.match(/預測約\s*(\d+)\s*分後達\s*(\d+(?:\.\d+)?)°C\s*上限/)
  const stop = stage === 'curative_added' ? CURATIVE_STOP_C : stage === 'unvulcanized' ? COMPOUND_STOP_C : null
  const limit = said ? Number(said[2]) : a.channel === 'temp_c' && p.trend_per_min > 0 ? stop : null
  if (limit != null && (said || p.predicted_value > limit)) {
    if (p.now_value >= limit) return `已達上限 ${limit}°C`
    const min = said ? Number(said[1]) : Math.max(1, Math.round((limit - p.now_value) / p.trend_per_min))
    return `約 ${min} 分後達上限 ${limit}°C`
  }
  return `${r1(p.predicted_value)}（${r1(p.band_lo)}–${r1(p.band_hi)}）`
}

/** 事件偵測: the detector watches every machine without the AI; per line, how many and when the last reading came;
 *  then recent anomalies, marked when they became an event. */
function Detection({ events, today }: { events: Event[]; today: number }) {
  const machines = usePoll<MachineState[]>('/api/machines')
  const anomalies = usePoll<Anomaly[]>('/api/anomalies')
  const { state } = useWarRoom()
  const tiles = state.snapshot?.machines ?? []
  const evByAnom = new Set(events.map((e) => e.anomaly.id))
  const [open, setOpen] = useState(false) // a phone shows only the header until opened; desktop always shows the panel
  const lineRow = (ln: Line) => {
    const ms_ = (machines ?? []).filter((m) => m.line === ln)
    const last = ms_.map((m) => m.last_reading?.ts).filter((x): x is string => !!x).sort().pop()
    const bad = tiles.filter((t) => t.line === ln && t.status !== 'running' && t.status !== 'idle').length
    const n = ms_.length || tiles.filter((t) => t.line === ln).length
    return { ln, n, last, bad }
  }
  return (
    <section className={`panel sm-detect ${open ? 'open' : ''}`} aria-label="事件偵測" data-testid="sm-detect">
      <header className="panel-h"><h2>事件偵測（不經 AI，常駐監看）</h2><span className="muted small">今天開了 {today} 個事件</span>
        <button className="btn-ghost sm-fold" aria-expanded={open} onClick={() => setOpen((x) => !x)}>{open ? '收合' : '展開'}</button></header>
      <div className="sm-lines">
        {LINES.map(lineRow).map((r) => (
          <div key={r.ln} className={`sm-line ${r.bad ? 'bad' : ''}`} data-line={r.ln}>
            <b>{LINE_SHORT[r.ln]}{r.ln.length === 1 ? ' 線' : ''}</b>
            <span>監看 {r.n} 台{r.bad ? `・注意 ${r.bad}` : ''}</span>
            <small className="muted">{r.last ? `最後讀值 ${fmtHMS(r.last)}` : '等讀值…'}</small>
          </div>
        ))}
      </div>
      <h3 className="sub">最近的異常與預測</h3>
      {!anomalies?.length && <p className="muted small">沒有異常。</p>}
      {!!anomalies?.length && (
        <div className="admin-table-wrap"><table className="admin-table" data-testid="sm-anoms">
          <thead><tr><th>時間</th><th>機台</th><th>訊號</th><th>讀值／基準</th><th>預測</th><th>結果</th></tr></thead>
          <tbody>{anomalies.slice(0, 8).map((a) => (
            <tr key={a.id}>
              <td className="muted">{fmtMD(a.ts)} {fmtHMS(a.ts)}</td><td>{machineLabel(a.machine_id)}</td><td>{CHANNEL[a.channel] ?? a.channel}</td>
              <td>{Math.round(a.value * 10) / 10}／{Math.round(a.baseline * 10) / 10}</td>
              <td>{forecastText(a, machines?.find((m) => m.machine_id === a.machine_id)?.compound_stage)}</td>
              <td>{evByAnom.has(a.id) ? <span className="tag">已開事件</span> : <span className="tag warn">未成事件</span>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- 每日事件紀錄
interface DayHead { key: string; label: string; first: string; last: string; runs: number; by_outcome: Record<string, number> }

function DayLog() {
  const days = usePoll<{ days: DayHead[] }>('/api/daylog', 15000)
  const [key, setKey] = useState<string | null>(null)
  const k = key ?? days?.days[0]?.key ?? null
  const [day, setDay] = useState<Day | null>(null)
  useEffect(() => {
    if (!k) return
    let off = false
    apiGet<Day>(`/api/daylog/${encodeURIComponent(k)}`).then((d) => { if (!off) setDay(d) }).catch(() => {})
    return () => { off = true }
  }, [k, days])
  const s = day?.summary
  return (
    <section className="admin-body" data-testid="sm-daylog">
      <div className="admin-row">
        <label className="admin-check">日期
          <select value={k ?? ''} onChange={(e) => setKey(e.target.value)} aria-label="選擇日期" data-testid="day-pick">
            {(days?.days ?? []).map((d) => <option key={d.key} value={d.key}>{d.label}（{d.runs} 件）</option>)}
          </select>
        </label>
        {k && <a className="btn-ghost" href={`/api/daylog/${encodeURIComponent(k)}/csv`} download data-testid="day-csv">下載 CSV</a>}
        <span className="muted small">來源：稽核紀錄（依工廠日期；有「新的一天（循環 n）」標記時依循環分天）</span>
      </div>
      {s && (
        <div className="admin-cards" data-testid="day-summary">
          <div className="admin-card"><span className="admin-card-k">件數</span><b>{s.runs} 件</b><span>{Object.entries(s.by_type).map(([t, n]) => `${TYPE[t] ?? t} ${n}`).join('・')}</span></div>
          <div className="admin-card"><span className="admin-card-k">結果</span><b>{Object.entries(s.by_outcome).map(([o, n]) => `${OUTCOME[o] ?? o} ${n}`).join('・')}</b><span>&nbsp;</span></div>
          <div className="admin-card"><span className="admin-card-k">平均結案時間</span><b>{s.avg_close_min != null ? `${s.avg_close_min} 分` : '—'}</b><span>從開事件到結案／安全停止</span></div>
          <div className="admin-card"><span className="admin-card-k">核准</span><b>已決定 {s.approvals_decided}・待核准 {s.approvals_pending}</b><span>&nbsp;</span></div>
        </div>
      )}
      {day && (
        <div className="admin-table-wrap">
          <table className="admin-table day-t" data-testid="day-runs">
            <thead><tr><th>開始</th><th>類型</th><th>機台／內容</th><th>走過的狀態</th><th>輪</th><th>結果</th><th>核准（誰）</th><th>耗時</th></tr></thead>
            <tbody>{day.runs.map((r) => (
              <tr key={r.id}>
                <td className="muted">{r.opened_at ? `${fmtMD(r.opened_at)} ${fmtHMS(r.opened_at)}` : ''}</td>
                <td>{TYPE[r.type] ?? r.type}</td>
                <td>{r.type === 'ask' ? <>{r.title}<small className="muted"> {r.id}</small></> : <a href={`#console?event=${encodeURIComponent(r.id)}`}>{r.machine_id ? machineLabel(r.machine_id) : '—'}{r.title ? `：${plain(r.title)}` : ''}</a>}</td>
                <td className="day-path">{r.path.map((p, i) => <span key={i} className={`pp st-${p.state}`} title={fmtHMS(p.ts)}>{STATE_LABEL[p.state as EventState] ?? p.state}</span>)}</td>
                <td>{r.type === 'ask' ? '—' : r.rounds}</td>
                <td className={`o-${r.outcome}`}>{OUTCOME[r.outcome] ?? r.outcome}</td>
                <td>{r.approvals.map((a) => `${a.decision === 'approve' ? '核准' : a.decision === 'reject' ? '駁回' : a.decision}・${a.by === 'supervisor' ? '主管' : a.by}${a.via ? `（${a.via}）` : ''}`).join('；') || (r.pending_approvals ? `待核准 ${r.pending_approvals}` : '—')}</td>
                <td>{r.duration_min != null ? `${r.duration_min} 分` : '—'}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      {day && !day.runs.length && <p className="muted">這一天沒有紀錄。</p>}
    </section>
  )
}

// ---------------------------------------------------------------- 事件預分類 (issue #54)
interface PreItem extends Anomaly {
  route: { type: string; type_label: string; priority: number; priority_label: string }
  event_id: string | null; event_state: string | null
}
interface PreView { items: PreItem[]; bad_data: { machine_id: string; count: number; last_quality: string; last_at: string }[]; bad_log: { ts: string; machine_id: string; channel: string; quality: string }[] }
const QUALITY: Record<string, string> = { out_of_range: '超出範圍', jump: '突跳', stale: '數值凍結', neighbor_mismatch: '與鄰機不符', missing: '缺值' }

/** CJ, 2026-10-20: 「我都沒有看到狀態機頁面用 python 去做事件預分類的部分」. What the Python detector decided before any
 *  LLM ran: which rule fired, the signals and thresholds, the validation the readings passed, the forecast, and where
 *  the main agent routes it; plus readings the validation layer rejected (they never wake the AI). */
function PreClassPanel() {
  const d = usePoll<PreView>('/api/preclass', 5000)
  return (
    <section className="panel sm-pre" aria-label="事件預分類" data-testid="sm-preclass">
      <header className="panel-h"><h2>事件預分類（Python 偵測器，LLM 之前）</h2><span className="muted small">規則 → 驗證 → 預測 → 分派</span></header>
      {!d?.items.length && <p className="muted small">還沒有異常。偵測器每秒檢查 28 台機台；觸發時這裡會列出是哪條規則、門檻、驗證結果與預測，再交給NemoClaw 分派。</p>}
      <ol className="pre-list">
        {(d?.items ?? []).slice(0, 8).map((a) => (
          <li key={a.id} className={`pre sev-${a.severity}`} data-testid="preclass-item">
            <div className="pre-h">
              <span className="muted">{fmtMD(a.ts)} {fmtHMS(a.ts)}</span><b>{machineLabel(a.machine_id)}</b>
              <span className="tag">{a.preclass?.label ?? a.kind}</span>
              <span className={`tag ${a.severity === 'critical' ? 'rush' : ''}`}>{a.severity === 'critical' ? '嚴重' : a.severity === 'warn' ? '注意' : '資訊'}</span>
              <span className="pre-route" data-testid="preclass-route">→ NemoClaw 派給 <b>{a.route.type_label}</b>（優先 {a.route.priority_label}）</span>
              {a.event_id ? <a className="panel-link" href={`#console?event=${encodeURIComponent(a.event_id)}`}>{a.event_state ? STATE_LABEL[a.event_state as EventState] : ''} →</a> : <span className="muted small">併入既有事件</span>}
            </div>
            <div className="pre-grid">
              <div><h4>規則</h4><code>{a.preclass?.rule ?? a.kind}</code><ul>{(a.preclass?.checks ?? []).map((c, i) => <li key={i}>{c}</li>)}</ul></div>
              <div><h4>訊號</h4><p>{CHANNEL[a.channel] ?? a.channel} {a.value} ／ 基準 {a.baseline}{a.preclass ? `（雜訊 σ ${a.preclass.sigma}）` : ''}</p>{a.preclass?.limit != null && <p className="muted small">上限 {a.preclass.limit}°C</p>}</div>
              <div><h4>驗證</h4><ul>{Object.entries(a.preclass?.validation ?? {}).map(([k, v]) => <li key={k}>{VAL_LABEL[k] ?? k}：{QUALITY[v] ?? (v === 'ok' ? '通過' : v)}</li>)}</ul></div>
              <div><h4>預測</h4>{a.preclass?.forecast ? <p>{a.preclass.forecast.horizon_min} 分後 {a.preclass.forecast.predicted}°C（{a.preclass.forecast.band[0]}–{a.preclass.forecast.band[1]}）<br /><small className="muted">模型 {FIT[a.preclass.forecast.model] ?? a.preclass.forecast.model}・斜率 {a.preclass.forecast.trend_per_min}°C/分</small></p> : <p className="muted">—</p>}</div>
            </div>
          </li>
        ))}
      </ol>
      <h3 className="sub">擋下的壞資料（不喚醒 AI）</h3>
      {!d?.bad_log.length && <p className="muted small">沒有。</p>}
      {!!d?.bad_log.length && (
        <ul className="pre-bad" data-testid="preclass-bad">
          {d.bad_log.slice(0, 8).map((b, i) => <li key={i}><span className="muted">{fmtHMS(b.ts)}</span> {machineLabel(b.machine_id)}・{CHANNEL[b.channel] ?? b.channel}・<b>{QUALITY[b.quality] ?? b.quality}</b></li>)}
        </ul>
      )}
    </section>
  )
}
const VAL_LABEL: Record<string, string> = { range: '範圍', jump: '突跳', stale: '凍結', quality: '最近讀值', steam_model: '蒸汽推算', neighbours: '鄰機' }
const FIT: Record<string, string> = { constant: '常數', 'linear-trend': '線性趨勢', 'exp-approach': '指數逼近' }
