// 計劃 area (right-bottom of the home screen): the plan for the item selected in the waiting list.
// A pending approval shows the decision card (problem, delivery impact, who does what, option picker, 核准 / 討論);
// work in progress shows its tasks and how it will be verified; a delivery risk shows the order and asks the AI to
// replan. Evidence (readings, tool calls) is not repeated here: it is in the conversation on the left.
import type { Event } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { OrderCard } from '../components/OrderCard'
import { warningsFor } from '../components/orderModel'
import { machineLabel } from '../factory'
import { plain } from '../plain'
import { TOOL_LABEL, resultText, stepLabel } from '../stepText'
import { TaskList } from '../components/TaskList'
import { currentRoundTools, planNodes } from './approvalView'
import { useWarRoom } from '../useWarRoom'
import { ApprovalVisual, PlanFlow } from './ApprovalVisual'
import type { HomeItem } from './homeItems'
import { expectationText, type Item } from './transcript'

const STEP_STATE: Record<string, string> = { pending: '等待', running: '執行中', ok: '完成', failed: '失敗', skipped: '略過', awaiting_approval: '待核准', rejected: '未執行' }

export function PlanArea({ sel, ev, items, onDiscuss, onAsk, onCommand }: {
  sel: HomeItem | null; ev: Event | null; items: Item[]
  onDiscuss: () => void; onAsk: () => void; onCommand: (text: string) => void
}) {
  const { state } = useWarRoom()
  if (!sel) return null
  if (sel.req && ev) {
    return (
      <section className="plan-area" aria-label="計劃" data-testid="plan-area">
        <ApprovalVisual key={sel.req.id} req={sel.req} ev={ev} items={items} layout="plan" onDiscuss={onDiscuss} />
      </section>
    )
  }
  if (sel.orderId) {
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
          <p className="muted small">這是排程與交期比對出的預警，還沒有 AI 計劃。要處理，請 AI 重排；結果會變成一張待核准的計劃。</p>
        </div>
        <footer className="apv-act"><div className="apv-btns">
          <button className="btn-approve" onClick={() => onCommand(`${sel.orderId} 可能晚交，請重排保交期`)} data-testid="plan-replan">請 AI 重排</button>
          <button className="btn-sec" onClick={onAsk}>問 AI</button>
        </div></footer>
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
    return (
      <section className="plan-area panel plan-other" aria-label="計劃" data-testid="plan-area">
        <div className="po-body">
          <div className="apv-tags"><span className="apv-round">{ev.source === 'supervisor' ? '主管指令' : machineLabel(ev.machine_id)}</span><span className={`state-pill st-${ev.state}`}>{STATE_LABEL[ev.state]}</span></div>
          <h3 className="apv-title">{plain(ev.alert_card?.title ?? ev.command ?? ev.anomaly.message)}</h3>
          {ev.outcome && <p className="apv-reason">結果：{plain(ev.outcome)}</p>}
          {nodes.length > 0 && (
            <div className="apv-b">
              <h4 className="apv-bh">計劃板<small>{ok}/{steps.length} 步完成</small></h4>
              <PlanFlow nodes={nodes} board lines={lines} />
            </div>
          )}
          {tasks.length > 0 && (
            <div className="apv-b">
              <h4 className="apv-bh">人員執行（核准後由程式派工）<small>{tasks.filter((t) => t.status === '完成').length}/{tasks.length} 完成</small></h4>
              <TaskList tasks={tasks} eventId={ev.id} testId="event-tasks" />
            </div>
          )}
          {steps.length > 0 && !nodes.length && (
            <ul className="who-list task-list" data-testid="plan-tasks">
              {steps.map((s) => (
                <li key={s.step.id} className={`ts-${s.state}`}><b>{STEP_STATE[s.state] ?? s.state}</b><span>{stepLabel(s.step.id)} {TOOL_LABEL[s.step.tool] ?? s.step.tool}・{plain(s.step.purpose)}</span></li>
              ))}
            </ul>
          )}
          {!closed && exp && <div className="apv-b"><h4 className="apv-bh">怎麼確認做完</h4><p className="impact-line">程式比對讀值：{exp}；達標即自動結案，不達標換下一個假設。</p></div>}
          {!steps.length && !closed && <p className="muted">AI 指揮官正在規劃；有需要你決定的會變成待核准。</p>}
        </div>
        <footer className="apv-act"><div className="apv-btns">
          <button className="btn-sec" onClick={onAsk}>問 AI</button>
          <span className="apv-hint">{closed ? '已結案，紀錄在左邊對話台' : '執行中，不需要再核准'}</span>
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
