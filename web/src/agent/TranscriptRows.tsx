// Transcript rows of the agent console (issue #8), shared by the home 主控台 (final dialogue only) and the
// full Agent page (issue #37: every state change, plan, tool call, gate and approval).
import type { Event } from '../contracts'
import { fmtHMS, fmtMs } from '../format'
import { hypName, plain } from '../plain'
import { TOOL_LABEL, resultText, stepLabel } from '../stepText'
import { STATE_LABEL } from '../components/common'
import { ToolIcon } from './ToolIcon'
import {
  expectationText, thinkContext,
  type ApprovalItem, type AskItem, type ChatItem, type ChipsItem, type GateItem, type Item, type ReviewItem, type TaskItem, type ThinkItem, type ToolItem,
  type VersionItem,
} from './transcript'

const PHASE: Record<string, string> = { plan: '規劃', replan: '重新規劃' }
const VERDICT: Record<string, string> = { allow: '放行', needs_approval: '需主管核准', reject: '拒絕' }
const DECISION: Record<string, string> = { approve: '核准', modify: '修改', reject: '駁回', superseded: '作廢此卡（討論後改版）', expired: '逾時' }
const VIA: Record<string, string> = { phone: '手機', touchscreen: '現場觸控螢幕', dashboard: '戰情室', auto: '自動' }
const TASK_PHASE: Record<TaskItem['phase'], string> = { created: '派工', assigned: '前往中', started: '執行中', done: '完成', cancelled: '取消派工', count: '人員任務' }

/** One transcript row. `open` expands tool calls (args, data read, raw result); `brief` keeps a 問戰情室 row to
 *  the question and the answer (the home 主控台 shows the final dialogue only). */
export function Row({ it, ev, nth, open = false, brief = false }: { it: Item; ev: Event | null; nth: number; open?: boolean; brief?: boolean }) {
  switch (it.k) {
    case 'state':
      return <div className="r-state" data-kind="state" data-to={it.to}><span>{STATE_LABEL[it.to as keyof typeof STATE_LABEL] ?? it.to}</span>{it.reason && <small>{reasonText(it.reason)}</small>}<time>{fmtHMS(it.ts)}</time></div>
    case 'think': return <ThinkRow it={it} ev={ev} nth={nth} />
    case 'tool': return <ToolRow it={it} open={open} />
    case 'gate': return <GateRow it={it} />
    case 'blocked':
      return (
        <div className="r-line r-blocked" data-kind="blocked"><ToolIcon name="blocked" />
          <span><b>已攔下</b>{it.tool ? ` ${TOOL_LABEL[it.tool as keyof typeof TOOL_LABEL] ?? it.tool}` : ''}{it.stepId ? `（${stepLabel(it.stepId)}）` : ''}：{it.reason}</span><time>{fmtHMS(it.ts)}</time></div>
      )
    case 'approval': return <ApprovalRow it={it} />
    case 'safety':
      return <div className="r-line r-safety" data-kind="safety"><ToolIcon name="safety" /><span><b>安全核心</b>　{plain(it.message)}<small>（不經 AI，毫秒級）</small></span><time>{fmtHMS(it.ts)}</time></div>
    case 'anomaly':
      return <div className="r-line r-anom" data-kind="anomaly"><ToolIcon name="safety" /><span><b>事件偵測</b>　{plain(it.message)}</span><time>{fmtHMS(it.ts)}</time></div>
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

/** 派工 → 前往中 → 執行中 → 完成, by whom (repair tasks marked: they are what fixes the machine). */
function TaskRow({ it }: { it: TaskItem }) {
  return (
    <div className={`r-line r-task tp-${it.phase} ${it.repair ? 'repair' : ''}`} data-kind="task" data-phase={it.phase} data-testid="task-row">
      <ToolIcon name={it.phase === 'done' ? 'confirm_schedule' : it.phase === 'created' ? 'notify_teams' : 'human'} />
      <span><b>{TASK_PHASE[it.phase]}</b>　{plain(it.text)}{it.who ? `・${it.who}` : ''}{it.repair && it.phase === 'created' && <small>維修</small>}</span>
      <time>{fmtHMS(it.ts)}</time></div>
  )
}

/** 維修完成，故障排除 → 請主管覆核 → 主管覆核. */
function ReviewRow({ it }: { it: ReviewItem }) {
  return (
    <div className={`r-line r-review rv-${it.phase}`} data-kind="review" data-phase={it.phase} data-testid={`review-${it.phase}`}>
      <ToolIcon name={it.phase === 'requested' ? 'human' : 'confirm_schedule'} />
      <span><b>{it.text}</b>{it.note ? `　${it.note}` : ''}{it.phase === 'requested' && <small>已送到主管手機</small>}</span>
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
  const head = it.kind === 'dispatch' ? '事件' : it.kind === 'report' ? '事件・結果' : it.kind === 'interrupt' ? '事件・中斷'
    : it.by === 'main_agent' ? 'Agent Nemo'
    : it.kind === 'summary' ? `Agent Nemo${it.version && it.version > 1 ? `・v${it.version}` : ''}・計劃摘要` : it.kind === 'refused' ? 'Agent Nemo・無法執行' : 'Agent Nemo・回覆'
  return (
    <div className={`r-chat agent k-${it.kind} ${it.by === 'main_agent' ? 'main' : ''}`} data-kind="chat" data-role="agent" data-testid={it.kind === 'reply' ? 'chat-reply' : it.kind === 'summary' ? 'chat-summary' : it.by === 'main_agent' ? `main-${it.kind}` : 'chat-agent'}>
      <header><ToolIcon name="think" /><b>{head}</b>
        {it.classifiedBy && it.kind !== 'dispatch' && <small>{it.classifiedBy === 'model' ? 'Nemotron 判讀' : '規則判讀'}{it.fellBack ? '（模型失敗改用規則）' : ''}</small>}
        <time>{fmtHMS(it.ts)}</time></header>
      {it.pending ? <p className="muted"><i className="spin" />{it.text}</p> : <p className="r-answer">{plain(it.text)}</p>}
      {it.constraints.length > 0 && <p className="r-chips">{it.constraints.map((c) => <span key={c} className="chip-c">{c}</span>)}</p>}
    </div>
  )
}

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
      <header><ToolIcon name="human" /><b>核准卡 v{it.from} → v{it.to}</b><small>v{it.from} 已作廢，不能再核准</small><time>{fmtHMS(it.ts)}</time></header>
      {it.changes.length ? <ul>{it.changes.map((c, i) => <li key={i}>{plain(c)}</li>)}</ul> : <p className="muted small">排程內容與上一版相同</p>}
    </div>
  )
}

function ThinkRow({ it, ev, nth }: { it: ThinkItem; ev: Event | null; nth: number }) {
  const { hyp, steps } = thinkContext(ev, it, nth)
  const who = it.mode === 'llm' ? `Nemotron${it.model ? `（${it.model}）` : ''}` : it.mode === 'mock' ? '規則模式指揮官' : 'AI 指揮官'
  const exp = expectationText(hyp)
  return (
    <article className="r-think" data-kind="think" data-testid="think-block">
      <header><ToolIcon name="think" /><b>{who} {PHASE[it.phase] ?? it.phase}</b>
        {it.totalMs != null && <small>{Math.round(it.totalMs)} ms</small>}{it.tokens != null && <small>{it.tokens} tokens</small>}<time>{fmtHMS(it.ts)}</time></header>
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
          {(steps.length ? steps : it.stepIds.map((id) => ({ id, tool: null, purpose: '' }))).map((s) => (
            <li key={s.id}><b>{s.id}</b>{s.tool && <span className="r-steps-tool"><ToolIcon name={s.tool} />{TOOL_LABEL[s.tool]}</span>}<span className="muted">{plain(s.purpose)}</span></li>
          ))}
        </ol>
      )}
      {it.rationale && <p className="r-sub">理由：{plain(it.rationale)}</p>}
      {it.safeStop && <p className="r-warn">指揮官建議安全停止</p>}
    </article>
  )
}

/** Say where the plan came from: the model (reasoning off), the rule fallback, or the rule-mode commander. */
function thinkNote(it: ThinkItem): string {
  if (it.mode === 'llm' && it.source === 'mock_fallback') return '模型輸出未採用：以下計畫由規則備援產生，不是模型的思考過程。'
  if (it.mode === 'llm') return '推理模式關閉：以下是模型回傳的計畫與假設，不是隱藏的思考過程。'
  if (it.mode === 'mock') return '規則模式：沒有呼叫模型，以下計畫與假設由規則式指揮官產生。'
  return '以下是這一輪的計畫與假設（依事件紀錄重建），不是隱藏的思考過程。'
}

function ToolRow({ it, open }: { it: ToolItem; open: boolean }) {
  let line = ''
  if (it.status === 'ok' && it.data) { try { line = resultText(it.tool, it.args, it.data) } catch { line = '' } }
  if (it.status === 'failed') line = `失敗：${it.error ?? ''}`
  const ask = it.phase === 'ASK'
  return (
    <details className={`r-tool ts-${it.status}`} data-kind="tool" data-tool={it.tool} open={open || undefined}>
      <summary>
        <ToolIcon name={it.tool} />
        <b>{TOOL_LABEL[it.tool] ?? it.tool}</b>
        <span className="r-step">{it.stepId && !ask ? stepLabel(it.stepId) : ask ? '提問' : '情境'}</span>
        {it.attempt > 1 && <span className="r-retry">重試 #{it.attempt}</span>}
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

function GateRow({ it }: { it: GateItem }) {
  const v = it.verdict ?? (it.dropped ? 'reject' : '')
  return (
    <div className={`r-line r-gate gv-${v}`} data-kind="gate"><ToolIcon name="gate" />
      <span><b>動作把關</b>　{it.stepId ? stepLabel(it.stepId) : ''}{it.tool ? ` ${TOOL_LABEL[it.tool as keyof typeof TOOL_LABEL] ?? it.tool}` : ''}
        {it.risk && <span className={`risk r-${it.risk}`}>{it.risk}</span>}
        <span className="r-verdict">{VERDICT[v] ?? v}</span>
        {it.reasons.filter((r) => !/approval required$/.test(r)).length > 0 && <small>（{it.reasons.filter((r) => !/approval required$/.test(r)).join('；')}）</small>}
      </span><time>{fmtHMS(it.ts)}</time></div>
  )
}

function ApprovalRow({ it }: { it: ApprovalItem }) {
  if (it.phase === 'request') {
    return <div className="r-line r-appr" data-kind="approval"><ToolIcon name="human" /><span><b>送主管核准</b>{it.round ? `（第 ${it.round} 輪）` : ''}{it.risk && <span className={`risk r-${it.risk}`}>{it.risk}</span>}　{plain(it.title)}</span><time>{fmtHMS(it.ts)}</time></div>
  }
  const who = !it.by || it.by === 'supervisor' ? '主管' : it.by === 'system' ? '系統' : it.by
  return (
    <div className={`r-line r-appr dec-${it.decision}`} data-kind="approval"><ToolIcon name="human" />
      <span><b>{who}{DECISION[it.decision ?? ''] ?? it.decision}</b>{it.optionId ? ` 方案 ${it.optionId}` : ''}{it.via ? `（${VIA[it.via] ?? it.via}）` : ''}{it.reason ? `　${plain(it.reason)}` : ''}</span><time>{fmtHMS(it.ts)}</time></div>
  )
}

function AskRow({ it, brief }: { it: AskItem; brief: boolean }) {
  const a = it.answer
  return (
    <div className="r-ask" data-kind="ask">
      <div className="r-q"><span>{it.question}</span><small>主管・{fmtHMS(it.ts)}</small></div>
      <div className="r-a" data-testid={a ? 'ask-answer' : undefined}>
        <header><ToolIcon name="think" /><b>Agent Nemo</b><span className="ro">查詢</span>{a && <small>{a.id}・{Math.round(a.total_ms)} ms{a.mode === 'mock' || a.fell_back_to_mock ? '・規則模式' : ''}</small>}</header>
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

const REASON: Record<string, string> = {
  'detector wake-up': '事件偵測喚醒', 'gather sensor window, prediction and SOP': '讀感測器、預測與 SOP', 'ask commander': '請指揮官規劃',
  'run read/compute steps': '執行讀取／計算步驟', 'check write steps': '檢查會改變現場的步驟', 'approved by supervisor': '主管已核准',
  'compare expectation with readings (code)': '程式比對預期與讀值', 'rejected by supervisor': '主管已駁回',
}
function reasonText(r: string): string {
  if (REASON[r]) return REASON[r]
  const m = /^(R\d) approval$/.exec(r)
  if (m) return `${m[1]} 需主管核准`
  return plain(r)
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
