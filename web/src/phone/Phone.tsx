// Supervisor phone (UX brief §6, CJ 2026-10-20): notification → ONE decision card → 核准, or 討論 to change it by
// talking (「agent 計劃就只有核准跟討論」) → done. Plus 問 Agent Nemo. Nothing else.
// The card (veteran 10-22, 人力排程 v2 layout, Demo steps 3 and 5): 請核准・03:54 前 → what happened, big → one sentence
// (why, who goes, how many seconds, when it turns green) → 核准後會做 (3 lines at most) → the 方案 only when there are
// 2+ staffing choices, each saying what it changes and why the system suggests it → the fixed 核准 / 討論 bar; SOP, the
// trend and the full actions fold into 看依據. No R0–R3 on this page (cardText.ts builds the words).
// Fast repair (Kyle 10-21 flow): after 核准 one line per event being handled (「7 號硫化機・維修中・約 8 秒」); at
// Event.repaired_at a 「維修完成，溫度回升中」 notice, at close 「已恢復正常」 with 看報告 / 知道了 (RepairNotice), one per event
// (3 件同時異常: one after another), each ringing once; nothing waits for them. 「已讀」 joins the history.
import { useEffect, useRef, useState } from 'react'
import { TodayOrders } from '../components/TodayOrders'
import { newConversationId } from '../components/ordersToday'
import type { ApprovalRequest, AskAnswer, Event } from '../contracts'
import { TemperatureTrend } from '../components/Trend'
import { plain } from '../plain'
import { MACHINE_META, machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { useHealth } from '../health'
import { useWarRoom } from '../useWarRoom'
import { repairNotices, useApprovalAlerts } from './alert'
import { ThemeToggle } from '../components/ThemeToggle'
import { RepairEta } from './RepairEta'
import { Txt } from '../components/BriefingView'
import { fullActionLines } from '../briefing'
import { AiStatusLine } from '../AiStatusLine'
import { optionChoices } from '../optionGuide'
import { RepairNotice } from './RepairNotice'
import { fastRepairOf, isRepairTask } from './fastEta'
import { cardSentence, cardTitle, deadlineText, doLines, gapNote, liveLine, problemText, rebasedText, staffNote, type LiveLine } from './cardText'

export function Phone() {
  const { state, transport, connected } = useWarRoom()
  // the most urgent card first: R3 before R2, then the newest
  const pending = [...state.pending].sort((a, b) => (a.risk === b.risk ? (a.created_at < b.created_at ? 1 : -1) : a.risk > b.risk ? -1 : 1))
  const [pick, setPick] = useState<string | null>(null)
  const top = pending.find((p) => p.id === pick) ?? pending[0]
  // kept here, not in the card: the card is replaced by its new version once the agent has replanned
  const [sent, setSent] = useState(false)
  const net = state.snapshot?.header.network ?? 'online'
  const lanDown = net === 'lan_down'
  // fast-repair notices: 維修完成 while the readings come back, then the report until 知道了 (hidden here at once, the event
  // update follows over the socket); keyed by the repair, since a scenario restart reuses event ids
  const [read, setRead] = useState<Record<string, Event>>({})
  const rkey = (e: Event) => `${e.id}:${e.repaired_at ?? ''}`
  const events = state.snapshot?.events ?? []
  const evOf = (id: string) => events.find((e) => e.id === id) ?? null
  const nameOf = (r: ApprovalRequest) => { const e = evOf(r.event_id); return e ? machineLabel(e.machine_id) : plain(r.title) }
  const notices = events.filter((e) => e.repaired_at && !read[rkey(e)] && (e.state === 'closed' ? e.review?.status === 'pending' : e.state !== 'safe_stop'))
    .sort((a, b) => (a.repaired_at! < b.repaired_at! ? -1 : 1))
  const readReports = events.filter((e) => e.review?.status === 'done' || read[rkey(e)])
    .map((e) => (e.review?.status === 'done' ? e : read[rkey(e)]))
  // 「02:15 已核准 7 號硫化機（方案 B）」, 「02:18 已讀 7 號硫化機報告」; a card the server carried over (decision.via 'auto')
  // repeats the supervisor's own decision, and a card approved again after a recompute (3 件同時異常) says the same as
  // the first: the newest of identical lines only
  const decisions = state.resolved.filter((r) => r.decision?.via !== 'auto').map((r) => ({ at: r.decision?.decided_at ?? r.created_at, key: r.id,
    text: `${DEC_LABEL[r.status] ?? r.status} ${nameOf(r)}${r.options.length > 1 && r.decision?.option_id ? `（方案 ${r.decision.option_id}）` : ''}` }))
  const history = [
    ...decisions.filter((h, i) => decisions.findIndex((x) => x.text === h.text) === i).slice(0, 3),
    ...readReports.map((e) => ({ at: e.review?.done_at ?? e.closed_at ?? e.opened_at, key: `rv-${e.id}`, text: `已讀 ${machineLabel(e.machine_id)}報告`, tid: 'phone-report-done' })),
  ].sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, 6)
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time
  const noticeCards = notices.map((e) => <RepairNotice key={rkey(e)} ev={e} disabled={lanDown || !transport}
    onRead={(x) => setRead((r) => ({ ...r, [rkey(e)]: x.review?.status === 'done' ? x : { ...e, review: { ...e.review!, status: 'done', done_at: ft ?? null, by: '主管' } } }))} />)
  const alerts = useApprovalAlerts([...pending, ...repairNotices(notices, state.snapshot?.machines)], connected && !lanDown)
  // 「已核准 10 號硫化機」 for 3 s, only once the server confirmed it (a stale card can still come back to be approved again)
  const [flash, setFlash] = useState<{ id: string; text: string } | null>(null)
  const decided = useRef<Set<string> | null>(null)
  useEffect(() => {
    const ok = state.resolved.filter((r) => r.status === 'approved' && r.decision?.via !== 'auto')
    const seen = decided.current
    decided.current = new Set(ok.map((r) => r.id))
    const fresh = seen && ok.find((r) => !seen.has(r.id))
    if (fresh) setFlash({ id: fresh.id, text: `已核准 ${nameOf(fresh)}` })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.resolved])
  useEffect(() => {
    if (!flash) return
    const t = window.setTimeout(() => setFlash(null), 3000)
    return () => window.clearTimeout(t)
  }, [flash])
  // watching: one line per event still being handled (open, closed while its crew still repairs, or safely stopped with
  // its machine not back to normal: a safe stop ends the flow, not the fault), 3 at most, newest first; an event whose
  // 維修完成 notice is up is said there
  const tasks = state.world?.tasks.items ?? []
  const fast = fastRepairOf(useHealth())
  const tiles = state.snapshot?.machines ?? []
  const stopped = (e: Event) => e.state === 'safe_stop' && !e.recovered_at
    && tiles.some((m) => m.machine_id === e.machine_id && m.status !== 'running' && m.status !== 'idle')
  const handling = events.filter((e) => (e.state !== 'closed' && e.state !== 'safe_stop') || stopped(e)
    || tasks.some((t) => t.event_id === e.id && isRepairTask(t) && t.status !== '完成'))
    .sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))
  const lines = handling.filter((e) => !notices.includes(e)).slice(0, 3).map((e) => liveLine(e, tasks, fast))
  return (
    <div className="phone-wrap">
      <div className="phone">
        <div className="ph-status">
          <span>主管手機</span>
          <span className="muted">{ft ? `${fmtMD(ft)} ${fmtHM(ft)}` : ''}</span>
          <span className={`ph-conn ${connected && !lanDown ? 'ok' : 'bad'}`}>{lanDown ? '廠內網路斷線' : connected ? (transport?.mode === 'mock' ? '模擬' : '廠內 LAN') : '連線中'}</span>
          {alerts.enabled && alerts.armed && (
            <button className="ph-armed ph-bell" data-testid="phone-alerts" title="核准提醒已開啟，按一下關閉" aria-label="核准提醒已開啟，按一下關閉"
              onClick={(e) => { e.stopPropagation(); alerts.setEnabled(false) }}>
              <svg viewBox="0 0 24 24" aria-hidden><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20a2 2 0 0 0 4 0" /></svg>
            </button>
          )}
          <ThemeToggle className="ph-tog" />
        </div>
        {!alerts.enabled ? (
          <button className="ph-arm" data-testid="phone-alerts-on" onClick={() => { alerts.setEnabled(true); void alerts.arm() }}>開啟核准提醒（聲音・震動{alerts.permission === 'unsupported' ? '' : '・通知'}）</button>
        ) : !alerts.armed && (
          <div className="ph-armed muted" data-testid="phone-alerts">
            <span>點一下畫面，打開提醒聲</span>
            <button className="ph-arm-off" onClick={(e) => { e.stopPropagation(); alerts.setEnabled(false) }}>關閉</button>
          </div>
        )}
        {alerts.enabled && alerts.blocked && (
          <p className="ph-blocked small" role="status" data-testid="phone-alerts-blocked">
            {alerts.permission === 'denied' ? '系統通知被瀏覽器擋住，聲音和震動照常提醒' : alerts.blocked}
          </p>
        )}
        {sent && <p className="ph-sent" role="status">已送出。Agent Nemo 正在改計畫，改好會再送來。</p>}
        {alerts.held > 0 && <div className="ph-banner">{alerts.held} 件通知暫存中，網路恢復後補送。</div>}
        {lanDown && <div className="ph-banner">廠內網路斷線：請到現場觸控螢幕核准（警示燈已亮）。</div>}
        {flash && <p className="ph-flash" role="status" data-testid="phone-approved">{flash.text}</p>}
        {/* the notices go under a waiting card: with 3 件同時異常 a 維修完成 notice above pushed 核准 off the screen
            (review 10-21); with nothing to approve they lead */}
        {pending.length === 0 && noticeCards}
        {pending.length === 0 ? (
          <div className="ph-card idle" data-testid="phone-idle">
            {handling.length ? <p className="ph-none muted">沒有要你核准的事</p> : <>
              <h2 className="ph-title">全廠正常</h2>
              <p className="ph-lead muted">沒有要你核准的事；有新的待核准會提醒你。</p>
            </>}
            {lines.length === 1 ? <OneLine l={lines[0]} /> : lines.map((l) => (
              <p key={l.id} className="ph-line" data-testid="phone-line"><b>{l.name}</b>・{l.phase}{l.secs ? `・${l.secs}` : ''}</p>
            ))}
            {lines[0] && <details className="ph-why"><summary>看執行明細</summary><RepairEta ev={evOf(lines[0].id)} /></details>}
            {history.map((h) => <p key={h.key} className="ph-hist" data-testid={'tid' in h ? h.tid : undefined}>{fmtHM(h.at)} {h.text}</p>)}
          </div>
        ) : <>
          <ApprovalCard key={top.id} a={top} n={pending.indexOf(top) + 1} of={pending.length} disabled={lanDown || !transport} onSent={() => setSent(true)} />
          {pending.length > 1 && (
            <div className="ph-more" data-testid="phone-more">
              <b>還有 {pending.length - 1} 件待核准</b>
              {pending.filter((p) => p.id !== top.id).map((p) => {
                const e = evOf(p.event_id)
                return <button key={p.id} className="ph-more-i" onClick={() => setPick(p.id)}>{e ? `${machineLabel(e.machine_id)}・${problemText(e, p)}` : plain(p.title)} ›</button>
              })}
            </div>
          )}
          {noticeCards}
        </>}
        <TodayOrders compact />
        {transport?.ask && <AskBox disabled={lanDown} />}
      </div>
    </div>
  )
}

/** The only event being handled, as a headline: 「7 號硫化機維修中，約 8 秒後維修完成」 and who is on it. */
function OneLine({ l }: { l: LiveLine }) {
  const done = l.phase === '維修中' ? '維修完成' : '完成'
  return <>
    <h2 className="ph-title" data-testid="phone-line">
      <span className="ph-chunk">{l.name}{/^[A-Z]/.test(l.phase) ? '：' : ''}{l.phase}{l.secs ? '，' : ''}</span>
      {l.secs && <span className="ph-chunk">{l.secs}後{done}</span>}
    </h2>
    {l.who && <p className="ph-lead">{l.who}</p>}
  </>
}

const ASK_EXAMPLES = ['今天有哪些訂單？', '哪些訂單有遲交風險？', '7 號機現在怎樣？']
const TOOL_ZH: Record<string, string> = {
  read_sensors: '感測器', predict_anomaly: '異常預測', cure_formula: '硫化公式',
  query_orders_shifts: '訂單／班表', sop_search: 'SOP',
}

// 問 Agent Nemo: read-only. The answer comes only from the read tools; anything that would change the
// plant is refused by the server and shown here as 已攔下.
function AskBox({ disabled }: { disabled: boolean }) {
  const { transport } = useWarRoom()
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [answers, setAnswers] = useState<AskAnswer[]>([])
  const convId = useRef(newConversationId())
  const send = async (question: string) => {
    if (!transport?.ask || !question.trim()) return
    setBusy(true); setErr(null)
    try {
      const a = await transport.ask(question.trim(), convId.current)
      setAnswers((xs) => [a, ...xs].slice(0, 3)); setQ('')
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  return (
    <div className="ph-card ph-ask" data-testid="ask-box">
      <h2 className="ph-title">問 Agent Nemo</h2>
      <p className="muted small">只查資料，不會改動工廠。</p>
      <div className="ph-opts">
        {ASK_EXAMPLES.map((x) => (
          <button key={x} className="ph-opt" disabled={disabled || busy} onClick={() => void send(x)}>{x}</button>
        ))}
      </div>
      <textarea className="ph-reason" rows={2} maxLength={300} placeholder="例如：9 號機下一模是哪張訂單？"
        value={q} disabled={disabled || busy} onChange={(e) => setQ(e.target.value)} />
      {err && <p className="err">{err}</p>}
      <div className="ph-acts">
        <button className="ph-approve" disabled={disabled || busy || !q.trim()} onClick={() => void send(q)} data-testid="ask-send">{busy ? '查詢中…' : '問'}</button>
      </div>
      {answers.map((a) => (
        <div key={a.id} className="ph-answer" data-testid="ask-answer">
          <p className="ph-hist"><b>問</b>　{a.question}</p>
          <p><b>答</b>　{a.answer}</p>
          {a.choices && a.choices.length > 0 && <div className="ph-opts">{a.choices.map((c) => <button key={c} className="ph-opt" disabled={disabled || busy} onClick={() => void send(`${c} 現在做到哪裡？`)}>{c}</button>)}</div>}
          <p className="muted small">{a.steps.length ? `資料來源：${[...new Set(a.steps.map((s) => `${TOOL_ZH[s.tool] ?? s.tool}${s.ok ? '' : '（讀取失敗）'}`))].join('、')}` : '沒有查資料'}
            {a.intercepted.length ? `・已攔下 ${a.intercepted.length} 個改動工廠的要求` : ''}{a.fell_back_to_mock ? '・規則備援' : ''}</p>
        </div>
      ))}
    </div>
  )
}

const DEC_LABEL: Record<string, string> = { approved: '已核准', modified: '已修改', rejected: '已駁回', expired: '逾時', superseded: '已改版' }

function ApprovalCard({ a, n, of, disabled, onSent }: { a: ApprovalRequest; n: number; of: number; disabled: boolean; onSent: () => void }) {
  const { transport, state } = useWarRoom()
  const ev = state.snapshot?.events.find((e) => e.id === a.event_id) ?? null
  const [opt, setOpt] = useState<string | null>(null)
  const [talk, setTalk] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState(false)   // 核准 went through: keep the button locked until the card leaves
  const fast = fastRepairOf(useHealth())
  const tasks = state.world?.tasks.items ?? []
  const now = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? null
  const base = (a.options.find((o) => o.recommended) ?? a.options[0])?.id ?? null
  const chosen = opt ?? base
  // 核准 executes the chosen option (the system's unless the supervisor picked another one)
  const approve = async () => {
    if (!transport) return
    if (a.options.length > 0 && !chosen) { setErr('請先選擇方案'); return }
    setBusy(true); setErr(null)
    try {
      await transport.decide({ request_id: a.id, decision: 'approve', option_id: chosen, reason: defaultReason(a, chosen), modified_args: null, decided_by: '主管', via: 'phone' })
      setSubmitted(true)
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  // 討論: changing or refusing the plan is said in words; the agent replans and the card comes back as a new version
  const discuss = async () => {
    if (!transport?.discuss || !text.trim()) return
    setBusy(true); setErr(null)
    try { const t = text.trim(); onSent(); setText(''); setTalk(false); await transport.discuss(a.event_id, t) } catch (e) { setErr(`送出失敗：${String(e)}`) } finally { setBusy(false) }
  }
  // every staffing option; a choice only with 2+ (a single one, e.g. after 「不要加班」, is one 人力 line): what 核准 runs
  // decides the gap note and 完整處置內容
  const staff = optionChoices(a, ev)
  const choices = staff.length > 1 ? staff : []
  const picked = staff.find((o) => o.id === chosen)
  const does = doLines(ev, a, tasks, fast)
  const gap = gapNote(a, !!picked)
  const due = deadlineText(a.expires_at, now)
  const recomputed = (a.version ?? 1) > 1
  const rebased = recomputed ? rebasedText(ev, a) : null
  const locked = disabled || busy || submitted
  // everything 核准 runs, named (the staffing option too), for 看依據
  const full = [...fullActionLines(a), ...(picked ? [`人力：${choices.length ? `方案 ${picked.id} ` : ''}${picked.label}`] : [])]
  return (
    <div className="ph-card ph-approval" data-testid="phone-card">
      <div className="ph-col-a">
      <p className="ph-meta" data-testid="phone-meta">
        請核准{of > 1 ? ` ${of} 件・第 ${n} 件` : ''}{due ? `・${due}` : ''}{a.round > 1 ? `・第 ${a.round} 輪` : ''}
        {recomputed && <b data-testid="phone-version">・已重算</b>}
      </p>
      <h2 className="ph-title" data-testid="phone-title">{cardTitle(ev, a)}</h2>
      <p className="ph-lead"><Txt>{cardSentence(ev, a, tasks, fast)}</Txt></p>
      {recomputed && (
        <div className="ph-changed" data-testid="phone-changes">
          {rebased ?? <><b>跟上一張相比：</b>{(a.changes ?? []).length ? (a.changes ?? []).map(plain).join('；') : '排程內容相同'}</>}
          {rebased && (a.changes ?? []).length > 0 && <span className="muted">（換機台明細在「看依據」）</span>}
          {(a.constraints ?? []).length > 0 && <div className="muted">主管條件：{(a.constraints ?? []).join('、')}</div>}
        </div>
      )}
      <AiStatusLine ev={ev} className="ph-ai" />
      {does.length > 0 && (
        <div className="ph-do" data-testid="phone-do">
          <b>核准後會做</b>
          <ul>{does.map((l) => <li key={l}><Txt>{l}</Txt></li>)}</ul>
        </div>
      )}
      {picked && !choices.length && <p className="ph-staff" data-testid="phone-staff">{staffNote(picked)}</p>}
      {gap && <p className="ph-gap" data-testid="phone-gap">{gap}</p>}
      </div>
      <div className="ph-col-b">
      {choices.length > 0 && <>
        <p className="ph-q">{a.gaps[0] ? `${plain(a.gaps[0])}，選一個方案` : '選一個方案'}</p>
        <div className="ph-opts" role="radiogroup" aria-label="方案">
          {choices.map((o) => (
            <button key={o.id} role="radio" aria-checked={chosen === o.id} disabled={locked}
              className={`ph-opt ph-optc ${chosen === o.id ? 'on' : ''}`} onClick={() => setOpt(o.id)}>
              <span className="ph-opt-t"><b>{o.id}</b><span>{o.label}{o.recommended ? '・系統建議' : ''}</span></span>
              <span className="ph-opt-e">{o.effect}</span>
              {o.why && <span className="ph-opt-e ph-opt-why">建議理由：{o.why}</span>}
            </button>
          ))}
        </div>
      </>}
      {talk && (
        <textarea className="ph-reason" rows={2} maxLength={300} autoFocus value={text} onChange={(e) => setText(e.target.value)}
          placeholder="例如：不要加班／改用 A" aria-label="跟 Agent Nemo 討論這個計畫" data-testid="phone-discuss-input" />
      )}
      {err && <p className="err">{err}</p>}
      <div className="ph-bar">
      <div className="ph-acts">
        <button className="ph-approve" disabled={locked || (a.options.length > 0 && !chosen)} onClick={() => void approve()}>
          {busy ? '送出中…' : submitted ? '已送出，等系統確認…' : `核准${a.options.length > 1 && chosen ? `（${chosen}）` : ''}`}
        </button>
        <button className="ph-sec" disabled={disabled || busy || submitted || !transport?.discuss} onClick={() => (talk ? void discuss() : setTalk(true))} data-testid="phone-discuss">{talk ? '送出' : '討論'}</button>
      </div>
      </div>
      <p className="ph-foot">不同意或要改，按「討論」，用一句話告訴 Agent Nemo。</p>
      <details className="ph-why">
        <summary>看依據（SOP、溫度曲線）</summary>
        <RepairEta ev={ev} />
        {rebased && (a.changes ?? []).length > 0 && <div className="ph-full"><b>跟上一張相比</b><ul>{(a.changes ?? []).map((x, i) => <li key={i}>{plain(x)}</li>)}</ul></div>}
        {full.length > 0 && <div className="ph-full" data-testid="phone-scope"><b>完整處置內容</b><ul>{full.map((x, i) => <li key={i}>{x}</li>)}</ul></div>}
        {a.done.length > 0 && <p className="muted small">已做：{a.done.map(plain).join('；')}</p>}
        <p className="muted small">判斷依據：{plain(a.reason)}</p>
        {/* plant events and supervisor commands are about the whole plant (machine PLANT): no sensor trend to draw */}
        {ev && ev.machine_id in MACHINE_META && <TemperatureTrend machineId={ev.machine_id} hypotheses={ev.hypotheses} compact />}
      </details>
      </div>
    </div>
  )
}

function defaultReason(a: ApprovalRequest, opt: string | null): string {
  const o = a.options.find((x) => x.id === opt)
  const rec = a.options.find((x) => x.recommended)
  if (o && rec && o.id !== rec.id) return `否決「${rec.label}」，改選「${o.label}」`
  return o ? `核准「${o.label}」` : ''
}
