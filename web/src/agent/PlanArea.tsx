// 計劃 area (right-bottom of the home screen): the plan for the item selected in the waiting list.
// A pending approval shows the decision card (problem, delivery impact, who does what, option picker, 核准 / 討論);
// work in progress shows its tasks and how it will be verified; a delivery risk shows the order and asks the AI to
// replan. Evidence (readings, tool calls) is not repeated here: it is in the conversation on the left.
import { useEffect } from 'react'
import type { Event } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { OrderCard } from '../components/OrderCard'
import { warningsFor } from '../components/orderModel'
import { machineLabel } from '../factory'
import { plain } from '../plain'
import { TOOL_LABEL, stepLabel } from '../stepText'
import { TaskList } from '../components/TaskList'
import { SafetyCaseCard } from '../components/Resolve'
import { useWarRoom } from '../useWarRoom'
import { ApprovalVisual } from './ApprovalVisual'
import { ResultStrip } from './ResultStrip'
import type { HomeItem } from './homeItems'
import { expectationText, type Item } from './transcript'
import { REPLAN_HINT, setReplan, useReplans, type Replan } from './replanStore'

const STEP_STATE: Record<string, string> = { pending: '等待', running: '執行中', ok: '完成', failed: '失敗', skipped: '略過', awaiting_approval: '待核准', rejected: '未執行' }

export function PlanArea({ sel, ev, items, onDiscuss, onAsk, onCommand, opened = true }: {
  sel: HomeItem | null; ev: Event | null; items: Item[]; opened?: boolean
  onDiscuss: () => void; onAsk: () => void; onCommand: (text: string) => void
}) {
  const { state, transport } = useWarRoom()
  const replans = useReplans()
  // issue #54 (CJ: 「下方應該要有核准，核准完要請主 agent 派一個 subagent 完成任務並回報」): an item without a plan gets
  // one by itself — the 主 agent asks the order subagent to draft it as soon as the item is opened; no button needed
  // only once the supervisor opens the item (a default pick on page load never starts a run nobody asked for)
  const autoOrder = opened && sel?.orderId && !replans[sel.orderId] && transport?.command ? sel.orderId : null
  // a plan already asked for this order (another tab, a reload) is adopted, never asked for twice
  const prior = autoOrder ? [...(state.snapshot?.events ?? [])].filter((e) => e.source === 'supervisor' && (e.command ?? '').startsWith(`${autoOrder} `))
    .sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))[0] : undefined
  useEffect(() => {
    if (!autoOrder || !transport) return
    if (prior) {
      const done = prior.state === 'closed' || prior.state === 'safe_stop'
      setReplan(autoOrder, done ? { state: 'closed', eventId: prior.id, outcome: prior.outcome } : { state: 'planning', eventId: prior.id })
    } else void startPlan(transport, autoOrder)
  }, [autoOrder, transport, prior])
  if (!sel) return null
  const sc = sel.caseId ? (state.snapshot?.safety_cases ?? []).find((c) => c.id === sel.caseId) : undefined
  if (sc) {
    return (
      <section className="plan-area panel plan-other" aria-label="計劃" data-testid="plan-area">
        <div className="po-body"><SafetyCaseCard c={sc} /></div>
      </section>
    )
  }
  if (sel.req && ev) {
    return (
      <section className="plan-area" aria-label="計劃" data-testid="plan-area">
        <ApprovalVisual key={sel.req.id} req={sel.req} ev={ev} items={items} layout="plan" onDiscuss={onDiscuss} />
      </section>
    )
  }
  if (sel.orderId) {
    const orderId = sel.orderId
    const rp = replans[orderId]
    void onCommand
    const o = state.world?.orders.items.find((x) => x.id === sel.orderId)
    const jobs = (state.schedule?.schedule.jobs ?? []).filter((j) => j.order_id === sel.orderId)
    const now = Date.parse(state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? '')
    return (
      <section className="plan-area panel plan-other" aria-label="計劃" data-testid="plan-area">
        <div className="po-body">
          <span className="hl-pill tone-risk">交期預警</span>
          <h3 className="apv-title">{sel.title}</h3>
          <p className="apv-reason">{sel.sub}</p>
          {o ? <OrderCard o={o} jobs={jobs} now={now} warnings={warningsFor(o.id, state.schedule?.schedule.warnings ?? [])} />
            : <p className="muted">工廠紀錄裡找不到 {sel.orderId} 的明細。</p>}
        </div>
        <footer className="apv-act">
          {sel.checked
            ? <p className="apv-next" data-testid="plan-prechecked"><b>{sel.checked.split('，建議')[0]}</b>{sel.checked.includes('，建議') ? `，建議${sel.checked.split('，建議')[1]}` : ''}</p>
            : <ReplanStatus r={rp} />}{/* the backend pre-plans it (Runtime._preplan_orders): a progress line, never 「點左邊這一項」 */}
          <div className="apv-btns">
            {!sel.checked && <button className="btn-approve" disabled title="Agent Nemo 的計劃還沒好；好了會自動切到它的核准卡" data-testid="plan-approve-wait">核准</button>}
            <button className={sel.checked ? 'btn-approve' : 'btn-sec'} onClick={onDiscuss} data-testid="plan-discuss">討論</button>
            {transport?.command && (rp?.state === 'failed' || rp?.state === 'closed') &&
              <button className="btn-ghost" onClick={() => void startPlan(transport, orderId)} data-testid="plan-again">再規劃一次</button>}
          </div>
        </footer>
      </section>
    )
  }
  if (ev) {
    const steps = ev.steps
    const hyp = ev.plan?.hypothesis ?? null
    const exp = hyp ? expectationText(hyp) : null
    const closed = ev.state === 'closed' || ev.state === 'safe_stop'
    const tasks = (state.world?.tasks?.items ?? []).filter((t) => t.event_id === ev.id)
    return (
      <section className="plan-area panel plan-other" aria-label="計劃" data-testid="plan-area">
        <div className="po-body">
          <ResultStrip ev={ev} />
          <div className="apv-tags"><span className="apv-round">{ev.source === 'supervisor' ? '主管指令' : machineLabel(ev.machine_id)}</span><span className={`state-pill st-${ev.state}`}>{STATE_LABEL[ev.state]}</span></div>
          <h3 className="apv-title">{plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message)}</h3>
          {ev.outcome && <p className="apv-reason">結果：{plain(ev.outcome)}</p>}
          {tasks.length > 0 && (
            <div className="apv-b">
              <h4 className="apv-bh">人員執行（核准後由程式派工）<small>{tasks.filter((t) => t.status === '完成').length}/{tasks.length} 完成</small></h4>
              <TaskList tasks={tasks} eventId={ev.id} testId="event-tasks" />
            </div>
          )}
          {steps.length > 0 && (
            <ul className="who-list task-list" data-testid="plan-tasks">
              {steps.map((s) => (
                <li key={s.step.id} className={`ts-${s.state}`}><b>{STEP_STATE[s.state] ?? s.state}</b><span>{stepLabel(s.step.id)} {TOOL_LABEL[s.step.tool] ?? s.step.tool}・{plain(s.step.purpose)}</span></li>
              ))}
            </ul>
          )}
          {!closed && exp && <div className="apv-b"><h4 className="apv-bh">怎麼確認做完</h4><p className="impact-line">程式比對讀值：{exp}；達標即自動結案，不達標換下一個假設。</p></div>}
          {!steps.length && !closed && <p className="muted">Agent Nemo 正在規劃；有需要你決定的會變成待核准。</p>}
        </div>
        <footer className="apv-act"><div className="apv-btns">
          <button className="btn-sec" onClick={onAsk}>問 AI</button>
          <span className="apv-hint">{ev.state === 'safe_stop' ? '安全停止：用上面的按鈕走向解決' : closed ? '已結案，Agent Nemo 的回報在右邊' : '執行中，不需要再核准'}</span>
        </div></footer>
      </section>
    )
  }
  return (
    <section className="plan-area panel plan-other" aria-label="計劃" data-testid="plan-area">
      <div className="po-body"><h3 className="apv-title">{sel.title}</h3><p className="apv-reason">{sel.sub}</p><p className="muted small">安全核心與驗證層直接處理，沒有喚醒 AI，不需要核准。</p></div>
    </section>
  )
}

/** Where a ② item's replan stands: nothing yet (the one-line hint), planning, or failed. */
function ReplanStatus({ r }: { r: Replan | undefined }) {
  if (!r) return <p className="apv-next" data-testid="replan-hint">{REPLAN_HINT}</p>
  if (r.state === 'failed') return <p className="apv-next err" data-testid="replan-status">Agent Nemo 沒有開始規劃：{r.error}</p>
  if (r.state === 'closed') return <p className="apv-next" data-testid="replan-status">Agent Nemo 規劃完，沒有需要核准的變更：{plain(r.outcome ?? '已結案')}</p>
  if (r.state === 'ready') return <p className="apv-next" data-testid="replan-status">Agent Nemo 的計劃好了：核准卡已自動選取</p>
  return <p className="apv-next live" role="status" aria-live="polite" data-testid="replan-status"><i className="spin" />Agent Nemo 規劃中…{r.eventId ? `（${r.eventId}）` : ''}好了會自動切到核准卡</p>
}

/** Agent Nemo drafts a plan for an at-risk order (POST /api/command → one supervisor-command event). */
async function startPlan(transport: { command?: (t: string) => Promise<{ event_id: string; reply: string }> }, orderId: string) {
  if (!transport.command) return
  setReplan(orderId, { state: 'sending', eventId: null, error: null, since: Date.now() })
  try {
    const r = await transport.command(`${orderId} 可能晚交，請重排保交期`)
    setReplan(orderId, r.event_id ? { state: 'planning', eventId: r.event_id } : { state: 'failed', error: r.reply })
  } catch (e) { setReplan(orderId, { state: 'failed', error: String(e).replace(/^Error: /, '') }) }
}
