import { useState } from 'react'
import type { AnomalyKind, ApprovalRequest, BadDataMark, DashboardSnapshot, Event, EventState, FocusPanel, MachineTile, OrderProgress, SemiFinishedProgress, TimelineItem } from '../contracts'
import { LINES, LINE_SHORT, MACHINE_META, ML_PRESSES, ML_RANGE, SIZE_LABEL, machineLabel } from '../factory'
import { fmtHM, fmtMD, fmtNum } from '../format'
import { closedTitle, hypLabel, hypName, plain } from '../plain'
import { aiStatusParts, expectationOf, recoverWord, repairStage, type RepairStage } from '../aiStatus'
import { useHealth } from '../health'
import { fastApplies } from '../phone/fastEta'
import { isAbnormal } from '../store'
import { useWarRoom } from '../useWarRoom'
import { Panel, QUALITY_LABEL, STATUS_LABEL, TimelineTable } from './common'
import { TemperatureTrend } from './Trend'
import { CureSchedule, NextAtPress } from './CureSchedule'
import { OrderGoodsLine, OrderRing, PlantPanels } from './PlantPanels'
import { ORDER_STAGES } from './orderModel'
import './monitor.css'

/** 產線監控, laid out like the approved 人力排程 draft (issue #102; Kyle, 2026-10-22). HY: 「界面簡單易懂、必要資訊就好；
 *  重複的、主管不需要知道的全部刪掉」. One sentence first — which machines are abnormal (or that all are normal) and what
 *  waits for the supervisor, with one button to 主控台 where approvals are made (the full approval card lived here too
 *  and repeated 主控台) — then the abnormal machines, the 28 machines (11 presses first), the 硫化機排程 for today +
 *  tomorrow (moved here from 人力排程) beside the next de-molds / mold-ins, and the rest of the plant folded into
 *  「更多全廠資訊」. 預警 is rendered by App; monitor.css places it right under the sentence. */
export function MonitorTab({ onMachine, onAgent, onOrders }: { onMachine: (id: string) => void; onAgent?: (eventId?: string) => void; onOrders?: () => void }) {
  const { state } = useWarRoom()
  const s = state.snapshot
  if (!s) return <div className="loading">等待工廠資料…</div>
  const machines = <MachinesPanel machines={s.machines} bad={s.header.bad_data ?? []} onMachine={onMachine} focusId={s.focus?.machine_id} />
  return (
    <div className="monitor-wrap mon">
      <Answer s={s} pending={state.pending} onAgent={onAgent} />
      {badRows(s).length > 0 ? <>
        <div className="mon-row">
          <AbnormalPanel s={s} onMachine={onMachine} />
          {machines}
        </div>
        <div className="mon-row">
          <CureSchedule onAgent={onAgent} />
          <NextAtPress />
        </div>
      </> : <>
        {/* all normal: the sentence already says so, so no empty 異常機台 panel; the machines lead and the schedule
            takes the full width */}
        <div className="mon-row">
          {machines}
          <NextAtPress />
        </div>
        <CureSchedule onAgent={onAgent} />
      </>}
      <PlantPanels>
        <OrdersPanel orders={s.orders} onOrders={onOrders} />
        <SemiPanel semi={s.semi_finished} />
      </PlantPanels>
    </div>
  )
}

/** What Agent Nemo is doing, in the supervisor's words (veteran glossary 10-22: Agent Nemo is the only subject; detection is rules). */
const AI_STEP: Record<EventState, string> = {
  detected: '偵測到異常', classify: 'Agent Nemo 判斷中', plan: 'Agent Nemo 規劃中', simulate: '排程模擬中', gate: '安全把關中',
  await_approval: '待核准', execute: '照計畫處理中', verify: '確認有沒有恢復', replan: '重新判斷中', closed: '已處理完', safe_stop: '已安全停止',
}
const KIND_WORD: Partial<Record<AnomalyKind, string>> = {
  overheat: '溫度過高', temp_drop: '溫度下降', pressure_low: '壓力偏低', water_flow_low: '水流偏低', steam_low: '蒸汽偏低',
  sensor_fault: '感測器異常', multi_machine: '多台同時異常', schedule_slip: '排程延誤', supervisor_command: '主管交辦',
  shift_start: '早班開工', order_rush: '插單', staffing_short: '人手不足',
}
const STATUS_WORD: Partial<Record<MachineTile['status'], string>> = { out_of_service: '停用', stopped: '停機', warning: '讀值偏離', alarm: '異常' }

/** Where an event stands in the supervisor's words. After the decision the event sits in verify through the whole fast
 *  repair and the ramp back (Kyle 10-21 flow), so its state is not what the phone and the 3D say: the crew's stage is
 *  (aiStatus.ts repairStage, the same seconds as aiStatusParts' eta): 「維修中・約 9 秒後維修完成」, 「維修完成・溫度回升中」,
 *  「已恢復正常」; a card waiting (a recomputed one too) stays 待核准; else how far Agent Nemo got (AI_STEP). */
function useStepWord(): { word: (ev: Event) => string; stage: (ev: Event) => RepairStage | null; fastS: number | null } {
  const { state } = useWarRoom()
  const fastS = useHealth()?.repair_fast_s ?? null
  const items = state.world?.tasks.items ?? []
  const stage = (ev: Event) => (ev.state === 'await_approval' ? null : repairStage(ev, items))
  const word = (ev: Event) => {
    const st = stage(ev)
    if (st === 'repairing') {
      const p = aiStatusParts(ev, state, null, fastS)
      return [p.phase.replace(/（\d+\/\d+）$/, ''), fastS ? p.eta : ''].filter(Boolean).join('・')
    }
    if (st === 'recovering') return `維修完成・${recoverWord(expectationOf(ev))}`
    if (st) return '已恢復正常'
    return AI_STEP[ev.state]
  }
  return { word, stage, fastS }
}

interface BadRow { id: string; name: string; problem: string; what: string; ev: Event | null; ended: Event | null; tile?: MachineTile }

const isOpen = (e: Event) => e.state !== 'closed' && e.state !== 'safe_stop'

/** Machines that are abnormal (same rule as the header's 異常 count) or have an open event; 等你核准 first. A machine
 *  still abnormal after its event ended (e.g. 安全中止：核准逾時) says how it ended. */
function badRows(s: DashboardSnapshot): BadRow[] {
  const byAge = [...s.events].sort((a, b) => (a.opened_at < b.opened_at ? -1 : 1))
  const evBy = new Map(byAge.filter(isOpen).map((e) => [e.machine_id, e]))   // the newest open event per machine
  const lastBy = new Map(byAge.map((e) => [e.machine_id, e]))
  const ids = [...new Set([...evBy.keys(), ...s.machines.filter((m) => isAbnormal(m.status)).map((m) => m.machine_id)])]
  const rows = ids.map((id): BadRow => {
    const ev = evBy.get(id) ?? null
    const ended = ev ? null : lastBy.get(id) ?? null
    const tile = s.machines.find((m) => m.machine_id === id)
    const temp = tile?.temp_c != null ? `（${tile.temp_c.toFixed(1)}°C）` : ''
    const problem = ev ? KIND_WORD[ev.anomaly.kind] ?? '異常' : tile ? STATUS_WORD[tile.status] ?? STATUS_LABEL[tile.status] : '異常'
    const what = ev ? plain(ev.anomaly.message).replace(new RegExp(`^(${machineLabel(id)}|\\d+ 號)\\s*`), '')
      : tile?.status === 'out_of_service' ? '停用中，等維修；排程不再排入這台'
      : ended?.outcome ? `${STATUS_LABEL[tile?.status ?? 'alarm']}${temp}。上一次處理：${plain(ended.outcome)}`
      : tile?.status === 'stopped' ? '停機中'
      : tile?.status === 'warning' ? `讀值偏離正常範圍${temp}，事件偵測監看中` : `${STATUS_LABEL[tile?.status ?? 'alarm']}${temp}`
    return { id, name: machineLabel(id), problem, what, ev, ended, tile }
  })
  const rank = (r: BadRow) => (r.ev?.state === 'await_approval' ? 0 : r.ev ? 1 : 2)
  return rows.sort((a, b) => rank(a) - rank(b))
}

/** The one sentence the page opens with (the draft's 「今晚夜班缺 1 人」), and on its right the one thing to do: the
 *  approvals waiting, as one line and one button to 主控台; else what the AI is doing; else the next time someone is
 *  needed at a press. */
function Answer({ s, pending, onAgent }: { s: DashboardSnapshot; pending: ApprovalRequest[]; onAgent?: (eventId?: string) => void }) {
  const { word, stage } = useStepWord()
  const rows = badRows(s)
  const mach = rows.filter((r) => r.id !== 'PLANT')
  const plant = rows.find((r) => r.id === 'PLANT')
  const working = rows.filter((r) => r.ev && r.ev.state !== 'await_approval')
  // the crew repairing (or the readings coming back) vs Agent Nemo still working it out
  const fixing = working.filter((r) => stage(r.ev!) != null)
  const thinking = working.filter((r) => stage(r.ev!) == null)
  const warm = fixing.length > 0 && !thinking.length && fixing.every((r) => stage(r.ev!) !== 'repairing')
  const main = pending.find((a) => a.event_id === rows[0]?.ev?.id) ?? pending[0] ?? null   // the first row of 異常機台
  const who = (a: ApprovalRequest) => { const m = s.events.find((e) => e.id === a.event_id)?.machine_id; return m ? machineLabel(m) : plain(a.title) }
  const rec = main ? main.options.find((o) => o.recommended) ?? main.options[0] ?? null : null
  const others = pending.filter((a) => a !== main).map(who)
  const calm = rows.length === 0
  // machine names never break inside (「3 號｜硫化機」) on a phone
  const h1 = calm ? `${s.machines.length} 台機台都正常`
    : mach.length === 1 ? `${mach[0].name}${mach[0].problem}`
    : mach.length > 1 ? <>{mach.length} 台機台異常：{mach.map((r, i) => <span key={r.id} className="nw">{r.name}{i < mach.length - 1 && '、'}</span>)}</>
    : `全廠：${plant?.problem ?? '有異常'}`
  // abnormal with no open event: out of service waits for maintenance, a 注意 reading is watched by the detector,
  // anything else (異常／停機 after its event ended) needs someone on site
  const idle = (st: string[]) => rows.filter((r) => !r.ev && st.includes(r.tile?.status ?? 'alarm'))
  const [check, out, watch] = [idle(['alarm', 'stopped']), idle(['out_of_service']), idle(['warning'])]
  const names = (rs: BadRow[]) => rs.map((r) => r.name).join('、')
  const said = [
    main && `Agent Nemo 已擬好處理計畫，${pending.length} 件待核准`,
    working.length > 0 && [...fixing.map((r) => `${r.name}${word(r.ev!).replace('・', '，')}`),
      ...(thinking.length ? [`${names(thinking)}${main ? ' Agent Nemo 還在處理' : ' Agent Nemo 正在處理'}`] : [])].join('；')
      + (main ? '' : '，目前不用你做什麼'),
    check.length > 0 && `${names(check)}沒有在處理中，請到現場確認`,
    out.length > 0 && `${names(out)}已停用，等維修；排程不再排入`,
    watch.length > 0 && `${names(watch)}讀值偏離，事件偵測監看中`,
    calm ? '機台全天自動監看，有異常 1 分鐘內交給 Agent Nemo 處理' : `其他 ${s.header.normal_count} 台正常`,
  ].filter(Boolean).join('。') + '。'
  const k = main ? `要你處理 ${pending.length} 件` : calm ? '全廠正常' : check.length ? '要你到現場確認'
    : working.length ? (thinking.length ? 'Agent Nemo 處理中' : !warm ? '維修中'
      : fixing.every((r) => stage(r.ev!) === 'recovering') ? `維修完成・${recoverWord(expectationOf(fixing[0].ev))}` : '已恢復正常')
    : out.length ? '停用中，等維修' : '監看中'
  const ended = [...check, ...out].find((r) => r.ended?.outcome)?.ended ?? null
  return (
    <section className={`panel mon-answer ${calm ? 'is-calm' : !main && !check.length && warm ? 'is-warm' : ''}`} aria-label="產線現在的結論" data-testid="mon-answer">
      <div>
        <span className="mon-k">{k}</span>
        <h1>{h1}</h1>
        <p>{said}</p>
      </div>
      {main ? (
        <div className="mon-fix" data-testid="monitor-pending">
          <span className="mon-fix-k">系統建議</span>
          <b title={rec ? plain(rec.label) : plain(main.title)}>{who(main)}：{rec ? plain(rec.label) : plain(main.title)}</b>
          {others.length > 0 && <small>另有 {others.length} 件：{others.join('、')}</small>}
          <button type="button" className="mon-btn" onClick={() => onAgent?.(main.event_id)} data-testid="monitor-pending-go">到主控台核准 →</button>
        </div>
      ) : working.length > 0 ? (
        <div className="mon-fix">
          <span className="mon-fix-k">{stage(working[0].ev!) ? '現場進度' : 'Agent Nemo 正在做'}</span>
          <b>{working[0].name}：{word(working[0].ev!)}</b>
          <small>目前沒有要你核准的事</small>
        </div>
      ) : calm ? (
        <div className="mon-fix">
          <span className="mon-fix-k">下一次要有人在硫化機旁</span>
          <b><NextAtPress limit={1} inline /></b>
        </div>
      ) : (
        <div className="mon-fix">
          <span className="mon-fix-k">{ended ? `上一次處理（${machineLabel(ended.machine_id)}）` : 'Agent Nemo 沒有在處理'}</span>
          <b title={ended?.outcome ? plain(ended.outcome) : undefined}>{ended?.outcome ? plain(ended.outcome) : '請到現場確認'}</b>
          <small>{check.length ? '機台還沒恢復正常，請到現場確認' : out.length ? '維修確認後才恢復排入' : '不用你做什麼'}</small>
        </div>
      )}
    </section>
  )
}

/** The abnormal machines, one row each: what is wrong and how far the AI got. The event in focus keeps its evidence
 *  under the list: temperature trend, the AI's judgement, the schedule impact and, folded, what was done. */
function AbnormalPanel({ s, onMachine }: { s: DashboardSnapshot; onMachine: (id: string) => void }) {
  const { state } = useWarRoom()
  const { word } = useStepWord()
  const rows = badRows(s)
  const focus = s.focus && (rows.some((r) => r.id === s.focus!.machine_id) || (s.focus.state !== 'closed' && s.focus.state !== 'safe_stop')) ? s.focus : null
  return (
    <Panel title={`異常機台（${rows.length}）`} className="mon-bad">
      <ul className="mon-bad-list" data-testid="abnormal-list">
        {rows.map((r) => {
          const st = r.ev ? r.ev.state : null
          return (
            <li key={r.id} className={r.tile && (r.tile.status === 'alarm' || r.tile.status === 'stopped' || r.tile.status === 'out_of_service') ? 'is-bad' : ''}>
              {r.id in MACHINE_META
                ? <button type="button" className="mon-m" onClick={() => onMachine(r.id)} title="看溫度、壓力、水流">{r.name}<small>{r.problem}</small></button>
                : <b className="mon-m">{r.name}<small>{r.problem}</small></b>}
              <span className="mon-what">{r.what}</span>
              {st && <span className={`mon-step ${st === 'await_approval' ? 'is-wait' : ''}`}>{word(r.ev!)}</span>}
            </li>
          )
        })}
      </ul>
      {focus && <FocusDetail focus={focus} pending={state.pending} />}
    </Panel>
  )
}

function FocusDetail({ focus, pending }: { focus: FocusPanel; pending: ApprovalRequest[] }) {
  const { state } = useWarRoom()
  const { fastS } = useStepWord()
  const closed = focus.state === 'closed' || focus.state === 'safe_stop'
  const ev = state.snapshot?.events.find((e) => e.id === focus.event_id) ?? null
  // fast repair (/api/health repair_fast_s) for a machine the crew fixes: the expectation without its 30-minute window
  const fast = fastS && fastS > 0 && ev && fastApplies(ev, (state.world?.tasks.items ?? []).filter((t) => t.event_id === ev.id)) ? fastS : null
  const title = focusTitle(focus, ev, [...pending, ...state.resolved])
  return (
    <div className="mon-fd focus" data-testid="focus-detail">
      <h3 className="mon-fd-h focus-title">{closedTitle(plain(title), closed)}</h3>
      <div className={`mon-fd-grid ${ev && ev.hypotheses.length > 0 ? 'has-judge' : ''}`}>
        {focus.machine_id in MACHINE_META && <TemperatureTrend machineId={focus.machine_id} hypotheses={ev?.hypotheses} compact fastS={fast} />}
        {ev && ev.hypotheses.length > 0 && <JudgmentTrail ev={ev} fast={fast} />}
      </div>
      <p className="impact"><b>排程影響<span className="sr">：</span></b>{plain(focus.schedule_impact)}</p>
      <FocusTimeline focus={focus} />
    </div>
  )
}

/** ① 訂單: per-size progress with 剩餘約 N 小時; the pipeline, the open orders with their goods rings and today's counts
 *  fold into 「訂單明細」. Now inside 「更多全廠資訊」: search, ETA vs due and risk are on the 訂單 tab. */
function OrdersPanel({ orders, onOrders }: { orders: OrderProgress[]; onOrders?: () => void }) {
  const w = useWarRoom().state.world
  const open = (w?.orders.items ?? []).filter((o) => o.status === '待排程' || o.status === '生產中')
    .sort((a, b) => Number(b.rush) - Number(a.rush) || (a.due < b.due ? -1 : 1)).slice(0, 4)
  return (
    <Panel title="訂單進度" className="orders"
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
          <summary>訂單明細</summary>
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

/** ② a compact one-line-per-part list. The data has one planned mold-in time for the whole tyre (not per part), so it
 *  sits in the header; the note trails in small text. */
function SemiPanel({ semi }: { semi: SemiFinishedProgress }) {
  return (
    <Panel title={`半成品進度（${semi.tire_label}）`} className="semi"
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

/** What was done for the event in focus and its timeline, folded (the demo opens it here): the last 6 rows, 「看全部」
 *  for the rest. */
function FocusTimeline({ focus }: { focus: FocusPanel }) {
  const [all, setAll] = useState(false)
  const items: TimelineItem[] = focus.timeline
  const nDone = focus.auto_done.length + (focus.approved ?? []).length
  return (
    <details className="focus-tl" data-testid="focus-timeline">
      <summary>處理紀錄（已做 {nDone} 項・時間軸 {items.length} 筆）</summary>
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
          <li className="todo-visual"><span className="ck" aria-hidden>◆</span><span><em className="act-k">待核准<span className="sr">：</span></em>{focus.pending.length > 1 ? `${focus.pending.length} 個方案擇一，` : ''}請到主控台看計畫後決定</span></li>
        )}
      </ul>
      {items.length > 0 && <TimelineTable items={all ? items : items.slice(-6)} compact />}
      {items.length > 6 && (
        <button type="button" className="btn-ghost tl-all" aria-expanded={all} onClick={() => setAll((a) => !a)} data-testid="timeline-all">
          {all ? '只看最近 6 筆' : `看全部（${items.length} 筆）`}
        </button>
      )}
    </details>
  )
}

/** Page 7 "dynamic adjustment": each hypothesis the commander made, what it expected, and what code verified. With fast
 *  repair (/api/health repair_fast_s) the crew fixes it seconds after the approval: no 30-minute window (veteran 10-22). */
function JudgmentTrail({ ev, fast }: { ev: Event; fast: number | null }) {
  return (
    <section className="judge" aria-label="Agent Nemo 的判斷">
      <h4 className="sub">Agent Nemo 的判斷</h4>
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
              {e && <p className="jg-exp">預期：{fast ? '' : `${e.within_min} 分鐘內`}{e.op === '>=' ? `回到 ${e.value}°C 以上` : e.op === '<=' ? `降到 ${e.value}°C 以下` : `維持在 ${e.value}–${e.value_hi}°C`}{fast ? `（核准後約 ${Math.round(fast)} 秒維修完成）` : ''}</p>}
              {h.status === 'rejected' && evidence && <p className="jg-ev">程式驗證：{plain(evidence)}</p>}
            </li>
          )
        })}
      </ol>
    </section>
  )
}

/** All 28 machines, 11 presses first (the bottleneck). Every machine keeps its data-machine / data-line hook: the
 *  ticker and 主控台 scroll here and open the drawer. */
function MachinesPanel({ machines, bad, onMachine, focusId }: { machines: MachineTile[]; bad: BadDataMark[]; onMachine: (id: string) => void; focusId?: string }) {
  const badBy = new Map(bad.map((b) => [b.machine_id, b]))
  const dqText = (id: string) => {
    const b = badBy.get(id)
    return b ? `・壞資料 ${b.count} 筆（${QUALITY_LABEL[b.last_quality] ?? b.last_quality}，未喚醒 AI）` : ''
  }
  const presses = machines.filter((m) => m.line === 'CURE')
  const off = machines.filter((m) => isAbnormal(m.status))
  return (
    <Panel title="機台現況" className="machines" fold="phone"
      summary={off.length ? <span className="late">需要注意：{off.map((m) => machineLabel(m.machine_id)).join('、')}</span> : `${machines.length} 台・全部正常`}>
      <p className="mon-sub">{ML_RANGE} 號硫化機可做中、大胎。點機台看溫度、壓力、水流。</p>
      <div className="presses" data-line="CURE">
        {presses.map((m) => (
          <button key={m.machine_id} className={`mdot press st-${m.status}${m.repaired ? ' repaired' : ''}${badBy.has(m.machine_id) ? ' dq' : ''}${m.machine_id === focusId ? ' is-focus' : ''}`}
            onClick={() => onMachine(m.machine_id)} data-machine={m.machine_id}
            title={`${machineLabel(m.machine_id)}・${STATUS_LABEL[m.status]}${m.temp_c != null ? `・${m.temp_c}°C` : ''}${dqText(m.machine_id)}`}
            aria-label={`${machineLabel(m.machine_id)} ${STATUS_LABEL[m.status]}${m.temp_c != null ? ` ${m.temp_c}°C` : ''}${dqText(m.machine_id)}`}>
            <span className="press-n">{m.short_name}<small>號</small></span>
            <span className="press-t">{m.temp_c != null ? `${Math.round(m.temp_c * 10) / 10}°` : '—'}</span>
            <span className="press-s">{m.repaired ? '檢修完成' : STATUS_LABEL[m.status]}{ML_PRESSES.includes(m.machine_id) && <span className="press-ml" title="可做中、大胎">中大</span>}</span>
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
      {bad.length > 0 && (
        <p className="note dq-note" data-testid="bad-data-note"><span className="dq-tag">資料</span> 驗證層攔下 {bad.reduce((n, b) => n + b.count, 0)} 筆壞資料（近 30 分）：{bad.map((b) => `${machineLabel(b.machine_id)} ${b.count} 筆・${QUALITY_LABEL[b.last_quality] ?? b.last_quality}`).join('；')}。壞資料不進預測、不喚醒 AI。</p>
      )}
    </Panel>
  )
}

/** The event's title, rebuilt from the live event: while it is open after a replan the round-2 approval title (e.g.
 *  7 號機停用，重排中) leads, so the heading never contradicts the phone. */
function focusTitle(focus: FocusPanel, ev: Event | null, reqs: ApprovalRequest[]): string {
  if (!ev) return focus.title
  const rejected = ev.hypotheses.filter((h) => h.status === 'rejected')
  const latestRound = reqs.filter((r) => r.event_id === ev.id).sort((a, b) => b.round - a.round || (a.created_at < b.created_at ? 1 : -1))[0]
  const open = ev.state !== 'closed' && ev.state !== 'safe_stop'
  return open && rejected.length > 0 && latestRound && latestRound.round > 1 ? `${machineLabel(ev.machine_id)}：${latestRound.title}` : focus.title
}

/** "supervisor" is the API default for a phone decision; show the role in Chinese. */
function whoLabel(by: string): string {
  return !by || by === 'supervisor' ? '主管' : by
}
