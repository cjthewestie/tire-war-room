// Transcript rows of the agent console (issue #8), shared by the home 主控台 (final dialogue only) and the
// full Agent page (issue #37: every state change, plan, tool call, gate and approval).
import type { Event } from '../contracts'
import { CH_LABEL, REPAIR_KINDS } from '../aiStatus'
import { machineLabel } from '../factory'
import { fmtHMS, fmtMs } from '../format'
import { hypName, plain } from '../plain'
import { TOOL_LABEL, resultText, stepLabel } from '../stepText'
import { STATE_LABEL } from '../components/common'
import { ToolIcon } from './ToolIcon'
import { workerName } from './subagents'
import {
  expectationText, thinkContext,
  type ApprovalItem, type AskItem, type ChatItem, type ChipsItem, type GateItem, type Item, type ReviewItem, type TaskItem, type ThinkItem, type ToolItem,
  type VersionItem,
} from './transcript'

const PHASE: Record<string, string> = { plan: '規劃', replan: '重新判斷' }
const DECISION: Record<string, string> = { approve: '核准', modify: '修改', reject: '駁回', superseded: '作廢此卡（討論後改版）', expired: '逾時' }
const VIA: Record<string, string> = { phone: '手機', touchscreen: '現場觸控螢幕', dashboard: '戰情室', auto: '自動' }

/** One transcript row. `open` expands tool calls (args, data read, raw result); `brief` keeps a 問戰情室 row to
 *  the question and the answer (the home 主控台 shows the final dialogue only). */
export function Row({ it, ev, open = false, brief = false }: { it: Item; ev: Event | null; open?: boolean; brief?: boolean }) {
  switch (it.k) {
    case 'state':
      return <div className="r-state" data-kind="state" data-to={it.to}><span>{STATE_LABEL[it.to as keyof typeof STATE_LABEL] ?? it.to}</span>{it.reason && <small>{reasonText(it.reason)}</small>}<time>{fmtHMS(it.ts)}</time></div>
    case 'think': return <ThinkRow it={it} ev={ev} />
    case 'tool': return <ToolRow it={it} open={open} />
    case 'gate': return <GateRow it={it} />
    case 'blocked':
      return (
        <div className="r-line r-blocked" data-kind="blocked"><ToolIcon name="blocked" />
          <span><b>已攔下</b>{it.tool ? ` ${TOOL_LABEL[it.tool as keyof typeof TOOL_LABEL] ?? it.tool}` : ''}{it.stepId ? `（${stepLabel(it.stepId)}）` : ''}：{it.reason}</span><time>{fmtHMS(it.ts)}</time></div>
      )
    case 'approval': return <ApprovalRow it={it} ev={ev} />
    case 'safety':
      return <div className="r-line r-safety" data-kind="safety"><ToolIcon name="safety" /><span><b>安全核心</b>　{plain(it.message)}<small>（不經 AI，毫秒級）</small></span><time>{fmtHMS(it.ts)}</time></div>
    case 'anomaly':
      return <div className="r-line r-anom" data-kind="anomaly"><ToolIcon name="safety" /><span><b>偵測到異常</b>　{anomalyText(it.message, ev)}</span><time>{fmtHMS(it.ts)}</time></div>
    case 'note':
      return <div className={`r-line r-note ${it.tone}`} data-kind="note"><span className="r-dot" /><span>{plain(it.text)}</span><time>{fmtHMS(it.ts)}</time></div>
    case 'ask': return <AskRow it={it} brief={brief} />
    case 'chat': return <ChatRow it={it} />
    case 'chips': return <ChipsRow it={it} />
    case 'version': return <VersionRow it={it} />
    case 'task': return <TaskRow it={it} />
    case 'review': return <ReviewRow it={it} />
  }
}

/** 「10 號 128 → 122°C，預測穩定在 122±1°C」 → 「10 號硫化機溫度 128 → 122°C（預測穩定在 122±1°C）」; other detector texts
 *  as they are. */
function anomalyText(message: string, ev: Event | null): string {
  const m = /^\d+ 號 (.+?)，(預測.+)$/.exec(message)
  const ch = ev?.anomaly.channel
  return m && ev && ch && CH_LABEL[ch] ? `${machineLabel(ev.machine_id)}${CH_LABEL[ch]} ${m[1]}（${m[2]}）` : plain(message)
}

/** Tasks done at one machine: 「檢修 3 號硫化機」; the others say what they are (宣達, 調派支援). */
const AT_MACHINE = new Set([...REPAIR_KINDS, '設定硫化時間'])

/** What a task does, in a 建立 row: 「硫化機：10 號機溫度下降，請檢查蒸汽供應與溫度計」 → 「檢查 10 號硫化機蒸汽供應與溫度計」. */
function madeText(it: TaskItem): string {
  const m = it.machine ? machineLabel(it.machine) : ''
  const ask = /請(.+)$/.exec(it.text)?.[1]
  if (m && ask && /^檢[查修]/.test(ask)) return `${ask.slice(0, 2)} ${m}${ask.slice(2)}`
  const t = plain(it.text)
  return m && it.kind && AT_MACHINE.has(it.kind) && !t.includes(m) ? `${t}（${m}）` : t
}

const TASK_TIMES: [keyof TaskItem['times'], string][] = [['assigned', '派人'], ['started', '開始'], ['done', '完成'], ['cancelled', '取消']]
const NOW: Partial<Record<TaskItem['phase'], string>> = { assigned: '前往中', started: '進行中' }

/** One row per worker task (mergeTasks): who, what, and when they were sent, started and finished; a repair held for the
 *  approval first has its own 「建立維修工作…（核准後才派人）」 row, a task nobody took yet says 待派. Repair tasks are
 *  marked: they fix the machine. */
function TaskRow({ it }: { it: TaskItem }) {
  const row = (head: string, body: React.ReactNode) => (
    <div className={`r-line r-task tp-${it.phase} ${it.repair ? 'repair' : ''}`} data-kind="task" data-phase={it.phase} data-testid="task-row">
      <ToolIcon name={it.phase === 'done' ? 'confirm_schedule' : it.phase === 'created' ? 'notify_teams' : 'human'} />
      <span><b>{head}</b>{body}</span>
      <time>{fmtHMS(it.ts)}</time></div>
  )
  if (it.phase === 'count') return row('現場工作', `　${it.text}`)
  if (!it.taskId) return row('取消現場工作', `　${it.text}`)
  if (it.phase === 'created') return row(it.repair ? '建立維修工作' : '建立現場工作', <>：{madeText(it)}{it.held ? '（核准後才派人）' : it.waiting && <small>待派</small>}</>)
  const what = it.kind && it.machine && AT_MACHINE.has(it.kind) ? `${it.kind} ${machineLabel(it.machine)}` : plain(it.text)
  const times = TASK_TIMES.filter(([k]) => it.times[k]).map(([k, w]) => `${w} ${fmtHMS(it.times[k])}`)
  const now = NOW[it.phase] && (it.repair && it.phase === 'started' ? '維修中' : NOW[it.phase])
  return row(workerName(it.who) || '待派', <>　{what}<small>{[...times, ...(now ? [now] : [])].join(' → ')}</small></>)
}

/** 維修完成 → 已恢復正常 → Agent Nemo 異常報告已送到主管手機 → 主管已讀報告. */
function ReviewRow({ it }: { it: ReviewItem }) {
  return (
    <div className={`r-line r-review rv-${it.phase}`} data-kind="review" data-phase={it.phase} data-testid={`review-${it.phase}`}>
      <ToolIcon name={it.phase === 'read' ? 'human' : 'confirm_schedule'} />
      <span><b>{it.text}</b>{it.note ? `　${it.note}` : ''}</span>
      <time>{fmtHMS(it.ts)}</time></div>
  )
}

function ChatRow({ it }: { it: ChatItem }) {
  if (it.role === 'me') {
    return (
      <div className={`r-chat me ${it.pending ? 'pending' : ''}`} data-kind="chat" data-role="me" data-testid="chat-me">
        <div className="r-q"><span>{it.text}</span><small>主管・{fmtHMS(it.ts)}</small></div>
      </div>
    )
  }
  // plain event lines, no dispatch to a typed agent (CJ 2026-10-21: 「現在先不要派發，主要就是一個 agent 做 tool calling」)
  const head = it.kind === 'dispatch' ? 'Agent Nemo 開始處理' : it.kind === 'interrupt' ? '事件・中斷'
    : it.by === 'main_agent' ? 'Agent Nemo'
    : it.kind === 'summary' ? `Agent Nemo 請主管核准${it.version && it.version > 1 ? '（已重算）' : ''}` : it.kind === 'refused' ? 'Agent Nemo・無法執行' : 'Agent Nemo・回覆'
  return (
    <div className={`r-chat agent k-${it.kind} ${it.by === 'main_agent' ? 'main' : ''}`} data-kind="chat" data-role="agent" data-testid={it.kind === 'reply' ? 'chat-reply' : it.kind === 'summary' ? 'chat-summary' : it.by === 'main_agent' ? `main-${it.kind}` : 'chat-agent'}>
      <header><ToolIcon name="think" /><b>{head}</b>
        {it.classifiedBy && it.kind !== 'dispatch' && <small>{it.classifiedBy === 'model' ? 'Nemotron 判讀' : '規則判讀'}{it.fellBack ? '（模型失敗改用規則）' : ''}</small>}
        <time>{fmtHMS(it.ts)}</time></header>
      {it.pending ? <p className="muted"><i className="spin" />{it.text}</p> : <p className="r-answer">{plain(it.kind === 'summary' ? summaryText(it.text) : it.text)}</p>}
      {it.constraints.length > 0 && <p className="r-chips">{it.constraints.map((c) => <span key={c} className="chip-c">{c}</span>)}</p>}
    </div>
  )
}

/** The plan summary at the gate: 「EV-…｜請核准：排程第 3 版（…）。」 → 「第 3 版排程（…）。」 (the row's head says 請主管核准). */
const summaryText = (t: string) => t.replace(/^EV-[^｜]*｜/, '').replace(/^請核准：/, '').replace(/^排程第 (\d+) 版/, '第 $1 版排程')

function ChipsRow({ it }: { it: ChipsItem }) {
  return (
    <div className="r-line r-chipsrow" data-kind="chips" data-testid="constraint-chips"><ToolIcon name="gate" />
      <span><b>主管條件</b>　{it.labels.map((c) => <span key={c} className="chip-c">{c}</span>)}
        {it.inForce.length > it.labels.length && <small>（目前共 {it.inForce.length} 項：{it.inForce.join('、')}）</small>}<small>程式會套用到每次排程並檢查結果</small></span>
      <time>{fmtHMS(it.ts)}</time></div>
  )
}

function VersionRow({ it }: { it: VersionItem }) {
  return (
    <div className="r-version" data-kind="version" data-testid="card-version">
      <header><ToolIcon name="human" /><b>核准卡已重算</b><small>舊的那張作廢、不能再核准</small><time>{fmtHMS(it.ts)}</time></header>
      {it.changes.length ? <ul>{it.changes.map((c, i) => <li key={i}>{plain(c)}</li>)}</ul> : <p className="muted small">排程內容跟上一張相同</p>}
    </div>
  )
}

function ThinkRow({ it, ev }: { it: ThinkItem; ev: Event | null }) {
  const { hyp, steps } = thinkContext(ev, it)
  const how = it.mode === 'llm' && it.source !== 'mock_fallback' ? `Nemotron${it.totalMs != null ? `・${(it.totalMs / 1000).toFixed(1)} 秒` : ''}`
    : it.mode ? '規則備援' : ''
  const exp = expectationText(hyp)
  return (
    <article className="r-think" data-kind="think" data-testid="think-block">
      <header><ToolIcon name="think" /><b>Agent Nemo {PHASE[it.phase] ?? it.phase}</b>{how && <small>{how}</small>}<time>{fmtHMS(it.ts)}</time></header>
      <p className="r-think-note">{thinkNote(it)}</p>
      {it.error && <p className="r-warn">{it.source === 'mock_fallback' ? '模型輸出未通過檢查，改用規則備援計畫：' : '呼叫失敗：'}{it.error}</p>}
      {it.hypothesisId && (
        <div className="r-hyp">
          <span className="r-hyp-id">{hypName(it.hypothesisId)}</span>
          <span>{hyp ? plain(hyp.statement) : it.causeCode ?? ''}</span>
          {hyp && <span className={`r-hyp-st st-${hyp.status}`}>{hyp.status === 'rejected' ? '已被讀值推翻' : hyp.status === 'confirmed' ? '已驗證成立' : '待驗證'}</span>}
        </div>
      )}
      {exp && <p className="r-sub">預期：{exp}</p>}
      {hyp && hyp.evidence.length > 0 && <ul className="r-ev">{hyp.evidence.slice(0, 3).map((e, i) => <li key={i}>{plain(e)}</li>)}</ul>}
      {it.stepIds.length > 0 && (
        <ol className="r-steps">
          {it.stepIds.map((id) => steps.find((s) => s.id === id) ?? { id, tool: null, purpose: '' }).map((s) => (
            <li key={s.id}><b>{s.id}</b>{s.tool && <span className="r-steps-tool"><ToolIcon name={s.tool} />{TOOL_LABEL[s.tool]}</span>}<span className="muted">{plain(s.purpose)}</span></li>
          ))}
        </ol>
      )}
      {it.rationale && <p className="r-sub">理由：{plain(it.rationale)}</p>}
      {it.safeStop && <p className="r-warn">Agent Nemo 建議安全停止</p>}
    </article>
  )
}

/** Say where the plan came from: the model (reasoning off: no hidden chain of thought) or the rule fallback. */
function thinkNote(it: ThinkItem): string {
  if (it.mode === 'llm' && it.source === 'mock_fallback') return '模型輸出沒有採用：這份計畫由規則備援產生。'
  if (it.mode === 'llm') return '這是模型回傳的計畫與判斷（推理模式關閉，不是模型的內部思考）。'
  if (it.mode === 'mock') return '沒有呼叫模型：這份計畫由規則備援產生。'
  return '這一輪的計畫與判斷（依事件紀錄重建）。'
}

/** A confirm_schedule another event beat to it (concurrent_3), in one amber line: the state machine re-solves on the
 *  schedule in force, then carries the approval over (same content) or sends a recalculated card (transcript.ts
 *  staleOutcome). The runtime's English error stays under 原始資料. */
const isStale = (it: ToolItem) => it.status === 'failed' && /is stale/.test(it.error ?? '')
const STALE: Record<NonNullable<ToolItem['stale']>, string> = {
  resolving: '排程剛被另一件異常更新，Agent Nemo 正在依新排程重算',
  ask: '排程剛被另一件異常更新，Agent Nemo 已依新排程重算，再請主管核准',
  carried: '排程剛被另一件異常更新，Agent Nemo 已依新排程重算；內容跟主管核准的相同，沿用原核准',
  stopped: '排程剛被另一件異常更新，這一輪沒有執行',
}

function ToolRow({ it, open }: { it: ToolItem; open: boolean }) {
  let line = ''
  if (it.status === 'ok' && it.data) { try { line = resultText(it.tool, it.args, it.data) } catch { line = '' } }
  const stale = isStale(it)
  if (it.status === 'failed') line = stale ? STALE[it.stale ?? 'resolving'] : `失敗：${it.error ?? ''}`
  const ask = it.phase === 'ASK'
  const step = ask ? '提問' : it.stepId ? stepLabel(it.stepId) : null
  return (
    <details className={`r-tool ts-${stale ? 'stale' : it.status}`} data-kind="tool" data-tool={it.tool} open={open || undefined}>
      <summary>
        <ToolIcon name={it.tool} />
        <b>{TOOL_LABEL[it.tool] ?? it.tool}</b>
        {step && <span className="r-step">{step}</span>}
        {it.attempt > 1 && <span className="r-retry">{it.attempt > 2 ? `再試第 ${it.attempt - 1} 次` : '再試一次'}</span>}
        <span className="r-res">{it.status === 'running' ? <><i className="spin" />執行中…</> : <>{plain(line) || '完成'}{it.partial && <em className="r-partial">（完整結果同步中…）</em>}</>}</span>
        {it.durationMs != null && <small className="r-ms">{fmtMs(it.durationMs)}</small>}
        <time>{fmtHMS(it.ts)}</time>
      </summary>
      <div className="r-json">
        {it.data && <DataRead tool={it.tool} data={it.data} />}
        <details><summary>參數</summary><pre>{JSON.stringify(it.args, null, 2)}</pre></details>
        {it.data && <details><summary>結果（原始資料）</summary><pre>{JSON.stringify(it.data, null, 2)}</pre></details>}
        {it.error && <pre className="r-err">{it.error}</pre>}
      </div>
    </details>
  )
}

const gateVerdict = (g: GateItem) => g.verdict ?? (g.dropped ? 'reject' : '')
/** 「確認排程 R3、設定硫化時間 R2」「2 個通知 R1」: the steps of one verdict, the same tool counted once. */
function gateNames(gs: GateItem[]) {
  const by = new Map<string, GateItem[]>()
  for (const g of gs) { const l = g.tool ? TOOL_LABEL[g.tool as keyof typeof TOOL_LABEL] ?? g.tool : g.stepId ?? '步驟'; by.set(l, [...(by.get(l) ?? []), g]) }
  return [...by].map(([l, x], i) => (
    <span key={l}>{i > 0 && '、'}{x.length > 1 ? `${x.length} 個${l}` : l}{x[0].risk && <span className={`risk r-${x[0].risk}`}>{x[0].risk}</span>}</span>
  ))
}

/** The gate's verdicts on the plan's write steps, one row: 「安全把關：確認排程、設定硫化時間要主管核准；2 個通知自動放行」. */
function GateRow({ it }: { it: GateItem }) {
  const all = it.group ?? [it]
  const of = (v: string) => all.filter((g) => gateVerdict(g) === v)
  const ask = of('needs_approval'), allow = of('allow'), no = of('reject')
  const why = [...new Set(no.flatMap((g) => g.reasons))]
  const parts = [
    ask.length > 0 && <span key="a">{gateNames(ask)}<span className="r-verdict">要主管核准</span></span>,
    allow.length > 0 && <span key="o">{gateNames(allow)}<span className="r-verdict">自動放行</span></span>,
    no.length > 0 && <span key="r">{gateNames(no)}<span className="r-verdict">擋下</span>{why.length > 0 && <small>（{why.join('；')}）</small>}</span>,
  ].filter(Boolean)
  return (
    <div className={`r-line r-gate gv-${no.length ? 'reject' : ask.length ? 'needs_approval' : 'allow'}`} data-kind="gate"><ToolIcon name="gate" />
      <span><b>安全把關</b>：{parts.map((p, i) => <span key={i}>{i > 0 && '；'}{p}</span>)}</span><time>{fmtHMS(it.ts)}</time></div>
  )
}

/** 「10 號機溫度下降 128→122.4°C」 → 「10 號硫化機溫度下降 128→122.4°C」; 「異常：B 線開煉機 溫度過高」 → without 「異常：」. */
function cardTitle(t: string | null, ev: Event | null): string {
  const x = plain(t).replace(/^異常[：:]\s*/, '')
  return ev?.machine_id.startsWith('CURE-') ? x.replace(/^\d+ ?號機/, machineLabel(ev.machine_id)) : x
}

function ApprovalRow({ it, ev }: { it: ApprovalItem; ev: Event | null }) {
  if (it.phase === 'request') {
    return <div className="r-line r-appr" data-kind="approval"><ToolIcon name="human" /><span><b>送主管核准</b>{it.round && it.round > 1 ? `（第 ${it.round} 輪）` : ''}{it.risk && <span className={`risk r-${it.risk}`}>{it.risk}</span>}　{cardTitle(it.title, ev)}</span><time>{fmtHMS(it.ts)}</time></div>
  }
  const who = !it.by || it.by === 'supervisor' ? '主管' : it.by === 'system' ? '系統' : it.by
  // the option is named only when the supervisor had a choice (方案 A「夜班加班」); a one-option card is the plan itself
  const opt = it.options.length > 1 ? it.options.find((o) => o.id === it.optionId) : null
  // the phone fills the reason with the option it approved (「核准「夜班加班」」): already said
  const reason = it.reason && !/^核准「[^」]*」$/.test(it.reason) ? it.reason : null
  return (
    <div className={`r-line r-appr dec-${it.decision}`} data-kind="approval"><ToolIcon name="human" />
      <span><b>{who}{DECISION[it.decision ?? ''] ?? it.decision}{opt ? `方案 ${opt.id}「${opt.label}」` : ''}</b>{it.via ? `（${VIA[it.via] ?? it.via}）` : ''}{reason ? `　${plain(reason)}` : ''}</span><time>{fmtHMS(it.ts)}</time></div>
  )
}

function AskRow({ it, brief }: { it: AskItem; brief: boolean }) {
  const a = it.answer
  return (
    <div className="r-ask" data-kind="ask">
      <div className="r-q"><span>{it.question}</span><small>主管・{fmtHMS(it.ts)}</small></div>
      <div className="r-a" data-testid={a ? 'ask-answer' : undefined}>
        <header><ToolIcon name="think" /><b>Agent Nemo</b><span className="ro">查詢</span>{a && <small>{a.id}・{Math.round(a.total_ms)} ms{a.mode === 'mock' || a.fell_back_to_mock ? '・規則備援' : ''}</small>}</header>
        {!a && !it.error && <p className="muted"><i className="spin" />查詢中…</p>}
        {it.error && <p className="err">{it.error}</p>}
        {a && !brief && a.steps.map((s, i) => (
          <details key={i} className={`r-tool ts-${s.ok ? 'ok' : 'failed'}`} data-kind="tool" data-tool={s.tool}>
            <summary><ToolIcon name={s.tool} /><b>{TOOL_LABEL[s.tool as keyof typeof TOOL_LABEL] ?? s.tool}</b><span className="r-step">Q{i + 1}</span><span className="r-res">{s.ok ? s.summary : `失敗：${s.error ?? ''}`}</span></summary>
            <div className="r-json"><details><summary>參數</summary><pre>{JSON.stringify(s.args, null, 2)}</pre></details></div>
          </details>
        ))}
        {a && !brief && a.intercepted.map((x, i) => <div key={`x${i}`} className="r-line r-blocked"><ToolIcon name="blocked" /><span><b>已攔下</b> {x.tool}：{x.reason}</span></div>)}
        {a && <p className="r-answer">{a.answer}</p>}
      </div>
    </div>
  )
}

// the state machine's transition reasons (warroom/agent/state_machine.py), in the supervisor's words
const REASON: Record<string, string> = {
  'detector wake-up': '偵測到異常', 'supervisor command': '主管指令', 'plant event': '工廠事件',
  'gather sensor window, prediction and SOP': '讀感測器、預測與 SOP', 'supervisor command: read context': '讀取資料',
  'plant event: read the SOP': '查 SOP', 'read orders, roster and materials; ask commander (plant event)': '讀訂單、班表與原料，請 Agent Nemo 規劃',
  'ask commander': '請 Agent Nemo 規劃', 'ask commander (supervisor command)': '請 Agent Nemo 規劃',
  'command cannot be carried out by any tool': '沒有工具做得到這個指令', 'run read/compute steps': '讀取資料、試算',
  'check write steps': '檢查會改動現場的步驟', 'R1 auto writes': '自動執行低風險步驟（通知）', 'no write steps': '沒有要改動現場的步驟',
  'approved writes executed': '已執行核准的步驟', 'compare expectation with readings (code)': '程式比對預期與讀值', 'rejected by supervisor': '主管已駁回',
}
const BY: Record<string, string> = { supervisor: '主管', system: '系統', 主管: '主管' }
function reasonText(r: string): string {
  if (REASON[r]) return REASON[r]
  const m = /^(R\d) approval$/.exec(r)
  if (m) return `${m[1]} 需主管核准`
  const a = /^approved by (\S+)( \(approval .* reused)?/.exec(r)
  if (a) return `${BY[a[1]] ?? a[1]}已核准${a[2] ? '（內容相同，沿用核准）' : ''}`
  return plain(r.replace(/^已修復：/, ''))
}

type D = Record<string, unknown>
const arr = (v: unknown): D[] => (Array.isArray(v) ? (v as D[]) : [])

/** What a read tool brought back, in plain lines: SOP hits with their source and score, sensor readings,
 *  the orders / shifts rows. Everything else is in the raw result below. */
function DataRead({ tool, data }: { tool: string; data: D }) {
  if (tool === 'sop_search') {
    const hits = [...arr(data.hits), ...arr(data.team_hits)]
    if (!hits.length) return <p className="r-data muted">SOP 無相符</p>
    return (
      <ul className="r-data" aria-label="SOP 命中">
        {hits.map((h, i) => (
          <li key={i}><b>{String(h.sop_id ?? '')}</b> {String(h.title ?? '')}
            <small>{h.source === 'team_dataset' ? '團隊資料集' : String(h.source ?? '')}{typeof h.score === 'number' ? `・分數 ${h.score.toFixed(3)}` : ''}</small></li>
        ))}
      </ul>
    )
  }
  if (tool === 'read_sensors') {
    const summ = (data.summary ?? {}) as Record<string, D>
    const rows = Object.entries(summ)
    if (!rows.length) return null
    return (
      <ul className="r-data" aria-label="感測器讀值">
        {rows.map(([id, s]) => (
          <li key={id}><b>{id}</b> {Object.entries(s).filter(([, v]) => typeof v === 'number').slice(0, 6)
            .map(([k, v]) => `${k} ${Math.round((v as number) * 10) / 10}`).join('・')}</li>
        ))}
      </ul>
    )
  }
  if (tool === 'query_orders_shifts') {
    const jobs = arr(data.next_jobs).slice(0, 5)
    const gaps = arr(data.gaps)
    if (!jobs.length && !gaps.length) return null
    return (
      <ul className="r-data" aria-label="訂單與班表">
        {jobs.map((j, i) => <li key={i}><b>{String(j.order_id ?? '')}</b> {String(j.press_id ?? '')} 入模 {String(j.mold_in ?? '').slice(5, 16).replace('T', ' ')}</li>)}
        {gaps.map((g, i) => <li key={`g${i}`} className="warn">{String(g)}</li>)}
      </ul>
    )
  }
  return null
}
