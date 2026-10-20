// 訂單 (UX brief moment 「查一張單」; CJ, 2026-10-20: 「看訂單狀況這個我很需要」): every order with its status,
// per-stage progress, ETA against the due date, at-risk orders first, rush orders marked, and a search box.
// Picking a row opens its card (where its tyres are now, what blocks it). Data: GET /api/world orders + the
// active schedule's jobs per order (A 線投料, 貼合, 入模, 出模); see orderModel.ts.
import { useMemo, useState } from 'react'
import { SIZE_LABEL } from '../factory'
import { fmtHM, fmtMD, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { OrderCard } from './OrderCard'
import { RISK_TEXT, byRisk, orderView, warningsFor, type OrderView } from './orderModel'

type Show = 'all' | 'risk' | 'rush' | 'open' | 'shipped'
const SHOW: { id: Show; label: string }[] = [
  { id: 'all', label: '全部' }, { id: 'risk', label: '有風險' }, { id: 'rush', label: '急單' }, { id: 'open', label: '未完成' }, { id: 'shipped', label: '已出貨' },
]
const DAY = 24 * 3_600_000

export function OrdersTab() {
  const { state } = useWarRoom()
  const w = state.world
  const jobs = useMemo(() => state.schedule?.schedule.jobs ?? [], [state.schedule])
  const warnings = useMemo(() => state.schedule?.schedule.warnings ?? [], [state.schedule])
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const minute = isFinite(now) ? Math.floor(now / 60_000) : 0
  const views = useMemo(() => (w?.orders.items ?? []).map((o) => orderView(o, jobs, minute * 60_000, warnings, w?.goods?.orders.find((g) => g.order_id === o.id))).sort(byRisk),
    [w?.orders.items, w?.goods, jobs, warnings, minute])
  const [q, setQ] = useState('')
  const [show, setShow] = useState<Show>('all')
  const [picked, setPicked] = useState<string | null>(null)
  const query = q.trim().toLowerCase().replace(/^#/, '')
  const match = (v: OrderView) => !query || `${v.o.id} ${v.o.customer} ${SIZE_LABEL[v.o.size]}`.toLowerCase().replace(/#/g, '').includes(query)
  const inShow = (v: OrderView) => show === 'all' || (show === 'risk' ? v.risk === 'late' || v.risk === 'risk'
    : show === 'rush' ? v.o.rush : show === 'open' ? v.o.status !== '已出貨' : v.o.status === '已出貨')
  const list = views.filter((v) => match(v) && inShow(v))
  const count = (s: Show) => views.filter((v) => s === 'all' || (s === 'risk' ? v.risk === 'late' || v.risk === 'risk' : s === 'rush' ? v.o.rush : s === 'open' ? v.o.status !== '已出貨' : v.o.status === '已出貨')).length
  const sel = views.find((v) => v.o.id === picked) ?? (query ? list[0] : null) ?? null
  if (!w) return <div className="loading">讀取訂單中…（訂單資料來自後端；瀏覽器模擬模式沒有這部分）</div>
  const atRisk = views.filter((v) => v.risk === 'late' || v.risk === 'risk')
  const dueSoon = views.filter((v) => v.o.status !== '已出貨' && v.due - now < 3 * DAY)
  return (
    <div className="orders-tab" data-testid="orders-tab">
      <section className="kpis" aria-label="訂單重點">
        <div className="kpi"><span className="kpi-l">全部訂單</span><span className="kpi-v">{views.length}<small> 張</small></span>
          <span className="kpi-s">未完成 {count('open')}・已出貨 {count('shipped')}</span></div>
        <div className={`kpi ${atRisk.length ? 'is-alarm' : ''}`}><span className="kpi-l">有風險</span><span className="kpi-v" data-testid="kpi-risk">{atRisk.length}<small> 張</small></span>
          <span className="kpi-s">{atRisk.length ? atRisk.slice(0, 3).map((v) => v.o.id).join('、') : '全部預估準時'}</span></div>
        <div className={`kpi ${w.orders.rush_open ? 'is-warn' : ''}`}><span className="kpi-l">急單</span><span className="kpi-v">{count('rush')}<small> 張</small></span>
          <span className="kpi-s">未完成 {w.orders.rush_open}</span></div>
        <div className="kpi"><span className="kpi-l">3 天內到期</span><span className="kpi-v">{dueSoon.length}<small> 張</small></span>
          <span className="kpi-s">今日新單 {w.orders.new_today}</span></div>
      </section>
      <div className="order-tools">
        <form className="order-search" role="search" onSubmit={(e) => { e.preventDefault(); if (list[0]) setPicked(list[0].o.id) }}>
          <input value={q} onChange={(e) => { setQ(e.target.value); setPicked(null) }} placeholder="訂單號、客戶或胎型，例如 A-311、中胎" aria-label="查訂單" data-testid="order-search" />
          <button type="submit" className="btn-approve">查</button>
        </form>
        <div className="seg sm" role="group" aria-label="篩選訂單">
          {SHOW.map((s) => <button key={s.id} className={show === s.id ? 'on' : ''} aria-pressed={show === s.id} onClick={() => setShow(s.id)}>{s.label}<small> {count(s.id)}</small></button>)}
        </div>
      </div>
      {sel && <OrderCard o={sel.o} jobs={jobs} now={now} warnings={warningsFor(sel.o.id, warnings)} />}
      <section className="panel order-table" aria-label="訂單清單">
        <header className="panel-h"><h2>{query || show !== 'all' ? `符合的訂單（${list.length}）` : `全部訂單（${views.length}）`}</h2><span className="muted small">有風險的排最前・點一列看明細</span></header>
        {query && !list.length && <p className="note">找不到「{q}」。請確認訂單號，或清空搜尋看全部訂單。</p>}
        <div className="ot-head" aria-hidden><span>訂單</span><span>狀態</span><span>各站進度（投料・貼合・入模・出模・完成・出貨）</span><span>預估完成</span><span>交期</span></div>
        <ul className="ot-list" role="list" data-testid="order-list">
          {list.map((v) => (
            <li key={v.o.id}>
              <button className={`ot-row r-${v.risk} ${sel?.o.id === v.o.id ? 'on' : ''}`} onClick={() => setPicked(sel?.o.id === v.o.id ? null : v.o.id)} aria-expanded={sel?.o.id === v.o.id} data-order={v.o.id}>
                <span className="ot-id"><b>{v.o.id}</b>{v.o.rush && <em className="tag rush">急單</em>}<small className="muted">{v.o.customer}・{SIZE_LABEL[v.o.size]} {v.o.qty} 條</small></span>
                <span className="ot-st"><i className={`risk-dot r-${v.risk}`} aria-hidden />{RISK_TEXT[v.risk]}{v.lateH != null && <small> {v.lateH} h</small>}<small className="muted">{v.o.status}・{v.o.done}/{v.o.qty}</small></span>
                <span className="ot-stages" aria-label={v.stages.map((s) => `${s.k} ${s.v}/${s.of}`).join('，')}>
                  {v.stages.map((s) => {
                    const pct = s.of ? Math.round((100 * s.v) / s.of) : 0
                    return <i key={s.k} className={pct >= 100 ? 'done' : pct > 0 ? 'part' : ''} title={`${s.k} ${s.of ? `${s.v}/${s.of}` : '—'}`}><b style={{ width: `${pct}%` }} /></i>
                  })}
                </span>
                <span className="ot-eta">{v.projected ? `預估 ${v.projected}` : isFinite(v.eta) ? `${fmtMD(v.eta)} ${fmtHM(v.eta)}` : v.o.status === '已出貨' && v.o.shipped_at ? `出貨 ${fmtMD(v.o.shipped_at)}` : '—'}</span>
                <span className={`ot-due ${v.risk === 'late' ? 'late' : ''}`}>{fmtMD(v.o.due)}</span>
                {v.why.length > 0 && v.risk !== 'done' && <span className="ot-why">{v.why.join('；')}</span>}
              </button>
            </li>
          ))}
        </ul>
        <p className="muted small">預估完成＝目前生效排程裡這張單最後一條出模的時間；超出排程視窗的部分用排程器依產能估的日期（「預估 10/16」）；各站進度依排程時間與工廠時鐘計算，完成與出貨依工廠紀錄。</p>
      </section>
    </div>
  )
}
