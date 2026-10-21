// Visual approval card (issue #8, part B): the plan as a flow diagram, the schedule before/after,
// the staffing options side by side, the tools that ran and the conclusion — then 核准 or 討論 (no separate 修改/駁回).
import { useId, useMemo, useState } from 'react'
import type { ApprovalRequest, DecisionKind, Event, ScheduleJob } from '../contracts'
import { machineLabel } from '../factory'
import { factoryMidnight, fmtHM, fmtMD, ms } from '../format'
import { RISK_PLAIN, hypName, plain } from '../plain'
import { TOOL_LABEL, NOTIFY_LABEL, resultText } from '../stepText'
import { useWarRoom } from '../useWarRoom'
import { useWidth } from '../components/common'
import {
  NODE_LABEL, cureChange, currentRoundTools, deliveryFacts, optionViews, planNodes, proposedVersion, scheduleDiff, toolUses,
  type PlanNode, type ScheduleDiff, type ToolUse,
} from './approvalView'
import { ToolIcon } from './ToolIcon'
import { OptionGuideNote } from '../OptionGuideNote'
import { expectationText, type Item, type ToolItem } from './transcript'
import { useTranscript, useVersions } from './useTranscript'
import './agent.css'

const ROUND = ['', '第一輪', '第二輪', '第三輪', '第四輪']

/** The visual card for one pending request, with its event's transcript (tool counts) looked up here. */
export function PendingApproval({ req, onAgent }: { req: ApprovalRequest; onAgent?: () => void }) {
  const { state } = useWarRoom()
  const ev = state.snapshot?.events.find((e) => e.id === req.event_id) ?? null
  const { items } = useTranscript(ev)
  return <ApprovalVisual req={req} ev={ev} items={items} onAgent={onAgent} onDiscuss={onAgent} />
}

/** `plan` = the 計劃 area of the home screen (UX brief, CJ 2026-10-20): problem in one line, delivery impact, who
 *  does what after approval, the option picker when there is a choice, and only two actions — 核准 and 討論
 *  (CJ: 「agent 計劃就只有核准跟討論」; changing or rejecting a plan happens by talking to the agent).
 *  `wide` = 產線監控: the same decision plus the evidence (plan diagram, schedule before/after, tools). */
export function ApprovalVisual({ req, ev, items, onAgent, onDiscuss, layout = 'wide' }: {
  req: ApprovalRequest; ev: Event | null; items: Item[]; onAgent?: () => void; onDiscuss?: () => void; layout?: 'wide' | 'plan'
}) {
  const { transport, state } = useWarRoom()
  const versions = useVersions()
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const lanDown = state.snapshot?.header.network === 'lan_down'
  const tools = useMemo(() => items.filter((i): i is ToolItem => i.k === 'tool'), [items])
  const nodes = useMemo(() => planNodes(ev, currentRoundTools(items)), [ev, items])
  const cure = useMemo(() => cureChange(ev), [ev])
  const diff = useMemo(() => scheduleDiff(versions, proposedVersion(ev, req), cure, now), [versions, ev, req, cure, now])
  const opts = useMemo(() => optionViews(req, ev), [req, ev])
  const uses = useMemo(() => toolUses(ev, tools), [ev, tools])
  const facts = deliveryFacts(ev)

  const base = (req.options.find((o) => o.recommended) ?? req.options[0])?.id ?? null
  const [pick, setPick] = useState<string | null>(null)
  const chosen = pick ?? base
  const chosenOpt = req.options.find((o) => o.id === chosen)
  const changesOption = chosen != null && chosen !== base
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  // 核准 only: the chosen option (or the system's) executes; a different option than the system's is recorded as such.
  // Changing or rejecting the plan is done by talking (討論 → POST /api/events/{id}/discuss replans and issues v2).
  const send = async () => {
    if (!transport) return
    if (req.options.length > 0 && !chosen) { setErr('請先選擇方案'); return }
    const d: { decision: DecisionKind; option_id: string | null; reason: string } = {
      decision: 'approve', option_id: chosen,
      reason: changesOption ? `否決「${req.options.find((o) => o.id === base)?.label}」，改選「${chosenOpt?.label}」` : chosenOpt ? `核准「${chosenOpt.label}」` : '戰情室核准',
    }
    setBusy(true); setErr(null)
    try {
      await transport.decide({ request_id: req.id, ...d, modified_args: null, decided_by: '主管', via: lanDown ? 'touchscreen' : 'dashboard' })
      setDone(`已核准${d.option_id ? ` ${d.option_id}` : ''}`)
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }

  const hyp = ev?.plan?.hypothesis ?? null
  const afterSteps = nodes.filter((n) => n.status === 'after')
  const activeV = diff?.before.version ?? versions.find((v) => v.status === 'active')?.version ?? null
  const expires = req.expires_at ? `${fmtMD(req.expires_at)} ${fmtHM(req.expires_at)} 前` : null
  // one delivery line for all options when the solver gives no per-option figure
  const dLine = deliveryLine(req.delivery_impact, facts)
  const planLayout = layout === 'plan'

  const plan = (n: number) => (
    <Block key="plan" n={n} title="處理計畫" hint={nodes.length ? `${nodes.length} 步・箭頭＝相依` : undefined}>
      {nodes.length ? <PlanFlow nodes={nodes} /> : <p className="muted small">尚無計畫步驟資料</p>}
    </Block>
  )
  const sched = (n: number) => (
    <Block key="sched" n={n} title="排程前後對照" hint={diff ? `第 ${diff.before.version} 版 → 第 ${diff.after.version} 版` : undefined}>
      {diff ? <ScheduleCompare diff={diff} now={now} /> : <p className="muted small">{versions.length ? '這一輪沒有可對照的排程版本' : '讀取排程版本中…'}</p>}
      {!diff && cure && <p className="apv-chips"><span className="chg cure">{machineLabel(cure.press)}硫化 {cure.from} h → {round1(cure.to)} h</span></p>}
    </Block>
  )
  const options = (n: number) => opts.length > 0 && (
    <Block key="opts" n={n} title="人力方案" hint="點選一個方案">
      <OptionGuideNote a={req} ev={ev} />
      <div className="apv-opts" role="group" aria-label="人力方案">
        {opts.map((o) => {
          const on = chosen === o.opt.id
          return (
            <button key={o.opt.id} aria-pressed={on} className={`opt ${on ? 'on' : ''}`} disabled={busy || !!done}
              onClick={() => setPick(o.opt.id)} data-testid={`option-${o.opt.id}`}>
              <span className="opt-h"><b className="opt-id">{o.opt.id}</b><span className="opt-l">{o.opt.label}</span>
                {o.opt.recommended && <span className="opt-rec">系統建議</span>}</span>
              <dl className="opt-dl">
                {o.who && <><dt>人員</dt><dd>{o.who}</dd></>}
                {o.shift && <><dt>班別</dt><dd>{o.shift}</dd></>}
                {o.people != null && <><dt>人數</dt><dd>{o.people} 人</dd></>}
                {o.teams.length > 0 && <><dt>通知</dt><dd>{o.kind && NOTIFY_LABEL[o.kind] ? `${NOTIFY_LABEL[o.kind]} → ` : ''}{o.teams.join('、')}</dd></>}
                {!o.who && o.message && <><dt>內容</dt><dd>{plain(o.message)}</dd></>}
              </dl>
              <span className="opt-pick" aria-hidden>{on ? '● 已選' : '○ 選這個'}</span>
            </button>
          )
        })}
      </div>
      {dLine !== '—' && <p className="opt-shared" data-testid="option-delivery">交期（求解器結果，{opts.length > 1 ? '各方案相同' : '此方案'}）：{dLine}</p>}
    </Block>
  )
  const toolsUsed = (n: number) => (
    <Block key="tools" n={n} title="調用的工具" hint={uses.length ? `${uses.reduce((s, u) => s + u.calls, 0)} 次` : undefined}>
      {uses.length ? <ToolChips uses={uses} /> : <p className="muted small">尚無工具紀錄</p>}
    </Block>
  )
  const gated = (ev?.steps ?? []).filter((x) => x.state === 'awaiting_approval')
  const conclusion = (n: number) => (
    <Block key="concl" n={n} title="結論" className="apv-concl">
      <dl className="concl">
        <dt>為什麼</dt>
        <dd>
          {hyp ? <><b>{hypName(hyp.id)}</b>　{plain(hyp.statement)}</> : plain(req.reason)}
          {hyp && expectationText(hyp) && <span className="concl-sub">預期：{expectationText(hyp)}（程式會用讀值驗證）</span>}
          {ev?.plan?.rationale && <span className="concl-sub">{plain(ev.plan.rationale)}</span>}
        </dd>
        <dt>交期影響</dt>
        <dd>{dLine}{req.gaps.length > 0 && <span className="concl-sub">缺口：{req.gaps.map(plain).join('；')}</span>}</dd>
        <dt className="ok">核准後</dt>
        <dd>
          <ul className="concl-list">
            {(req.actions ?? []).filter((a) => !(chosenOpt && /^人力：依所選方案/.test(a))).map((a, i) => <li key={i}>{plain(a)}</li>)}
            {chosenOpt && <li>人力：{chosenOpt.id} {chosenOpt.label}</li>}
            {afterSteps.map((n) => <li key={n.id}>接著自動（R1）：{TOOL_LABEL[n.tool]}・{plain(n.purpose)}</li>)}
          </ul>
          {gated.length > 0 && (
            // the exact tool and args approving runs, from the event's steps (code), next to the AI's own words
            // above: title and rationale are model prose and could differ (CJ 2026-10-21: 「我有方法避免prompt injection嗎」)
            <details className="concl-sub apv-exec" data-testid="apv-exec">
              <summary>程式實際會執行（工具與參數）</summary>
              <ul>{gated.map((g) => <li key={g.step.id}><code>{g.step.id} {g.step.tool} {JSON.stringify(g.step.args)}</code></li>)}</ul>
            </details>
          )}
        </dd>
        <dt className="no">不同意時</dt>
        <dd>按「討論」跟 AI 說，AI 重排發新版；未核准前不執行{activeV != null ? `（排程維持第 ${activeV} 版）` : ''}。</dd>
      </dl>
    </Block>
  )
  // 對交期的影響: one row per order the proposed schedule changes (code-built diff of the two schedule versions)
  const impactRows = diff ? impactTable(diff, facts) : []
  const impact = (n: number) => (
    <Block key="impact" n={n} title="對交期的影響" className="apv-impact">
      {impactRows.length > 0 && (
        <table className="impact-t" data-testid="impact-table">
          <thead><tr><th>訂單</th><th>原本</th><th>改後</th><th>交期</th></tr></thead>
          <tbody>{impactRows.map((r) => (
            <tr key={r.order}><td><b>{r.order}</b></td><td>{r.before}</td><td>{r.after}</td><td className={r.late ? 'late' : r.ok ? 'ontime' : ''}>{r.verdict}</td></tr>
          ))}</tbody>
        </table>
      )}
      {!impactRows.length && cure && <p className="impact-line">{machineLabel(cure.press)}硫化 {cure.from} h → {round1(cure.to)} h</p>}
      <p className="impact-line">{dLine}{req.gaps.length > 0 && <span className="concl-sub">缺口：{req.gaps.map(plain).join('；')}</span>}</p>
    </Block>
  )
  // 核准後誰去做: what executes on approval (the card's actions, the chosen staffing option, the R1 steps after)
  const whoRows = [
    ...(req.actions ?? []).filter((a) => !(chosenOpt && /^人力：依所選方案/.test(a))).map((a) => splitAction(plain(a))),
    ...opts.filter((o) => o.opt.id === chosen).map((o) => ({ who: o.who ?? (o.teams.join('、') || '人力'), what: `${o.opt.id} ${o.opt.label}${o.shift ? `・${o.shift}` : ''}` })),
    ...afterSteps.map((n) => ({ who: '系統（R1 自動）', what: `${TOOL_LABEL[n.tool]}・${plain(n.purpose)}` })),
  ]
  const who = (n: number) => (
    <Block key="who" n={n} title="核准後誰去做" className="apv-who">
      {whoRows.length ? <ul className="who-list" data-testid="who-does-what">{whoRows.map((r, i) => <li key={i}><b>{r.who}</b><span>{r.what}</span></li>)}</ul>
        : <p className="muted small">這張卡沒有列出要執行的動作</p>}
    </Block>
  )
  const optionPick = (n: number) => opts.length > 1 && options(n)
  // 計劃板: the steps as cards, key result per card (from the tool results), arrows = depends_on
  const lines: Record<string, string> = {}
  for (const sr of ev?.steps ?? []) {
    if (sr.result?.ok) { try { lines[sr.step.id] = plain(resultText(sr.step.tool, sr.step.args, sr.result.data)) } catch { /* purpose shown instead */ } }
  }
  const changedIds = planLayout && ev ? changedSteps(ev.id, req.version ?? 1, nodes, lines) : undefined
  const boardBlock = (n: number) => (
    <Block key="board" n={n} title="計劃板" hint={nodes.length ? `${nodes.length} 步・箭頭＝相依${changedIds?.size ? `・本版改了 ${changedIds.size} 步` : ''}` : undefined}>
      {nodes.length ? <PlanFlow nodes={nodes} board lines={lines} changed={changedIds} /> : <p className="muted small">尚無計畫步驟資料</p>}
    </Block>
  )
  // plan: impact, who, option picker; wide (產線監控): plan + schedule | options + tools + conclusion
  const order = planLayout ? [boardBlock, impact, who, optionPick] : [plan, sched, options, toolsUsed, conclusion]
  let k = 0
  const blocks = order.map((f) => { const el = f(k + 1); if (el) k++; return el })

  return (
    <section className="apv" data-testid="approval-visual" data-layout={layout} aria-label={`核准：${plain(req.title)}`}>
      <header className="apv-h">
        <div className="apv-tags">
          <span className="apv-main" data-testid="main-plan-tag">Agent Nemo 的計劃</span>
          <span className={`apv-risk r-${req.risk}`}>{req.risk}・{RISK_PLAIN[req.risk]}</span>
          <span className="apv-round">{ROUND[req.round] ?? `第 ${req.round} 輪`}核准</span>
          {(req.version ?? 1) > 1 && <span className="apv-ver" data-testid="card-version-tag">v{req.version}</span>}
          {ev && <span className="apv-round">{machineLabel(ev.machine_id)}</span>}
          {expires && <span className="apv-exp">{expires}決定</span>}
          {onAgent && <button className="btn-ghost apv-link" onClick={onAgent}>看 AI 怎麼想 →</button>}
        </div>
        <h3 className="apv-title">{plain(req.title)}</h3>
        <p className="apv-reason">{plain(req.reason)}</p>
        {(req.version ?? 1) > 1 && (
          <div className="apv-changed" data-testid="card-changes">
            <b>與 v{(req.version ?? 2) - 1} 相比</b>
            {(req.changes ?? []).length ? <ul>{(req.changes ?? []).map((c, i) => <li key={i}>{plain(c)}</li>)}</ul> : <span>排程內容相同</span>}
          </div>
        )}
        {(req.constraints ?? []).length > 0 && (
          <p className="apv-chips" data-testid="card-constraints"><small>主管條件：</small>{(req.constraints ?? []).map((c) => <span key={c} className="chip-c">{c}</span>)}</p>
        )}
      </header>

      {planLayout
        ? <div className="apv-col apv-plan-body">{blocks}</div>
        : (
          <div className="apv-grid">
            <div className="apv-col">{blocks.slice(0, 2)}</div>
            <div className="apv-col">{blocks.slice(2)}</div>
          </div>
        )}

      <footer className="apv-act">
        {done ? <p className="apv-done" role="status">{done}・執行中；決定已寫入稽核日誌</p> : <>
          {err && <p className="err">{err}</p>}
          <div className="apv-btns">
            <button className="btn-approve" disabled={busy || (req.options.length > 0 && !chosen)} onClick={() => void send()} data-testid="visual-approve">
              {busy ? '送出中…' : `核准${chosen && req.options.length > 1 ? `（${chosen}）` : ''}`}
            </button>
            {onDiscuss && <button className="btn-sec" disabled={busy} onClick={onDiscuss} data-testid="visual-discuss">討論</button>}
            <span className="apv-hint">核准後才執行；要改，按「討論」</span>
          </div>
        </>}
      </footer>
    </section>
  )
}

function round1(n: number): number { return Math.round(n * 10) / 10 }
function clip(t: string, n: number): string { const a = Array.from(t); return a.length > n ? `${a.slice(0, Math.max(1, n - 1)).join('')}…` : t }

// which board cards a new version of the card changed: remembered per event across versions (cards remount per id)
const boardMemory = new Map<string, { version: number; sigs: Map<string, string>; changed: Set<string> }>()
function changedSteps(evId: string, version: number, nodes: PlanNode[], lines: Record<string, string>): Set<string> {
  const sigs = new Map(nodes.map((n) => [n.id, `${n.tool}|${plain(n.purpose)}|${lines[n.id] ?? ''}`]))
  const mem = boardMemory.get(evId)
  if (mem && mem.version === version) return mem.changed
  const changed = new Set<string>()
  if (mem && version > mem.version) for (const [id, sig] of sigs) if (mem.sigs.get(id) !== sig) changed.add(id)
  boardMemory.set(evId, { version, sigs, changed })
  return changed
}

interface ImpactRow { order: string; before: string; after: string; verdict: string; late: boolean; ok: boolean }
function impactTable(diff: ScheduleDiff, facts: { met: boolean | null; late: string[] }): ImpactRow[] {
  const byOrder = new Map<string, { before: ScheduleJob | null; after: ScheduleJob }>()
  for (const c of diff.changes) {
    if (c.kind !== 'moved' && c.kind !== 'cure') continue
    const cur = byOrder.get(c.job.order_id)
    if (!cur || ms(c.job.demold) > ms(cur.after.demold)) byOrder.set(c.job.order_id, { before: c.before, after: c.job })
  }
  const at = (j: ScheduleJob) => `${Number(j.press_id.slice(5))} 號・${fmtMD(j.demold)} ${fmtHM(j.demold)} 出模`
  return [...byOrder.entries()].map(([order, { before, after }]) => {
    const late = facts.late.some((l) => l.includes(order))
    return { order, before: before ? at(before) : '—', after: at(after), late, ok: !late && facts.met === true, verdict: late ? '延誤' : facts.met === true ? '準時' : '—' }
  })
}

/** Who carries out one approved action: the line's own label ("人力：…", "通知：…"), else by what it changes. */
function splitAction(a: string): { who: string; what: string } {
  const m = /^([^：:（(]{1,8})[：:]\s*(.+)$/.exec(a)
  if (m) return { who: m[1], what: m[2] }
  if (a.includes('排程')) return { who: '排程（系統生效）', what: a }
  if (a.includes('硫化')) return { who: '硫化機（系統設定）', what: a }
  return { who: '系統', what: a }
}

function deliveryLine(impact: string, facts: { met: boolean | null; late: string[] }): string {
  const bits = [plain(impact)]
  if (facts.met === true && !/滿足|不受影響/.test(impact)) bits.push('求解器：交期全部滿足')
  if (facts.late.length) bits.push(`延誤 ${facts.late.join('、')}`)
  return bits.filter(Boolean).join('；') || '—'
}

function Block({ n, title, hint, children, className = '' }: { n: number; title: string; hint?: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={`apv-b ${className}`}>
      <h4 className="apv-bh"><span className="apv-n">{n}</span>{title}{hint && <small>{hint}</small>}</h4>
      {children}
    </div>
  )
}

// ---------------------------------------------------------------- flow diagram (inline SVG)
/** `board` = the 計劃板 of the home screen (CJ, 2026-10-20: 「我的plancanvas 那樣」): bigger step cards on a dotted
 *  grid with each step's key result and owner, and the cards a new plan version changed marked 「改」. */
export function PlanFlow({ nodes, board = false, lines = {}, changed }: {
  nodes: PlanNode[]; board?: boolean; lines?: Record<string, string>; changed?: Set<string>
}) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const arrowId = `flow-arrow${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const levels = Math.max(...nodes.map((n) => n.level)) + 1
  const byLevel = Array.from({ length: levels }, (_, l) => nodes.filter((n) => n.level === l))
  const widest = Math.max(...byLevel.map((l) => l.length))
  const horiz = w >= 560
  const GAP = horiz ? 30 : 14, VGAP = horiz ? 12 : 26
  let NW: number, NH = board ? 78 : 58
  const pos = new Map<string, { x: number; y: number }>()
  let W: number, Hh: number
  if (horiz) {
    NW = Math.min(board ? 200 : 170, (w - GAP * (levels - 1)) / levels)
    W = levels * NW + (levels - 1) * GAP
    Hh = widest * NH + (widest - 1) * VGAP
    byLevel.forEach((col, l) => {
      const colH = col.length * NH + (col.length - 1) * VGAP
      col.forEach((n, i) => pos.set(n.id, { x: l * (NW + GAP), y: (Hh - colH) / 2 + i * (NH + VGAP) }))
    })
  } else {
    NW = Math.min(board ? 200 : 170, (w - GAP * (widest - 1)) / widest)
    NH = board ? 74 : 54
    W = Math.max(200, w)
    Hh = levels * NH + (levels - 1) * VGAP
    byLevel.forEach((row, l) => {
      const rowW = row.length * NW + (row.length - 1) * GAP
      row.forEach((n, i) => pos.set(n.id, { x: (W - rowW) / 2 + i * (NW + GAP), y: l * (NH + VGAP) }))
    })
  }
  return (
    <div ref={ref} className={`flow ${board ? 'plan-board' : ''}`} data-testid={board ? 'plan-board' : 'plan-flow'}>
      <svg width={W + 4} viewBox={`-2 -2 ${W + 4} ${Hh + 4}`} role="img" aria-label={`處理計畫：${nodes.map((n) => `${n.id} ${TOOL_LABEL[n.tool]} ${NODE_LABEL[n.status]}`).join('，')}`}>
        <defs>
          <marker id={arrowId} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0L8 4L0 8z" className="flow-ah" />
          </marker>
        </defs>
        {nodes.flatMap((n) => n.deps.map((d) => {
          const a = pos.get(d), b = pos.get(n.id)
          if (!a || !b) return null
          const path = horiz
            ? `M${a.x + NW},${a.y + NH / 2} C${a.x + NW + GAP / 2},${a.y + NH / 2} ${b.x - GAP / 2},${b.y + NH / 2} ${b.x - 1},${b.y + NH / 2}`
            : `M${a.x + NW / 2},${a.y + NH} C${a.x + NW / 2},${a.y + NH + VGAP / 2} ${b.x + NW / 2},${b.y - VGAP / 2} ${b.x + NW / 2},${b.y - 1}`
          return <path key={`${d}-${n.id}`} d={path} className="flow-edge" markerEnd={`url(#${arrowId})`} />
        }))}
        {nodes.map((n) => {
          const p = pos.get(n.id)!
          const risk = n.risk && (n.status === 'wait' || n.status === 'after' || n.status === 'auto') ? ` ${n.risk}` : ''
          const line = board ? clip(lines[n.id] || plain(n.purpose), Math.floor((NW - 20) / 12)) : ''
          const isNew = !!changed?.has(n.id)
          return (
            <g key={n.id} transform={`translate(${p.x},${p.y})`} className={`fnode fs-${n.status} ${isNew ? 'fs-changed' : ''}`} data-step={n.id}>
              <title>{`${n.id} ${TOOL_LABEL[n.tool]}：${plain(n.purpose)}${lines[n.id] ? `｜${lines[n.id]}` : ''}（${NODE_LABEL[n.status]}${risk}）`}</title>
              <rect width={NW} height={NH} rx={9} />
              <text x={10} y={21} className="fn-t"><tspan className="fn-id">{n.id}</tspan> {TOOL_LABEL[n.tool]}</text>
              {board && <text x={10} y={42} className="fn-l">{line}</text>}
              <text x={10} y={NH - 13} className="fn-s">{statusGlyph(n.status)} {n.status === 'wait' ? '待核准' : NODE_LABEL[n.status]}{risk}</text>
              {isNew && <g className="fn-new"><rect x={NW - 30} y={6} width={24} height={17} rx={4} /><text x={NW - 18} y={18.5} textAnchor="middle">改</text></g>}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
function statusGlyph(s: PlanNode['status']): string {
  return s === 'done' || s === 'auto' ? '✓' : s === 'wait' ? '◆' : s === 'running' ? '…' : s === 'failed' || s === 'rejected' ? '✕' : '○'
}

// ---------------------------------------------------------------- schedule before / after (inline SVG Gantt)
function ScheduleCompare({ diff, now }: { diff: ScheduleDiff; now: number }) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const LABEL = w < 480 ? 54 : 70, LANE = 17, PAD = 5, TOP = 20
  const W = Math.max(300, w)
  const x = (t: number) => LABEL + ((t - diff.t0) / (diff.t1 - diff.t0)) * (W - LABEL - 4)
  const clampX = (t: number) => Math.max(LABEL, Math.min(W - 4, x(t)))
  const rowH = LANE * 2 + PAD + 10
  const Hh = TOP + diff.presses.length * rowH + 12
  const changedNew = new Map(diff.changes.map((c) => [c.job.id, c]))
  const changedOld = new Map(diff.changes.filter((c) => c.before).map((c) => [c.before!.id, c]))
  const inWin = (j: { mold_in: string; demold: string }) => ms(j.demold) > diff.t0 && ms(j.mold_in) < diff.t1
  const days: number[] = []
  const DAY = 86_400_000
  for (let t = factoryMidnight(diff.t0) + DAY; t < diff.t1; t += DAY) days.push(t)
  const retimed = diff.changes.filter((c) => c.kind === 'retimed').length
  return (
    <div ref={ref} className="cmp" data-testid="schedule-compare">
      <svg width="100%" viewBox={`0 0 ${W} ${Hh}`} role="img" aria-label="排程前後對照">
        {days.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={TOP - 4} y2={Hh} className="cmp-grid" />
            <text x={x(t) + 3} y={12} className="cmp-tick">{fmtMD(t)}</text>
          </g>
        ))}
        {diff.presses.map((p, i) => {
          const y0 = TOP + i * rowH
          const oldJobs = diff.before.schedule.jobs.filter((j) => j.press_id === p && inWin(j))
          const newJobs = diff.after.schedule.jobs.filter((j) => j.press_id === p && inWin(j))
          return (
            <g key={p}>
              <text x={0} y={y0 + LANE + 4} className="cmp-press">{shortPress(p)} 號機</text>
              <text x={LABEL - 6} y={y0 + LANE - 5} textAnchor="end" className="cmp-lane">原</text>
              <text x={LABEL - 6} y={y0 + 2 * LANE + PAD - 5} textAnchor="end" className="cmp-lane">新</text>
              {oldJobs.map((j) => {
                const c = changedOld.get(j.id)
                const cls = c?.kind === 'moved' ? 'out' : c?.kind === 'cure' ? 'cure-old' : ''
                return <Bar key={`o${j.id}`} x0={clampX(ms(j.mold_in))} x1={clampX(ms(j.demold))} y={y0} h={LANE - 2} cls={`old ${cls}`}
                  label={j.order_id} title={`原：${j.order_id}・${machineLabel(j.press_id)}・${fmtMD(j.mold_in)} ${fmtHM(j.mold_in)} 入模・${round1(j.cure_h)} h`} />
              })}
              {newJobs.map((j) => {
                const c = changedNew.get(j.id)
                const cls = c?.kind === 'moved' ? 'moved' : c?.kind === 'cure' ? 'cure' : c?.kind === 'retimed' ? 'retimed' : ''
                const label = c?.kind === 'moved' && c.before ? `${j.order_id} ←${shortPress(c.before.press_id)}`
                  : c?.kind === 'cure' && c.before ? `${round1(c.before.cure_h)}→${round1(j.cure_h)} h` : j.order_id
                return <Bar key={`n${j.id}`} x0={clampX(ms(j.mold_in))} x1={clampX(ms(j.demold))} y={y0 + LANE + PAD} h={LANE - 2} cls={`new ${cls}`}
                  label={label} title={`新：${j.order_id}・${machineLabel(j.press_id)}・${fmtMD(j.mold_in)} ${fmtHM(j.mold_in)} 入模・${round1(j.cure_h)} h${c?.before ? `（原 ${machineLabel(c.before.press_id)} ${fmtMD(c.before.mold_in)} ${fmtHM(c.before.mold_in)}・${round1(c.before.cure_h)} h）` : ''}`} />
              })}
            </g>
          )
        })}
        {isFinite(now) && now > diff.t0 && now < diff.t1 && (
          <g><line x1={x(now)} x2={x(now)} y1={TOP - 4} y2={Hh - 12} className="cmp-now" /><text x={x(now)} y={Hh - 1} textAnchor="middle" className="cmp-now-t">現在</text></g>
        )}
      </svg>
      <p className="apv-chips" data-testid="schedule-changes">
        {diff.moved.map((m) => <span key={`${m.order}${m.from}${m.to}`} className="chg moved">{m.order} {shortPress(m.from)} → {shortPress(m.to)} 號機{m.n > 1 ? ` ×${m.n}` : ''}</span>)}
        {diff.cure && <span className="chg cure">{machineLabel(diff.cure.press)}硫化 {round1(diff.cure.from)} h → {round1(diff.cure.to)} h</span>}
        {retimed > 0 && <span className="chg">另 {retimed} 模微調時間</span>}
      </p>
    </div>
  )
}
function shortPress(id: string): string { return id.startsWith('CURE-') ? String(Number(id.slice(5))) : id }
function Bar({ x0, x1, y, h, cls, label, title }: { x0: number; x1: number; y: number; h: number; cls: string; label: string; title: string }) {
  const w = Math.max(2, x1 - x0)
  return (
    <g className={`cbar ${cls}`}>
      <title>{title}</title>
      <rect x={x0} y={y} width={w} height={h} rx={3} />
      {w > label.length * 6.4 + 8 && <text x={x0 + 4} y={y + h - 4}>{label}</text>}
    </g>
  )
}

// ---------------------------------------------------------------- tool chips
export function ToolChips({ uses }: { uses: ToolUse[] }) {
  return (
    <ul className="tchips" data-testid="tool-chips">
      {uses.map((u) => (
        <li key={u.tool} className={`tchip ${u.failed ? 'has-fail' : ''}`} data-tool={u.tool}>
          <span className="tchip-h"><ToolIcon name={u.tool} /><b>{TOOL_LABEL[u.tool] ?? u.tool}</b><span className="tchip-n">×{u.calls}</span>{u.failed > 0 && <span className="tchip-f">失敗 {u.failed}</span>}</span>
          {u.line && <span className="tchip-l">{plain(u.line)}</span>}
        </li>
      ))}
    </ul>
  )
}
