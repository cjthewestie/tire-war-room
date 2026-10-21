// 訂單 (UX brief moment 「查一張單」; CJ, 2026-10-20: 「看訂單狀況這個我很需要」): every order with its status,
// progress, due date, at-risk orders first, rush orders marked (a rush order is not an at-risk order), and a search box.
// Picking a row opens its card (where its tyres are now, what blocks it; print / demo-edit buttons). Data: GET /api/world
// orders + the active schedule's jobs per order (A 線投料, 貼合, 入模, 出模); see orderModel.ts. Filters (search, status,
// customer, due day from 未來 7 日交貨計畫) and the sort all work on that same list -- no second copy of the orders.
import { useMemo, useState } from 'react'
import { SIZE_LABEL } from '../factory'
import { fmtHM, fmtMD, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { OrderCard } from './OrderCard'
import { DueWeekPlan } from './DueWeekPlan'
import { dueKey } from './dueWeek'
import { successOf, type Success } from '../agent/tickerItems'
import { RISK_TEXT, byRisk, orderView, warningsFor, type OrderSuccess, type OrderView } from './orderModel'

type Show = 'all' | 'risk' | 'rush' | 'open' | 'done' | 'shipped'
const SHOW: { id: Show; label: string }[] = [
  { id: 'all', label: '全部' }, { id: 'risk', label: '有風險' }, { id: 'rush', label: '急單' }, { id: 'open', label: '進行中' }, { id: 'done', label: '已完成' }, { id: 'shipped', label: '已出貨' },
]
type Sort = 'risk' | 'due-asc' | 'due-desc'
const SORT: { id: Sort; label: string }[] = [{ id: 'risk', label: '風險優先' }, { id: 'due-asc', label: '交期（近→遠）' }, { id: 'due-desc', label: '交期（遠→近）' }]
const inShowOf = (v: OrderView, s: Show) => s === 'all' || (s === 'risk' ? v.risk === 'late' || v.risk === 'risk' : s === 'rush' ? v.o.rush
  : s === 'open' ? v.o.status === '待排程' || v.o.status === '生產中' : s === 'done' ? v.risk === 'complete' : v.risk === 'done')
const DAY = 24 * 3_600_000

export function OrdersTab() {
  const { state } = useWarRoom()
  const w = state.world
  const jobs = useMemo(() => state.schedule?.schedule.jobs ?? [], [state.schedule])
  const warnings = useMemo(() => state.schedule?.schedule.warnings ?? [], [state.schedule])
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const minute = isFinite(now) ? Math.floor(now / 60_000) : 0
  // success moments from the world feed (issue #36 order_completed / shipment_departed; main's order_done)
  const succ = useMemo(() => {
    const by = new Map<string, Partial<OrderSuccess>>()
    for (const s of [...(w?.events ?? [])].reverse().map(successOf).filter((x): x is Success => !!x)) {
      for (const id of s.orderIds) {
        const cur = by.get(id) ?? {}
        by.set(id, s.kind === 'done' ? { ...cur, doneAt: s.ts, doneOnTime: s.onTime } : { ...cur, shipAt: s.ts, shipOnTime: s.onTime })
      }
    }
    return by
  }, [w?.events])
  const views = useMemo(() => (w?.orders.items ?? []).map((o) => orderView(o, jobs, minute * 60_000, warnings, w?.goods?.orders.find((g) => g.order_id === o.id), succ.get(o.id))).sort(byRisk),
    [w?.orders.items, w?.goods, jobs, warnings, minute, succ])
  const customers = useMemo(() => [...new Set(views.map((v) => v.o.customer))].sort((a, b) => a.localeCompare(b, 'zh-TW')), [views])
  const [q, setQ] = useState('')
  const [show, setShow] = useState<Show>('all')
  const [cust, setCust] = useState('')
  const [sort, setSort] = useState<Sort>('risk')
  const [picked, setPicked] = useState<string | null>(null)
  const [day, setDay] = useState<string | null>(null)   // 未來 7 日交貨計畫: picked due day (factory date) filters the table
  const query = q.trim().toLowerCase().replace(/^#/, '')
  const match = (v: OrderView) => !query || `${v.o.id} ${v.o.customer} ${SIZE_LABEL[v.o.size]}`.toLowerCase().replace(/#/g, '').includes(query)
  const filtered = views.filter((v) => match(v) && inShowOf(v, show) && (!cust || v.o.customer === cust) && (!day || dueKey(v.o.due) === day))
  const list = sort === 'risk' ? filtered : [...filtered].sort((a, b) => (sort === 'due-asc' ? a.due - b.due : b.due - a.due))
  const count = (s: Show) => views.filter((v) => inShowOf(v, s)).length
  const sel = views.find((v) => v.o.id === picked) ?? (query ? list[0] : null) ?? null
  if (!w) return <div className="loading">讀取訂單中…（訂單資料來自後端；瀏覽器模擬模式沒有這部分）</div>
  const atRisk = views.filter((v) => v.risk === 'late' || v.risk === 'risk')
  const dueSoon = views.filter((v) => v.o.status !== '已出貨' && v.due - now < 3 * DAY)
  const narrowed = !!(query || show !== 'all' || cust || day)
  const clearAll = () => { setQ(''); setShow('all'); setCust(''); setDay(null); setPicked(null) }
  return (
    <div className="orders-tab" data-testid="orders-tab">
      <DueWeekPlan views={views} now={now} day={day} onPick={(k) => { setDay(k); setPicked(null) }} />
      <section className="kpis" aria-label="訂單重點">
        <div className="kpi"><span className="kpi-l">全部訂單</span><span className="kpi-v">{views.length}<small> 張</small></span>
          <span className="kpi-s">進行 {count('open')}・完成 {count('done')}・出貨 {count('shipped')}</span></div>
        <div className={`kpi ${atRisk.length ? 'is-alarm' : ''}`}><span className="kpi-l">有風險</span><span className="kpi-v" data-testid="kpi-risk">{atRisk.length}<small> 張</small></span>
          <span className="kpi-s">{atRisk.length ? `${atRisk.slice(0, 2).map((v) => v.o.id).join('、')}${atRisk.length > 2 ? ' 等' : ''}` : '全部預估準時'}</span></div>
        <div className={`kpi ${w.orders.rush_open ? 'is-warn' : ''}`}><span className="kpi-l">急單</span><span className="kpi-v">{count('rush')}<small> 張</small></span>
          <span className="kpi-s">未完成 {w.orders.rush_open}</span></div>
        <div className="kpi"><span className="kpi-l">3 天內到期</span><span className="kpi-v">{dueSoon.length}<small> 張</small></span>
          <span className="kpi-s">今日新單 {w.orders.new_today}</span></div>
      </section>
      <div className="order-tools">
        <form className="order-search" role="search" onSubmit={(e) => { e.preventDefault(); if (list[0]) setPicked(list[0].o.id) }}>
          <span className="os-field">
            <input value={q} onChange={(e) => { setQ(e.target.value); setPicked(null) }} placeholder="搜尋訂單編號、客戶或胎型" aria-label="搜尋訂單" data-testid="order-search" />
            {q && <button type="button" className="os-clear" aria-label="清除搜尋" onClick={() => { setQ(''); setPicked(null) }}>×</button>}
          </span>
          <button type="submit" className="btn-approve os-go">
            <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="8.5" cy="8.5" r="5.5" /><path d="M13 13l4 4" /></svg>搜尋
          </button>
        </form>
        <div className="seg sm" role="group" aria-label="篩選訂單">
          {SHOW.map((s) => <button key={s.id} className={show === s.id ? 'on' : ''} aria-pressed={show === s.id} onClick={() => setShow(s.id)}>{s.label}<small> {count(s.id)}</small></button>)}
        </div>
        <select value={cust} onChange={(e) => { setCust(e.target.value); setPicked(null) }} aria-label="依客戶篩選" data-testid="order-customer">
          <option value="">全部客戶</option>
          {customers.map((c) => <option key={c} value={c}>{c}（{views.filter((v) => v.o.customer === c).length} 張）</option>)}
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label="排序" data-testid="order-sort">
          {SORT.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>
      </div>
      {sel && <OrderCard o={sel.o} jobs={jobs} now={now} warnings={warningsFor(sel.o.id, warnings)} actions />}
      <section className="panel order-table" aria-label="訂單清單">
        <header className="panel-h"><h2>{narrowed ? `符合的訂單（${list.length}）` : `全部訂單（${views.length}）`}</h2><span className="muted small">{sort === 'risk' ? '有風險的排最前' : '依交期排序'}・點一列看明細</span></header>
        {day && <p className="dw-filter" data-testid="due-filter">日期篩選：<b>{fmtMD(`${day}T12:00:00+08:00`)}</b> 應交貨
          <button type="button" className="btn-ghost" onClick={() => { setDay(null); setPicked(null) }}>清除日期篩選</button></p>}
        {!list.length && <p className="note">{query ? `找不到「${q}」。請確認訂單號，` : '沒有符合目前條件的訂單。'}<button type="button" className="btn-ghost" onClick={clearAll}>清除所有條件</button></p>}
        <div className="ot-head" aria-hidden><span>訂單／客戶</span><span>產品／數量</span><span>生產進度</span><span>交期</span><span>狀態</span><span /></div>
        <ul className="ot-list" role="list" data-testid="order-list">
          {list.map((v) => {
            const pct = v.o.qty ? Math.min(100, Math.round((100 * v.o.done) / v.o.qty)) : 0
            return (
              <li key={v.o.id}>
                <button className={`ot-row r-${v.risk} ${sel?.o.id === v.o.id ? 'on' : ''}`} onClick={() => setPicked(sel?.o.id === v.o.id ? null : v.o.id)} aria-expanded={sel?.o.id === v.o.id} data-order={v.o.id}>
                  <span className="ot-id"><b>{v.o.id}</b>{v.o.rush && <em className="tag rush">急單</em>}<small className="muted">{v.o.customer}</small></span>
                  <span className="ot-prod">{SIZE_LABEL[v.o.size]}<small className="muted">{v.o.qty} 條</small></span>
                  <span className="ot-prog" aria-label={`已完成 ${v.o.done} / ${v.o.qty}`}><span className="ot-pv">{v.o.done}／{v.o.qty}</span><i className="ot-bar"><b style={{ width: `${pct}%` }} /></i></span>
                  <span className={`ot-due ${v.risk === 'late' ? 'late' : ''}`}>{fmtMD(v.o.due)}</span>
                  <span className="ot-st">{v.success ? <span className="ok-mark" aria-hidden>✓</span> : <i className={`risk-dot r-${v.risk}`} aria-hidden />}{RISK_TEXT[v.risk]}{v.lateH != null && <small> {v.lateH} h</small>}<small className="muted">{v.o.status}</small></span>
                  <span className="ot-go" aria-hidden>›</span>
                  {v.why.length > 0 && !v.success && <span className="ot-why">{v.why[0]}</span>}
                  {v.success && <span className="ot-why ok" data-testid="order-success">{successLine(v)}</span>}
                </button>
              </li>
            )
          })}
        </ul>
      </section>
    </div>
  )
}

/** 「✓ 10/02 08:30 出貨・準時（交期 10/02）」: when it finished / shipped and whether that was by the due day. */
function successLine(v: OrderView): string {
  const s = v.success!
  const when = (t: string | null) => (t ? `${fmtMD(t)} ${fmtHM(t)}` : '')
  const ok = (b: boolean | null) => (b == null ? '' : b ? '・準時' : '・逾期')
  const parts: string[] = []
  if (s.doneAt) parts.push(`${when(s.doneAt)} 全數完成${ok(s.doneOnTime)}`)
  else if (v.risk === 'complete') parts.push(`${v.o.qty} 條全數完成`)
  if (v.risk === 'done') parts.push(s.shipAt ? `${when(s.shipAt)} 出貨${ok(s.shipOnTime)}` : '已出貨')
  return `✓ ${parts.join('；')}（交期 ${fmtMD(v.o.due)}）`
}
