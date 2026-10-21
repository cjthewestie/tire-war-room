// 今日工廠概況: the 主控台 middle column when nothing needs a human (agreed 2026-10-21, item 2). It used to be blank
// (PlanArea returned null); IA prototype v12 (docs/ux/war-room-ia-prototype.html): 「沒事時不是空白：告訴你下一件會發生的事」.
// One home per piece of info: summaries and links only — machine detail stays on 產線監控, each event's full process on
// the Agent page (#console?event=<id>). Sections: the result strip of the latest decision (until something new arrives),
// 今日進度, 機台摘要, 接下來, 今天處理過的事 (history.ts, kept across scenario switches) and, live only, 人力・出貨・品質.
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Shift } from '../contracts'
import { factoryMidnight, fmtHM, fmtMD, fmtNum, fmtWeekday, ms } from '../format'
import { useHistory, type HistRow } from '../history'
import { useWarRoom } from '../useWarRoom'
import { machineSummary, upcoming, when } from './overviewModel'
import { ResultStrip } from './ResultStrip'

const H = 3_600_000
/** 「剩餘約 6.5 小時」; past two days in days (「剩餘約 11 天」), which an operator reads faster than 265 小時. */
const remaining = (h: number) => (h < 48 ? `剩餘約 ${fmtNum(h)} 小時` : `剩餘約 ${Math.round(h / 24)} 天`)

export function HomeOverview({ onMonitor, onFull }: { onMonitor?: () => void; onFull?: (eventId?: string) => void }) {
  const { state, transport } = useWarRoom()
  const hist = useHistory()
  const snap = state.snapshot
  const now = ms(state.clock?.factory_time ?? snap?.header.factory_time)
  const day = isNaN(now) ? 0 : Math.floor(now / (6 * H))
  const [shifts, setShifts] = useState<Shift[]>([])
  useEffect(() => { transport?.shifts().then(setShifts).catch(() => {}) }, [transport, day])
  const minute = isNaN(now) ? NaN : Math.floor(now / 60_000) * 60_000
  const next = useMemo(() => upcoming(state.schedule, state.world, shifts, minute), [state.schedule, state.world, shifts, minute])
  const sum = useMemo(() => machineSummary(snap?.machines ?? []), [snap?.machines])
  const events = useMemo(() => snap?.events ?? [], [snap?.events])
  const d0 = isNaN(now) ? 0 : factoryMidnight(now)
  const today = hist.rows.filter((r) => r.run === hist.run && ms(r.at) >= d0)
  const before = hist.rows.filter((r) => r.run < hist.run)
  // the strip stays on the latest decision until the next event or approval arrives
  const last = hist.rows.find((r) => r.run === hist.run && r.kind === 'decision' && r.tag !== '逾時未決定')
  const lastEv = last ? events.find((e) => e.id === last.eventId) : undefined
  const stripEv = last && lastEv && !events.some((e) => e.id !== lastEv.id && e.opened_at > last.at) && !state.pending.length ? lastEv : null
  const full = (id: string) => (e: React.MouseEvent) => { if (onFull) { e.preventDefault(); onFull(id) } }
  const w = state.world
  return (
    <section className="plan-area panel plan-other home-ov" aria-label="今日工廠概況" data-testid="home-overview">
      <div className="po-body">
        <header className="ov-head">
          <h2>今日工廠概況</h2>
          {!isNaN(now) && <span className="muted small">{fmtMD(now)}（{fmtWeekday(now)}）{fmtHM(now)}</span>}
        </header>
        {stripEv && <ResultStrip ev={stripEv} subject={last?.subject} onFull={onFull} />}

        <Sec title="今日進度" testId="ov-progress">
          {snap?.orders.length ? (
            <ul className="ov-prog">
              {snap.orders.map((o) => {
                const pct = o.total ? Math.min(100, Math.round((100 * o.done) / o.total)) : 0
                return (
                  <li key={o.size}>
                    <span className="ov-pl">{o.label}</span>
                    <span className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={o.total} aria-valuenow={o.done} aria-label={`${o.label}完成 ${o.done}/${o.total} 條`}>
                      <i style={{ width: `${pct}%`, background: `var(--sz-${o.size.toLowerCase()})` }} />
                    </span>
                    <span className="ov-pn"><b>{o.done}</b>/{o.total} 條</span>
                    <span className="ov-pr muted">{o.done >= o.total ? '已完成' : remaining(o.remaining_h)}</span>
                  </li>
                )
              })}
            </ul>
          ) : <p className="muted ov-empty">今天沒有排定的硫化量</p>}
        </Sec>

        <Sec title="機台摘要" testId="ov-machines"
          link={<a className="panel-link" href="#monitor" onClick={(e) => { if (onMonitor) { e.preventDefault(); onMonitor() } }} data-testid="ov-monitor">看產線監控 →</a>}>
          {sum.press || sum.other
            ? <p className={`ov-line ${sum.bad ? 'has-bad' : ''}`}>{sum.press && <span>{sum.press}</span>}{sum.other && <span>{sum.other}</span>}</p>
            : <p className="muted ov-empty">還沒收到機台資料</p>}
        </Sec>

        <Sec title="接下來" testId="ov-next">
          {next.length ? (
            <ul className="ov-list">{next.map((u) => <li key={u.key}><span className="ov-time">{u.when}</span><span>{u.text}</span></li>)}</ul>
          ) : <p className="muted ov-empty">排程裡接下來沒有出模或入模；有新排程會出現在這裡</p>}
        </Sec>

        <Sec title="今天處理過的事" testId="ov-done">
          {today.length || before.length ? (
            <ul className="ov-list ov-hist">
              {today.map((r) => <HistLine key={r.key} r={r} now={now} href={events.some((e) => e.id === r.eventId) ? r.eventId : null} onClick={full} />)}
              {!today.length && <li className="muted ov-empty">今天還沒有處理過的事</li>}
              {before.length > 0 && <li className="ov-sub" aria-hidden="true">上一輪（切換情境前）</li>}
              {before.map((r) => <HistLine key={r.key} r={r} now={now} href={null} old onClick={full} />)}
            </ul>
          ) : <p className="muted ov-empty">今天還沒有處理過的事；事件結案或你做了決定，會記在這裡</p>}
        </Sec>

        {w && (
          <Sec title="人力・出貨・品質" testId="ov-world">
            <p className="ov-line">
              <span>在廠 <b>{w.people.on_duty}</b>/{w.people.total} 人</span>
              <span>待出貨 <b>{w.finished_goods.waiting_ship}</b> 條{w.dock.next_departure && ms(w.dock.next_departure) > now ? `・下一車 ${when(ms(w.dock.next_departure), now)}` : ''}</span>
              <span>良率 <b>{w.quality.yield_pct != null ? `${fmtNum(w.quality.yield_pct)}%` : '—'}</b>・今日不良 {w.quality.defects_today} 條（已檢 {w.quality.inspected_today}）</span>
            </p>
          </Sec>
        )}
      </div>
    </section>
  )
}

function Sec({ title, testId, link, children }: { title: string; testId: string; link?: ReactNode; children: ReactNode }) {
  return (
    <div className="ov-sec" data-testid={testId}>
      <h3 className="ov-h">{title}{link}</h3>
      {children}
    </div>
  )
}

/** One row: time, what (subject・text：outcome), its tag; a row of this run opens the event on the Agent page. */
function HistLine({ r, now, href, old = false, onClick }: { r: HistRow; now: number; href: string | null; old?: boolean; onClick: (id: string) => (e: React.MouseEvent) => void }) {
  const body = <>
    <span className="ov-time">{old ? when(ms(r.at), now) : fmtHM(r.at)}</span>
    <span className="ov-what">{r.subject && <b>{r.subject}・</b>}{r.text}{r.detail && <span className="muted">：{r.detail}</span>}</span>
    <span className="ov-tags"><span className={`ov-tag t-${r.tone}`}>{r.tag}</span>{old && <span className="ov-tag t-old">上一輪</span>}</span>
  </>
  return (
    <li className={old ? 'old' : ''} data-testid="ov-done-row" data-run={old ? 'prev' : 'cur'}>
      {href ? <a className="ov-row" href={`#console?event=${encodeURIComponent(href)}`} onClick={onClick(href)}>{body}</a>
        : <div className="ov-row" title={old ? '上一輪的紀錄：切換情境後模擬已重建，這件事的完整過程不在這一輪' : undefined}>{body}</div>}
    </li>
  )
}
