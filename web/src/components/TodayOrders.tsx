// 今日訂單 (issue #95): today's overview first (production / delivery, counted once per order), then the orders
// that are overdue, predicted late, or otherwise at risk -- kept apart and each with its due time, evidence-based
// finish time and reason. Everything else is one compact line (id, done count, status). Detail is a fold-out;
// the counts always come from the whole list. Same data and rules as 問戰情室 (GET /api/orders/today), so the page
// and the chat cannot disagree. Unknown stays 待確認 / 尚無排程; nothing is shown as 0.
import { useEffect, useState } from 'react'
import { fmtHM, fmtMD, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import type { OrderBrief, OrdersToday } from './ordersToday'

const dt = (iso: string | null) => (iso ? `${fmtMD(iso)} ${fmtHM(iso)}` : '待確認')
const dueText = (b: OrderBrief) => (b.due_at ? dt(b.due_at) : `${fmtMD(b.due)}（只有日期，未提供時間）`)
const etaText = (b: OrderBrief) => (b.eta ? dt(b.eta) : `待確認（${b.eta_unknown_reason ?? '尚無資料'}）`)
const mach = (ids: string[]) => ids.map((m) => m.replace(/^CURE-0?/, '') + ' 號機').join('、')

function RiskRow({ b, kind }: { b: OrderBrief; kind: 'overdue' | 'late' | 'other' }) {
  return (
    <li className={`to-risk to-${kind}`} data-order={b.id} data-testid={`today-risk-${kind}`}>
      <b>{b.id}</b><span className="to-tag">{kind === 'overdue' ? '已遲交' : kind === 'late' ? '預估會遲交' : '其他風險'}</span>
      <span className="to-line">交期 {dueText(b)}・預計完成 {etaText(b)}</span>
      <span className="to-why">{b.reasons.join('；') || '—'}</span>
    </li>
  )
}

export function TodayOrders({ compact = false }: { compact?: boolean }) {
  const { state, transport } = useWarRoom()
  const [data, setData] = useState<OrdersToday | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [more, setMore] = useState(false)
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const minute = isFinite(now) ? Math.floor(now / 60_000) : 0
  const live = !!transport?.ordersToday
  // refetch every factory minute and whenever the active schedule changes (insert / approved replan)
  useEffect(() => {
    if (!transport?.ordersToday) return
    let dead = false
    transport.ordersToday().then((d) => { if (!dead) { setData(d); setErr(null) } }).catch((e) => { if (!dead) setErr(String(e)) })
    return () => { dead = true }
  }, [transport, minute, state.scheduleRev])
  if (!live) return <section className="panel today-orders"><h2>今日訂單</h2><p className="muted small">模擬模式沒有訂單資料，需連線後端。</p></section>
  if (err && !data) return <section className="panel today-orders"><h2>今日訂單</h2><p className="err">讀不到今日訂單：{err}</p></section>
  if (!data) return <section className="panel today-orders"><h2>今日訂單</h2><p className="muted small">讀取中…</p></section>
  const by = new Map(data.orders.map((b) => [b.id, b]))
  const get = (ids: string[]) => ids.map((i) => by.get(i)).filter((b): b is OrderBrief => !!b)
  const c = data.counts
  const riskIds = new Set(data.at_risk.concat(data.overdue))
  const today = get(data.production.concat(data.delivery.filter((i) => !data.production.includes(i))))
  const calm = today.filter((b) => !riskIds.has(b.id))
  const LIM = compact ? 4 : 8
  const shown = more ? calm : calm.slice(0, LIM)
  return (
    <section className="panel today-orders" data-testid="today-orders" aria-label="今日訂單">
      <header className="to-h"><h2>今日訂單</h2><span className="muted small">{fmtMD(data.date)}・排程 v{data.schedule_version ?? '待確認'}</span></header>
      <p className="to-sum" data-testid="today-summary">
        今日生產 <b>{c.production}</b> 張・今日交貨 <b>{c.delivery}</b> 張・合計 <b>{c.unique_today}</b> 張不重複<span className="muted small">（共 {c.total} 張）</span>
      </p>
      {c.overdue + c.late + c.other_risk > 0 ? (
        <ul className="to-risks" role="list" aria-label="需要注意的訂單">
          {get(data.overdue).map((b) => <RiskRow key={b.id} b={b} kind="overdue" />)}
          {get(data.late).map((b) => <RiskRow key={b.id} b={b} kind="late" />)}
          {get(data.other_risk).map((b) => <RiskRow key={b.id} b={b} kind="other" />)}
        </ul>
      ) : <p className="muted small" data-testid="today-no-risk">目前沒有遲交或其他風險的訂單。</p>}
      {c.unique_today === 0 && <p className="muted small">今天沒有生產或交貨的訂單（沒有拿其他日期的訂單代替）。</p>}
      {calm.length > 0 && (
        <ul className="to-calm" role="list" aria-label="其他今日訂單">
          {shown.map((b) => (
            <li key={b.id} data-order={b.id}>
              <details>
                <summary><b>{b.id}</b><span>已完成 {b.done}／{b.qty}</span><span className="muted">{b.state_text}</span></summary>
                <dl className="to-dl">
                  <dt>機台</dt><dd>{b.machines.length ? mach(b.machines) : (b.schedule_state ?? '待確認')}</dd>
                  <dt>預計完成</dt><dd>{etaText(b)}</dd>
                  <dt>交期</dt><dd>{dueText(b)}</dd>
                  <dt>今日</dt><dd>{[b.in_production_today ? `生產（${b.production_basis.join('、')}）` : '', b.due_today ? '交貨' : ''].filter(Boolean).join('；') || '—'}</dd>
                </dl>
              </details>
            </li>
          ))}
        </ul>
      )}
      {calm.length > LIM && <button type="button" className="to-more" onClick={() => setMore(!more)}>{more ? '收起' : `再看 ${calm.length - LIM} 張`}</button>}
    </section>
  )
}
