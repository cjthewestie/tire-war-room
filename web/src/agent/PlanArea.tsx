// 計劃 area (right-bottom of the home screen): the plan for the item selected in the waiting list.
// A pending approval shows the decision card (problem, delivery impact, who does what, option picker, 核准 / 討論);
// work in progress shows its tasks and how it will be verified; a delivery risk shows the order and asks the AI to
// replan. Evidence (readings, tool calls) is not repeated here: it is in the conversation on the left.
import { useEffect } from 'react'
import type { Event } from '../contracts'
import { CH_LABEL, REPAIR_KINDS } from '../aiStatus'
import { OrderCard } from '../components/OrderCard'
import { warningsFor } from '../components/orderModel'
import { machineLabel } from '../factory'
import { plain } from '../plain'
import { TOOL_LABEL, resultText, stepLabel } from '../stepText'
import { TaskList } from '../components/TaskList'
import { currentRoundTools, planNodes } from './approvalView'
import { useWarRoom } from '../useWarRoom'
import { ApprovalVisual, PlanFlow } from './ApprovalVisual'
import { latestDecision, useHistory } from '../history'
import { ResultStrip } from './ResultStrip'
import { ended, outcomeText, type HomeItem } from './homeItems'
import { expectationText, type Item } from './transcript'
import { REPLAN_HINT, setReplan, useReplans, type Replan } from './replanStore'

const STEP_STATE: Record<string, string> = { pending: '等待', running: '執行中', ok: '完成', failed: '失敗', skipped: '略過', awaiting_approval: '待核准', rejected: '未執行' }

export function PlanArea({ sel, ev, items, onDiscuss, onAsk, onCommand, opened = true }: {
  sel: HomeItem | null; ev: Event | null; items: Item[]; opened?: boolean
  onDiscuss: () => void; onAsk: () => void; onCommand: (text: string) => void
}) {
  const { state, transport } = useWarRoom()
  const replans = useReplans()
  const hist = useHistory()
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
  if (sel.req && ev) {
    return (
      <section className="plan-area" aria-label="計畫" data-testid="plan-area">
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
      <section className="plan-area panel plan-other" aria-label="計畫" data-testid="plan-area">
        <div className="po-body">
          <span className="hl-pill tone-risk">交期預警</span>
          <h3 className="apv-title">{sel.title}</h3>
          <p className="apv-reason">{sel.sub}</p>
          {o ? <OrderCard o={o} jobs={jobs} now={now} warnings={warningsFor(o.id, state.schedule?.schedule.warnings ?? [])} />
            : <p className="muted">工廠紀錄裡找不到 {sel.orderId} 的明細。</p>}
        </div>
        <footer className="apv-act">
          <ReplanStatus r={rp} />{/* the backend pre-plans it (Runtime._preplan_orders): a progress line, never 「點左邊這一項」 */}
          <div className="apv-btns">
            <button className="btn-approve" disabled title="Agent Nemo 的計畫還沒好；好了會自動跳到它的核准卡" data-testid="plan-approve-wait">核准</button>
            <button className="btn-sec" onClick={onDiscuss} data-testid="plan-discuss">討論</button>
            {transport?.command && (rp?.state === 'failed' || rp?.state === 'closed') &&
              <button className="btn-ghost" onClick={() => void startPlan(transport, orderId)} data-testid="plan-again">再規劃一次</button>}
          </div>
        </footer>
      </section>
    )
  }
  if (ev) {
    const steps = ev.steps
    const ok = steps.filter((s) => s.state === 'ok').length
    const hyp = ev.plan?.hypothesis ?? null
    const exp = hyp ? expectationText(hyp) : null
    const closed = ev.state === 'closed' || ev.state === 'safe_stop'
    const nodes = planNodes(ev, currentRoundTools(items))
    const lines: Record<string, string> = {}
    for (const sr of ev.steps) if (sr.result?.ok) { try { lines[sr.step.id] = plain(resultText(sr.step.tool, sr.step.args, sr.result.data)) } catch { /* purpose instead */ } }
    const tasks = (state.world?.tasks?.items ?? []).filter((t) => t.event_id === ev.id)
    const repairing = tasks.some((t) => REPAIR_KINDS.has(t.kind) && t.status !== '完成')
    // the stage in the supervisor's words (結案 / 驗證 are state-machine words): the same as the list and the result strip
    const stage = ev.state === 'closed' ? ended(ev).pill : ev.state === 'safe_stop' ? '安全停止'
      : ev.repaired_at ? '維修完成・回溫中' : repairing ? '維修中' : ev.state === 'verify' ? '讀值確認中' : ev.state === 'execute' ? '執行中' : 'Agent Nemo 規劃中'
    // the result strip already says how it ended once the supervisor decided: no second 結果 line
    const decided = !!latestDecision(hist, ev.id)
    return (
      <section className="plan-area panel plan-other" aria-label="計畫" data-testid="plan-area">
        <div className="po-body">
          <ResultStrip ev={ev} />
          <div className="apv-tags"><span className="apv-round">{ev.source === 'supervisor' ? '主管指令' : machineLabel(ev.machine_id)}</span><span className={`state-pill st-${ev.state}`}>{stage}</span></div>
          <h3 className="apv-title">{plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message)}</h3>
          {ev.outcome && !decided && <p className="apv-reason">結果：{outcomeText(ev)}</p>}
          {tasks.length > 0 && (
            <div className="apv-b">
              <h4 className="apv-bh">現場工作（核准後才派人）<small>{tasks.filter((t) => t.status === '完成').length}/{tasks.length} 完成</small></h4>
              <TaskList tasks={tasks} eventId={ev.id} testId="event-tasks" />
            </div>
          )}
          {!closed && exp && hyp?.expectation && (
            <div className="apv-b"><h4 className="apv-bh">怎麼確認修好</h4>
              <p className="impact-line">{exp.replace('分鐘內', `分鐘內${CH_LABEL[hyp.expectation.channel]}`)}，系統用讀值自動確認；沒達到就換下一個判斷。</p></div>
          )}
          {nodes.length > 0 && (
            <details className="apv-b apv-steps" data-testid="plan-steps">
              <summary className="apv-bh">Agent Nemo 的計畫步驟<small>{ok}/{steps.length} 步完成</small></summary>
              <PlanFlow nodes={nodes} board lines={lines} />
            </details>
          )}
          {steps.length > 0 && !nodes.length && (
            <ul className="who-list task-list" data-testid="plan-tasks">
              {steps.map((s) => (
                <li key={s.step.id} className={`ts-${s.state}`}><b>{STEP_STATE[s.state] ?? s.state}</b><span>{stepLabel(s.step.id)} {TOOL_LABEL[s.step.tool] ?? s.step.tool}・{plain(s.step.purpose)}</span></li>
              ))}
            </ul>
          )}
          {!steps.length && !closed && <p className="muted">Agent Nemo 正在規劃；有需要你決定的會變成待核准。</p>}
        </div>
        <footer className="apv-act"><div className="apv-btns">
          <button className="btn-sec" onClick={onAsk}>問 Agent Nemo</button>
          <span className="apv-hint">{ev.state === 'safe_stop' ? '已安全停止，請到現場處理' : closed ? `${ended(ev).pill}；Agent Nemo 異常報告在對話區` : '執行中，不需要再核准'}</span>
        </div></footer>
      </section>
    )
  }
  return (
    <section className="plan-area panel plan-other" aria-label="計畫" data-testid="plan-area">
      <div className="po-body"><h3 className="apv-title">{sel.title}</h3><p className="apv-reason">{sel.sub}</p><p className="muted small">安全核心與驗證層直接處理，沒有交給 Agent Nemo，不需要核准。</p></div>
    </section>
  )
}

/** Where a 交期風險 item's replan stands: nothing yet (the one-line hint), planning, or failed. */
function ReplanStatus({ r }: { r: Replan | undefined }) {
  if (!r) return <p className="apv-next" data-testid="replan-hint">{REPLAN_HINT}</p>
  if (r.state === 'failed') return <p className="apv-next err" data-testid="replan-status">Agent Nemo 沒有開始規劃：{r.error}</p>
  if (r.state === 'closed') return <p className="apv-next" data-testid="replan-status">Agent Nemo 規劃完，沒有需要核准的變更{r.outcome ? `：${plain(r.outcome)}` : ''}</p>
  if (r.state === 'ready') return <p className="apv-next" data-testid="replan-status">Agent Nemo 的計畫好了：核准卡已自動選取</p>
  return <p className="apv-next live" role="status" aria-live="polite" data-testid="replan-status"><i className="spin" />Agent Nemo 規劃中，好了會自動跳到核准卡</p>
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
