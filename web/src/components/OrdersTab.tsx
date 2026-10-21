// 訂單 (UX brief moment 「查一張單」; CJ, 2026-10-20: 「看訂單狀況這個我很需要」): the answer first (veteran 10-22 for
// HY: will any order be late, how many are not scheduled yet), then every order with its status, per-stage progress,
// ETA against the due date, late orders first, rush orders marked, and a search box. 今日訂單 lives on the phone only.
// Picking a row opens its card (where its tyres are now, what blocks it). Data: GET /api/world orders + the
// active schedule's jobs per order (A 線投料, 貼合, 入模, 出模); see orderModel.ts.
import { useMemo, useState } from 'react'
import { SIZE_LABEL } from '../factory'
import { fmtHM, fmtMD, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { OrderCard } from './OrderCard'
import { successOf, type Success } from '../agent/tickerItems'
import { RISK_TEXT, byRisk, orderView, warningsFor, type OrderSuccess, type OrderView } from './orderModel'

// four filters with their counts on them (veteran 10-22 for HY: the KPI numbers move here; fewer buttons, no line breaks)
type Show = 'all' | 'late' | 'rush' | 'finished'
const SHOW: { id: Show; label: string }[] = [
  { id: 'all', label: '全部' }, { id: 'late', label: '會晚交' }, { id: 'rush', label: '急單' }, { id: 'finished', label: '已完成・出貨' },
]
const inShowOf = (v: OrderView, s: Show) => s === 'all' || (s === 'late' ? v.risk === 'late' : s === 'rush' ? v.o.rush : v.risk === 'complete' || v.risk === 'done')

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
  const [q, setQ] = useState('')
  const [show, setShow] = useState<Show>('all')
  const [picked, setPicked] = useState<string | null>(null)
  const query = q.trim().toLowerCase().replace(/^#/, '')
  const match = (v: OrderView) => !query || `${v.o.id} ${v.o.customer} ${SIZE_LABEL[v.o.size]}`.toLowerCase().replace(/#/g, '').includes(query)
  const inShow = (v: OrderView) => inShowOf(v, show)
  const list = views.filter((v) => match(v) && inShow(v))
  const count = (s: Show) => views.filter((v) => inShowOf(v, s)).length
  const sel = views.find((v) => v.o.id === picked) ?? (query ? list[0] : null) ?? null
  if (!w) return <div className="loading">讀取訂單中…（訂單資料來自後端；瀏覽器模擬模式沒有這部分）</div>
  const late = views.filter((v) => v.risk === 'late')
  const unsched = views.filter((v) => v.risk === 'unsched').sort((a, b) => a.due - b.due)
  return (
    <div className="orders-tab" data-testid="orders-tab">
      <section className="panel order-answer" data-testid="orders-answer" aria-label="訂單現況">
        <p className={`oa-1 ${late.length ? 'late' : 'ok'}`}>{late.length
          ? `${late.length} 張訂單會晚交：${late.slice(0, 3).map((v) => v.o.id).join('、')}${late.length > 3 ? '…' : ''}`
          : '沒有訂單會晚交'}</p>
        {unsched.length > 0 && <p className="oa-2">{unsched.length} 張還沒排進硫化排程，最近交期 {fmtMD(unsched[0].o.due)}（{unsched[0].o.id}）</p>}
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
        <header className="panel-h"><h2>{query || show !== 'all' ? `符合的訂單 ${list.length} 張` : `訂單 ${views.length} 張`}</h2><span className="muted small">會晚交的排最前，點一列看明細</span></header>
        {query && !list.length && <p className="note">找不到「{q}」。請確認訂單號，或清空搜尋看全部訂單。</p>}
        <div className="ot-head" aria-hidden><span>訂單</span><span>狀態</span><span>進度</span><span>預估完成</span><span>交期</span></div>
        <ul className="ot-list" role="list" data-testid="order-list">
          {list.map((v) => (
            <li key={v.o.id}>
              <button className={`ot-row r-${v.risk} ${sel?.o.id === v.o.id ? 'on' : ''}`} onClick={() => setPicked(sel?.o.id === v.o.id ? null : v.o.id)} aria-expanded={sel?.o.id === v.o.id} data-order={v.o.id}>
                <span className="ot-id"><b>{v.o.id}</b>{v.o.rush && <em className="tag rush">急單</em>}<small className="muted">{v.o.customer}・{SIZE_LABEL[v.o.size]} {v.o.qty} 條</small></span>
                <span className="ot-st">{v.success ? <span className="ok-mark" aria-hidden>✓</span> : <i className={`risk-dot r-${v.risk}`} aria-hidden />}{RISK_TEXT[v.risk]}{v.lateH != null && <small> {v.lateH} h</small>}<small className="muted">{v.o.status}</small></span>
                <Progress v={v} />
                <span className="ot-eta">{v.projected ? `預估 ${v.projected}` : isFinite(v.eta) ? `${fmtMD(v.eta)} ${fmtHM(v.eta)}` : v.o.status === '已出貨' && v.o.shipped_at ? `出貨 ${fmtMD(v.o.shipped_at)}` : v.risk === 'unsched' ? '未排程' : '—'}</span>
                <span className={`ot-due ${v.risk === 'late' ? 'late' : ''}`}>{fmtMD(v.o.due)}</span>
                {v.why.length > 0 && !v.success && <span className="ot-why">{v.why.join('；')}</span>}
                {v.success && <span className="ot-why ok" data-testid="order-success">{successLine(v)}</span>}
              </button>
            </li>
          ))}
        </ul>
        <p className="muted small">預估完成＝排程裡這張單最後一條出模的時間；寫「預估」的是依產能推算。</p>
      </section>
    </div>
  )
}

/** One sentence and one bar instead of six unlabelled stage bars (veteran 10-22 for HY): 「完成 19/30・硫化中 5 條」;
 *  the per-stage detail stays on the order card. */
function Progress({ v }: { v: OrderView }) {
  const { o } = v
  const inMold = Math.max(0, v.stages[2].v - v.stages[3].v)       // 入模硫化 minus 出模: tyres in a press now
  const pct = (n: number) => (o.qty ? Math.min(100, (100 * n) / o.qty) : 0)
  const text = o.status === '已出貨' ? `已出貨 ${o.qty} 條` : `完成 ${o.done}/${o.qty}${inMold ? `・硫化中 ${inMold} 條` : ''}`
  return (
    <span className="ot-prog">
      <span className="ot-pt">{text}</span>
      <span className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={o.qty} aria-valuenow={o.done} aria-label={text}>
        <i style={{ width: `${pct(o.done)}%` }} /><i className="mold" style={{ width: `${pct(inMold)}%` }} />
      </span>
    </span>
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
