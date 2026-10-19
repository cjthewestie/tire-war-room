// 發車看板 (DESIGN.md, CJ chose 台鐵發車時刻表 on 2026-10-20): every open event, its pending approval or a
// supervisor task is one "train" — 時間｜機台｜狀態｜下一步｜負責人 — most urgent on top in arrival orange.
// The plant status (normal / abnormal counts, pending approvals) folds into the board header; clicking it
// opens 產線監控. A row flips cell by cell (split-flap) when its status, plan version or approval changes.
import type { Event } from '../contracts'
import { Flap } from '../components/Flap'
import { FLAP_STEP_MS, useFlipGen } from '../components/flip'
import { boardRows, type BoardRowData } from './boardRows'
import { useWarRoom } from '../useWarRoom'

export function BoardRow({ r, first, selected, onPick, compact = false }: {
  r: BoardRowData; first: boolean; selected?: boolean; onPick?: (id: string) => void; compact?: boolean
}) {
  const gen = useFlipGen(r.sig)
  // cells flip left to right, each one starting after the cells before it (≈ a train arriving on the board)
  const len = (t: string) => Math.min(Array.from(t).length, 16) * FLAP_STEP_MS
  const tTime = 0, tMach = len(r.time), tStat = tMach + len(r.machine), tNext = tStat + len(r.status), tOwn = tNext + len(r.next)
  const cls = `brow sev-${r.sev} tone-${r.tone} ${first ? 'is-first' : ''} ${selected ? 'is-selected' : ''} ${gen ? 'is-changed' : ''}`
  const body = (
    <>
      <span className="bc bc-time"><Flap text={r.time} gen={gen} delay={tTime} /></span>
      <span className="bc bc-mach"><Flap text={r.machine} gen={gen} delay={tMach} /></span>
      <span className="bc bc-stat"><span className={`bstat tone-${r.tone}`}><Flap text={r.status} gen={gen} delay={tStat} /></span></span>
      <span className="bc bc-next"><Flap text={r.next} gen={gen} delay={tNext} /></span>
      {!compact && <span className="bc bc-own"><Flap text={r.owner} gen={gen} delay={tOwn} /></span>}
    </>
  )
  if (!onPick) return <div className={cls} data-testid={`board-row-${r.id}`}>{body}</div>
  return (
    <button type="button" className={cls} aria-pressed={!!selected} onClick={() => onPick(r.id)} data-testid={`board-row-${r.id}`}
      aria-label={`${r.time} ${r.machine} ${r.status}，下一步：${r.next}，負責人：${r.owner}`}>{body}</button>
  )
}

const NET_SHORT: Record<string, string> = { online: '網路正常', weak: '弱網', wan_down: 'WAN 中斷', lan_down: 'LAN 中斷・現場操作' }

export function DepartureBoard({ events, picked, onPick, onMonitor }: {
  events: Event[]; picked: string | null; onPick: (id: string) => void; onMonitor?: () => void
}) {
  const { state } = useWarRoom()
  const h = state.snapshot?.header
  const rows = boardRows(events, state.pending)
  const pend = state.pending.length
  const net = h?.network ?? 'online'
  const abn = h?.abnormal_count ?? 0
  return (
    <section className="board" aria-label="處理中事件看板">
      <button type="button" className="board-h" onClick={onMonitor} data-testid="status-strip" title="打開完整的產線監控">
        <span className="bh-title">處理中事件</span>
        <span className="bh-stat"><i className="dot ok" />正常 <b>{h?.normal_count ?? '–'}</b> 台</span>
        <span className={`bh-stat ${abn > 0 ? 'bad' : ''}`}><i className={`dot ${abn > 0 ? 'bad' : 'idle'}`} />異常 <b>{h?.abnormal_count ?? '–'}</b> 台</span>
        <span className={`bh-stat ${pend ? 'wait' : ''}`}>待核准 <b>{pend}</b> 件</span>
        {net !== 'online' && <span className="bh-stat bad">{NET_SHORT[net] ?? net}</span>}
        <span className="bh-go">產線監控<svg viewBox="0 0 16 16" aria-hidden><path d="M3 8h9M8 4l4 4-4 4" /></svg></span>
      </button>
      <div className="board-table">
        <div className="brow brow-head" aria-hidden>
          <span className="bc bc-time">時間</span><span className="bc bc-mach">機台</span>
          <span className="bc bc-stat">狀態</span><span className="bc bc-next">下一步</span>
          <span className="bc bc-own">負責人</span>
        </div>
        <div className="board-rows" role="group" aria-label="事件，最急的在最上面">
          {rows.length === 0 && <p className="board-empty">全廠正常・目前沒有事件。事件偵測常駐監看，不經 AI。</p>}
          {rows.map((r, i) => <BoardRow key={r.id} r={r} first={i === 0 && r.tone !== 'done'} selected={r.id === picked} onPick={onPick} />)}
        </div>
      </div>
    </section>
  )
}
