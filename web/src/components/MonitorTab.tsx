import { useRef, useState } from 'react'
import type { ApprovalRequest, BadDataMark, DashboardSnapshot, Event, FocusPanel, MachineTile, OrderProgress, SemiFinishedProgress, TimelineItem } from '../contracts'
import { LINES, LINE_SHORT, MACHINE_META, ML_PRESSES, SIZE_LABEL, machineLabel } from '../factory'
import { fmtHM, fmtMD, fmtNum } from '../format'
import { closedTitle, hypLabel, hypName, plain } from '../plain'
import { isAbnormal, primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { Panel, QUALITY_LABEL, STATE_LABEL, STATUS_LABEL, StageChain, TimelineTable } from './common'
import { TemperatureTrend } from './Trend'
import { PendingApproval } from '../agent/ApprovalVisual'
import { OrderGoodsLine, OrderRing, PlantPanels } from './PlantPanels'
import { ORDER_STAGES } from './orderModel'

/** 產線監控 (proposal page 2) with the whole plant below it — issue #37 merged the 工廠全貌 tab in here, so the
 *  page reads top-down: 預警 (rendered by App), the KPI strip, the approval card, ③ event / ④ 28 machines /
 *  ①② orders and semi-finished goods, then people and materials (dock, goods, tasks and the rest of the plant fold into
 *  「更多全廠資訊」). UX declutter: the page is for the 現場主管; on a phone ①②④ and the plant fold into their headers. */
export function MonitorTab({ onMachine, onAgent, onOrders }: { onMachine: (id: string) => void; onAgent?: (eventId?: string) => void; onOrders?: () => void }) {
  const { state } = useWarRoom()
  const s = state.snapshot
  if (!s) return <div className="loading">等待工廠資料…</div>
  return (
    <div className="monitor-wrap">
      <KpiStrip s={s} pending={state.pending} />
      <PendingCards pending={state.pending} focusEvent={s.focus?.event_id ?? primaryEvent(s.events)?.id ?? null} onAgent={onAgent} />
      <div className="monitor">
        <FocusPanelView focus={s.focus} pending={state.pending} />
        <div className="col-left">
          <OrdersPanel orders={s.orders} onOrders={onOrders} />
          <SemiPanel semi={s.semi_finished} />
        </div>
        <MachinesPanel machines={s.machines} bad={s.header.bad_data ?? []} onMachine={onMachine} focusId={s.focus?.machine_id} />
      </div>
      <PlantPanels />
    </div>
  )
}

/** One visual card (the focus event's, else the oldest) above the monitor; any other pending
 *  approval is a one-line row that opens the Agent tab on its event, so the plant view stays near the top.
 *  UX declutter: the card is compact here — risk, title, reason, the system's recommended option and 核准 / 討論
 *  stay; the evidence (plan, schedule before/after, options, tools, conclusion) opens with 「展開」. */
function PendingCards({ pending, focusEvent, onAgent }: { pending: ApprovalRequest[]; focusEvent: string | null; onAgent?: (eventId?: string) => void }) {
  const { state } = useWarRoom()
  if (!pending.length) return null
  const main = pending.find((a) => a.event_id === focusEvent) ?? pending[0]
  const rest = pending.filter((a) => a !== main)
  const machineOf = (a: ApprovalRequest) => state.snapshot?.events.find((e) => e.id === a.event_id)?.machine_id
  return (
    <>
      <CompactApproval key={main.id} req={main} onAgent={onAgent ? () => onAgent(main.event_id) : undefined} />
      {rest.length > 0 && (
        <ul className="apv-more" aria-label="其他待核准" data-testid="pending-more">
          {rest.map((a) => {
            const m = machineOf(a)
            return (
              <li key={a.id}>
                <a href="#agent" onClick={(e) => { if (onAgent) { e.preventDefault(); onAgent(a.event_id) } }}>
                  <span className={`risk r-${a.risk}`}>{a.risk}</span>
                  {m && <b>{machineLabel(m)}</b>}
                  <span>{plain(a.title)}</span>
                  <span className="go">看計畫並核准 →</span>
                </a>
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}

/** The shared approval card with its evidence grid folded away (CSS hides `.apv-grid` until 「展開」); the card
 *  itself lives in agent/ and is not changed, so 核准 / 討論 and the option ids keep their test hooks.
 *  Folded, a small A/B toggle picks the option without 展開: it clicks the card's own (hidden) option button, so the
 *  card keeps the one selection and 「核准（A）」 sends the same request and option ids. */
function CompactApproval({ req, onAgent }: { req: ApprovalRequest; onAgent?: () => void }) {
  const [open, setOpen] = useState(false)
  const rec = req.options.find((o) => o.recommended) ?? null
  const box = useRef<HTMLDivElement>(null)
  const [pick, setPick] = useState<string | null>(null)
  const chosen = pick ?? rec?.id ?? req.options[0]?.id ?? null
  const choose = (id: string) => box.current?.querySelector<HTMLButtonElement>(`[data-testid="option-${id}"]`)?.click()
  // follows a pick made in the expanded card too (its option buttons are data-testid="option-<id>")
  const follow = (e: React.MouseEvent) => {
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-testid^="option-"]')
    if (b && !b.disabled && b.dataset.testid) setPick(b.dataset.testid.slice('option-'.length))
  }
  return (
    <div ref={box} className={`apv-compact ${open ? 'is-open' : ''}`} data-testid="approval-compact" onClick={follow}>
      <PendingApproval req={req} onAgent={onAgent} />
      <div className="apv-fold">
        {req.options.length > 1 && !open ? (
          <span className="apv-fold-pick" role="radiogroup" aria-label="人力方案" data-testid="apv-fold-pick">
            <b>人力方案</b>
            {req.options.map((o) => (
              <button key={o.id} type="button" role="radio" aria-checked={chosen === o.id} className={chosen === o.id ? 'on' : ''}
                onClick={(e) => { e.stopPropagation(); choose(o.id) }} data-testid={`apv-fold-opt-${o.id}`}>
                {o.id} {plain(o.label)}{o.recommended ? <small>系統建議</small> : null}
              </button>
            ))}
          </span>
        ) : rec ? <span className="apv-fold-rec"><b>系統建議</b>{rec.id} {plain(rec.label)}{req.options.length > 1 ? `（共 ${req.options.length} 個方案）` : ''}</span> : <span />}
        <button type="button" className="btn-ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)} data-testid="apv-expand">
          {open ? '收起 ▴' : '展開：處理計畫、排程前後對照、方案與結論 ▾'}
        </button>
      </div>
    </div>
  )
}

/** The numbers a supervisor reads first: machines running, what needs attention, what waits for approval, where the AI
 *  is, who is in the plant. Low materials are not repeated here: they are already rows in 預警 (UX declutter). */
function KpiStrip({ s, pending }: { s: DashboardSnapshot; pending: ApprovalRequest[] }) {
  const w = useWarRoom().state.world
  const h = s.header
  const total = h.normal_count + h.abnormal_count
  const bad = s.machines.filter((m) => isAbnormal(m.status))
  const ev = primaryEvent(s.events)
  const open = !!ev && ev.state !== 'closed' && ev.state !== 'safe_stop'
  const round = pending.reduce((r, a) => Math.max(r, a.round), 0)
  return (
    <section className="kpis" aria-label="全廠重點">
      <div className="kpi">
        <span className="kpi-l">機台運轉</span>
        <span className="kpi-v" data-testid="kpi-normal">{h.normal_count}<small> / {total} 台</small></span>
        <span className="kpi-s">事件偵測常駐監看，不經 AI</span>
      </div>
      <div className={`kpi ${h.abnormal_count > 0 ? 'is-alarm' : ''}`}>
        <span className="kpi-l">需要注意</span>
        <span className="kpi-v">{h.abnormal_count}<small> 台</small></span>
        <span className="kpi-s">{bad.length ? bad.map((m) => machineLabel(m.machine_id)).join('、') : '全廠正常'}</span>
      </div>
      <div className={`kpi ${pending.length ? 'is-warn' : ''}`}>
        <span className="kpi-l">等主管核准</span>
        <span className="kpi-v">{pending.length}<small> 件</small></span>
        <span className="kpi-s">{pending.length ? `第 ${round} 輪・已推到主管手機` : '沒有待核准事項'}</span>
      </div>
      <div className={`kpi ${open ? 'is-live' : ''}`}>
        <span className="kpi-l">AI 指揮官</span>
        <span className="kpi-v kpi-word">{open ? STATE_LABEL[ev.state] : '待命'}</span>
        <span className="kpi-s">{open ? `${plain(ev.machine_id)}${ev.replans ? `・重新規劃 ${ev.replans} 次` : '・處理中'}` : ev ? `上一事件：${machineLabel(ev.machine_id)}已結案` : '有異常才喚醒'}</span>
      </div>
      {w && (
        <div className="kpi">
          <span className="kpi-l">在廠人員</span>
          <span className="kpi-v" data-testid="kpi-people">{w.people.on_duty}<small> / {w.people.total} 人</small></span>
          <span className="kpi-s">在機台 {w.people.by_state['在機台'] ?? 0}・請假 {w.people.by_state['請假'] ?? 0}</span>
        </div>
      )}
    </section>
  )
}

/** ① 訂單: the proposal's per-size progress (page 2) with 剩餘約 N 小時; the order pipeline, the open orders with their
 *  goods rings (was 工廠全貌's 訂單流程) and today's counts fold into 「訂單明細」 so ①②③④ fit the first screen
 *  (UX declutter). Search, ETA vs due and risk are on the 訂單 tab. */
function OrdersPanel({ orders, onOrders }: { orders: OrderProgress[]; onOrders?: () => void }) {
  const w = useWarRoom().state.world
  const open = (w?.orders.items ?? []).filter((o) => o.status === '待排程' || o.status === '生產中')
    .sort((a, b) => Number(b.rush) - Number(a.rush) || (a.due < b.due ? -1 : 1)).slice(0, 4)
  return (
    <Panel num="①" title="訂單進度" className="orders" fold="phone"
      right={onOrders && <a className="panel-link" href="#orders" onClick={(e) => { e.preventDefault(); onOrders() }}>全部訂單 →</a>}>
      {orders.map((o) => (
        <div className="order" key={o.size}>
          <div className="order-row">
            <span className="order-l">{o.label}<b>{o.done}</b><small> / {o.total}</small></span>
            <span className={o.note ? 'order-h moved' : 'muted'}>剩餘約 {fmtNum(o.remaining_h, 0)} 小時</span>
          </div>
          {o.note && <p className="order-note small">{plain(o.note)}</p>}
          <div className="bar" role="progressbar" aria-valuenow={o.done} aria-valuemax={o.total} aria-label={`${o.label}進度`}>
            <i style={{ width: `${(100 * o.done) / Math.max(1, o.total)}%` }} />
          </div>
        </div>
      ))}
      {w && (
        <details className="orders-more" data-testid="orders-more">
          <summary>訂單明細（流程、生產中的單、今日新單與庫存）</summary>
          <ol className="pipe" data-testid="order-pipeline" aria-label="訂單流程">
            {ORDER_STAGES.map((st) => <li key={st} className={w.orders.pipeline[st] ? 'has' : ''}><b>{w.orders.pipeline[st] ?? 0}</b><span>{st}</span></li>)}
          </ol>
          <ul className="w-list">
            {open.map((o) => (
              <li key={o.id}>
                <div className="w-row">
                  <span><OrderRing w={w} id={o.id} /><b>{o.id}</b>{o.rush && <i className="tag rush">急單</i>}<span className="muted"> {o.customer}・{SIZE_LABEL[o.size]}</span></span>
                  <span className="muted small">{o.done}/{o.qty}・交期 {fmtMD(o.due)}</span>
                </div>
                <OrderGoodsLine w={w} id={o.id} />
              </li>
            ))}
          </ul>
          <p className="muted small w-note">今日新單 {w.orders.new_today}{w.orders.rush_open ? `・急單 ${w.orders.rush_open}` : ''}・庫存成品 {w.finished_goods.total}/{w.finished_goods.capacity} 條・待出貨 {w.finished_goods.waiting_ship} 條</p>
        </details>
      )}
    </Panel>
  )
}

/** ② stays open on desktop as a compact one-line-per-part list. The data has one planned mold-in time for the
 *  whole tyre (not per part), so it sits in the header; the note trails in small text. */
function SemiPanel({ semi }: { semi: SemiFinishedProgress }) {
  return (
    <Panel num="②" title={`半成品進度（${semi.tire_label}）`} className="semi" fold="phone"
      right={semi.planned_mold_in_at && <span className="muted small">預計 {fmtHM(semi.planned_mold_in_at)} 入硫化機</span>}>
      <ul className="semi-list compact">
        {semi.parts.map((p) => (
          <li key={p.part}>
            <span>{p.label}</span>
            <span className={`semi-st ${p.state}`}>{p.status_text}</span>
          </li>
        ))}
      </ul>
      {semi.note && <p className={`small semi-note semi-st ${semi.note_state ?? ''}`}>{plain(semi.note)}</p>}
    </Panel>
  )
}

function FocusPanelView({ focus, pending }: { focus: FocusPanel | null; pending: ApprovalRequest[] }) {
  const { state } = useWarRoom()
  if (!focus) {
    const ev = primaryEvent(state.snapshot?.events)
    return (
      <Panel num="③" title="目前事件" className="focus calm">
        <div className="calm-body">
          <p className="calm-title">全廠正常</p>
          <p className="muted">事件偵測常駐監看 28 台機台（不經 AI、全天候）。發現異常 1 分鐘內喚醒 AI 指揮官。</p>
          {(state.snapshot?.header.bad_data_total ?? 0) > 0 && (
            <p className="note dq-note" data-testid="bad-data-note">驗證層攔下 {state.snapshot?.header.bad_data_total} 筆壞資料（近 30 分）：{(state.snapshot?.header.bad_data ?? []).map((b) => `${machineLabel(b.machine_id)} ${b.count} 筆・${QUALITY_LABEL[b.last_quality] ?? b.last_quality}`).join('；')}。壞資料不進預測、不喚醒 AI。</p>
          )}
          {ev && <p className="muted small">上一事件：{machineLabel(ev.machine_id)}・{STATE_LABEL[ev.state]}{ev.outcome ? `・${plain(ev.outcome)}` : ''}</p>}
        </div>
      </Panel>
    )
  }
  const closed = focus.state === 'closed' || focus.state === 'safe_stop'
  const ev = state.snapshot?.events.find((e) => e.id === focus.event_id) ?? null
  const view = focusView(focus, ev, [...pending, ...state.resolved])
  return (
    <Panel num="③" title="目前事件" className={`focus ${closed ? 'closed' : 'hot'}`} right={<span className={`state-pill st-${focus.state}`}>{STATE_LABEL[focus.state]}</span>}>
      {ev && <StageChain ev={ev} />}
      <h3 className="focus-title">{closedTitle(plain(view.title), closed)}</h3>
      <p className="focus-detail">{plain(view.detail)}</p>
      <div className={`focus-grid ${ev && ev.hypotheses.length > 0 ? 'has-judge' : ''}`}>
        <div className="focus-main">
          {focus.machine_id in MACHINE_META && <TemperatureTrend machineId={focus.machine_id} hypotheses={ev?.hypotheses} />}
          <div className="tiles">
            {focus.tiles.map((t) => (
              <div className={`tile ${t.status}`} key={t.label}>
                <span className="tile-l">{t.label}</span>
                <span className="tile-v">{tileValue(t.label, t.value, focus.machine_id)}</span>
              </div>
            ))}
          </div>
        </div>
        {ev && ev.hypotheses.length > 0 && <JudgmentTrail ev={ev} />}
      </div>
      <h4 className="sub">處置與核准</h4>
      <ul className="actions">
        {focus.auto_done.map((a) => <li key={a} className="done"><span className="ck">✓</span><span><em className="act-k">已自動<span className="sr">：</span></em>{plain(a)}</span></li>)}
        {(focus.approved ?? []).map((a, i) => (
          <li key={`${a.approval_id}-${i}`} className="done approved">
            <span className="ck">✓</span><span><em className="act-k">已核准<span className="sr">：</span></em>{plain(a.label)}<small className="muted">（{whoLabel(a.decided_by)}）</small></span>
          </li>
        ))}
        {(focus.revoked ?? []).map((r) => (
          <li key={`rv-${r}`} className="revoked"><span className="ck" aria-hidden>↺</span><span><em className="act-k">已撤回<span className="sr">：</span></em>{plain(r)}</span></li>
        ))}
        {focus.pending.length > 0 && (
          <li className="todo-visual"><span className="ck" aria-hidden>◆</span><span><em className="act-k">待核准<span className="sr">：</span></em>{focus.pending.length > 1 ? `${focus.pending.length} 個方案擇一，` : ''}請在上方核准卡看計畫、排程與方案後決定</span></li>
        )}
      </ul>
      <p className="impact"><b>排程影響<span className="sr">：</span></b>{plain(focus.schedule_impact)}</p>
      {focus.timeline.length > 0 && <FocusTimeline items={focus.timeline} />}
    </Panel>
  )
}

/** ③'s 事件時間軸 (the demo opens it here): the last 6 rows, 「看全部」 for the rest. The full table used to be ⑦ on
 *  人力排程; that copy was removed in the UX declutter, so this is where the whole timeline is read now. */
function FocusTimeline({ items }: { items: TimelineItem[] }) {
  const [all, setAll] = useState(false)
  return (
    <details className="focus-tl" data-testid="focus-timeline">
      <summary>事件時間軸（{items.length} 筆）</summary>
      <TimelineTable items={all ? items : items.slice(-6)} compact />
      {items.length > 6 && (
        <button type="button" className="btn-ghost tl-all" aria-expanded={all} onClick={() => setAll((a) => !a)} data-testid="timeline-all">
          {all ? '只看最近 6 筆' : `看全部（${items.length} 筆）`}
        </button>
      )}
    </details>
  )
}

/** Page 7 "dynamic adjustment": each hypothesis the commander made, what it expected, and what code verified. */
function JudgmentTrail({ ev }: { ev: Event }) {
  return (
    <section className="judge" aria-label="AI 指揮官的判斷">
      <h4 className="sub">AI 指揮官的判斷</h4>
      <ol className="judge-list">
        {ev.hypotheses.map((h, i) => {
          const label = hypLabel(h.status, ev)
          const e = h.expectation
          const evidence = [...h.evidence].reverse().find((x) => /°C/.test(x))
          return (
            <li key={h.id} className={`jg jg-${h.status} ${label === '驗證中' ? 'live' : ''}`}>
              {i > 0 && ev.hypotheses[i - 1].status === 'rejected' && <span className="jg-turn">沒達到預期，改判</span>}
              <div className="jg-head">
                <b>{hypName(h.id)}</b>
                <span className="jg-st">{label}</span>
              </div>
              <p className="jg-text">{plain(h.statement)}</p>
              {!e && h.status !== 'rejected' && <p className="jg-exp">沒有可量測的預期值，依 SOP 處置並交維修確認</p>}
              {e && <p className="jg-exp">預期：{e.within_min} 分鐘內{e.op === '>=' ? `回到 ${e.value}°C 以上` : e.op === '<=' ? `降到 ${e.value}°C 以下` : `維持在 ${e.value}–${e.value_hi}°C`}</p>}
              {h.status === 'rejected' && evidence && <p className="jg-ev">程式驗證：{plain(evidence)}</p>}
            </li>
          )
        })}
      </ol>
    </section>
  )
}

function MachinesPanel({ machines, bad, onMachine, focusId }: { machines: MachineTile[]; bad: BadDataMark[]; onMachine: (id: string) => void; focusId?: string }) {
  const badBy = new Map(bad.map((b) => [b.machine_id, b]))
  const dqText = (id: string) => {
    const b = badBy.get(id)
    return b ? `・壞資料 ${b.count} 筆（${QUALITY_LABEL[b.last_quality] ?? b.last_quality}，未喚醒 AI）` : ''
  }
  const presses = machines.filter((m) => m.line === 'CURE')
  const off = machines.filter((m) => isAbnormal(m.status))
  return (
    <Panel num="④" title="機台狀態" className="machines" fold="phone"
      summary={off.length ? <span className="late">需要注意：{off.map((m) => machineLabel(m.machine_id)).join('、')}</span> : `${machines.length} 台・全部正常`}>
      <p className="mach-k"><span>硫化機</span><small className="muted">瓶頸製程・7–10 號可做中大胎</small></p>
      <div className="presses" data-line="CURE">
        {presses.map((m) => (
          <button key={m.machine_id} className={`mdot press st-${m.status}${badBy.has(m.machine_id) ? ' dq' : ''}${m.machine_id === focusId ? ' is-focus' : ''}`}
            onClick={() => onMachine(m.machine_id)} data-machine={m.machine_id}
            title={`${machineLabel(m.machine_id)}・${STATUS_LABEL[m.status]}${m.temp_c != null ? `・${m.temp_c}°C` : ''}${dqText(m.machine_id)}`}
            aria-label={`${machineLabel(m.machine_id)} ${STATUS_LABEL[m.status]}${m.temp_c != null ? ` ${m.temp_c}°C` : ''}${dqText(m.machine_id)}`}>
            <span className="press-n">{m.short_name}<small>號</small></span>
            <span className="press-t">{m.temp_c != null ? `${Math.round(m.temp_c * 10) / 10}°` : '—'}</span>
            <span className="press-s">{STATUS_LABEL[m.status]}{ML_PRESSES.includes(m.machine_id) && <span className="press-ml" title="可做中、大胎">中大</span>}</span>
          </button>
        ))}
      </div>
      <div className="mlines">
        {LINES.filter((ln) => ln !== 'CURE').map((ln) => (
          <div className="mline" key={ln} data-line={ln}>
            <span className={`mline-k ln-${ln}`}>{LINE_SHORT[ln]}{ln.length === 1 ? ' 線' : ''}</span>
            <div className="mline-items">
              {machines.filter((m) => m.line === ln).map((m) => {
                const dq = dqText(m.machine_id)
                return (
                  <button key={m.machine_id} className={`mdot st-${m.status}${dq ? ' dq' : ''}`} onClick={() => onMachine(m.machine_id)}
                    title={`${machineLabel(m.machine_id)}・${STATUS_LABEL[m.status]}${m.temp_c != null ? `・${m.temp_c}°C` : ''}${dq}`}
                    aria-label={`${machineLabel(m.machine_id)} ${STATUS_LABEL[m.status]}${dq}`} data-machine={m.machine_id}>
                    <i className="dot" />{m.short_name}{dq && <span className="dq-tag" aria-hidden>資料</span>}
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>
      <p className="muted small foot">點機台看溫度、壓力、時間、水流{bad.length > 0 && <>・<span className="dq-tag">資料</span> = 驗證層攔下壞資料</>}</p>
    </Panel>
  )
}

/** Rebuild ③'s title and detail from the live event, so the panel never contradicts the tiles or the phone:
 *  the wake-time reading is labelled as such and the live value comes from the temperature tile.
 *  After a replan the round-2 approval title (e.g. 7 號機停用，重排中) leads. */
function focusView(focus: FocusPanel, ev: Event | null, reqs: ApprovalRequest[]): { title: string; detail: string } {
  // Only the server-built panel freezes the wake-time anomaly message; a live-built detail (mock) is used as-is.
  if (!ev || !ev.anomaly.message || !focus.detail.startsWith(ev.anomaly.message)) return { title: focus.title, detail: focus.detail }
  const base = focus.detail.split('；原因：')[0]
  const temp = focus.tiles.find((t) => t.label === '溫度')
  const detail = `喚醒時（${fmtHM(ev.anomaly.ts)}）：${base}${temp && temp.value !== '—' ? `。現在：${temp.value}` : ''}`
  const rejected = ev.hypotheses.filter((h) => h.status === 'rejected')
  const latestRound = reqs.filter((r) => r.event_id === ev.id).sort((a, b) => b.round - a.round || (a.created_at < b.created_at ? 1 : -1))[0]
  const title = rejected.length > 0 && latestRound && latestRound.round > 1
    ? `${machineLabel(ev.machine_id)}：${latestRound.title}`
    : focus.title
  return { title, detail }
}

/** "supervisor" is the API default for a phone decision; show the role in Chinese. */
function whoLabel(by: string): string {
  return !by || by === 'supervisor' ? '主管' : by
}

/** Presses cure for 24–34 h: show the server's minute count as hours. */
function tileValue(label: string, value: string, machineId: string): string {
  const m = label === '時間' && machineId.startsWith('CURE-') ? /^(\d+(?:\.\d+)?)分$/.exec(value) : null
  return m ? `${(Number(m[1]) / 60).toFixed(1)} h` : value
}
