// Live state-machine diagram for one event (CJ, 2026-10-20: 「我看得到狀態機裡面的 loop 嗎？」): the 11 states of
// warroom/contracts.py with every allowed transition, the current state lit, the path taken so far with the time on
// each edge (×n when taken more than once), the replan loop (SIMULATE → … → VERIFY → REPLAN → SIMULATE) marked
// 「第 n 輪」 (from round 2), and the failures against MAX_CONSECUTIVE_FAILURES (once there is one). Clicking a state filters
// the transcript.
import type { Event, EventState } from '../contracts'
import { fmtHM, fmtHMS } from '../format'
import { STATE_LABEL } from '../components/common'
import { ALLOWED_TRANSITIONS, MAX_CONSECUTIVE_FAILURES, STATES, STATE_CODE, type Hop } from './stateGraph'

const W = 1000, NODE_W = 96, NODE_H = 46
// main row left → right, REPLAN under the loop, SAFE_STOP bottom right
const POS: Record<EventState, [number, number]> = {
  detected: [56, 80], classify: [168, 80], plan: [280, 80], simulate: [392, 80], gate: [504, 80], await_approval: [616, 80],
  execute: [728, 80], verify: [840, 80], closed: [945, 80], replan: [616, 200], safe_stop: [945, 200],
}
const LOOP = new Set(['verify>replan', 'replan>simulate', 'gate>replan', 'await_approval>replan', 'simulate>replan'])

function edgePath(a: EventState, b: EventState): { d: string; lx: number; ly: number } {
  const [x1, y1] = POS[a], [x2, y2] = POS[b]
  const hw = NODE_W / 2, hh = NODE_H / 2
  if (y1 === y2 && x2 > x1 && Math.abs(x2 - x1) < 130) return { d: `M${x1 + hw},${y1} L${x2 - hw},${y2}`, lx: (x1 + x2) / 2, ly: y1 - hh - 6 }
  if (a === 'classify' && b === 'closed') return { d: `M${x1},${y1 - hh} C${x1},${y1 - 70} ${x2},${y2 - 70} ${x2},${y2 - hh}`, lx: (x1 + x2) / 2, ly: y1 - 56 }
  if (a === 'execute' && b === 'await_approval') return { d: `M${x1 - 20},${y1 - hh} C${x1 - 30},${y1 - 44} ${x2 + 30},${y2 - 44} ${x2 + 20},${y2 - hh}`, lx: (x1 + x2) / 2, ly: y1 - 40 }
  if (b === 'safe_stop') return { d: `M${x1 + (a === 'closed' ? 0 : 18)},${y1 + hh} C${x1 + 18},${y2} ${x2 - 60},${y2} ${x2 - hw},${y2}`, lx: (x1 + x2) / 2, ly: y2 - 6 }
  if (b === 'replan') return { d: `M${x1},${y1 + hh} C${x1},${y2 - 10} ${x2 + (x1 > x2 ? 60 : -60)},${y2} ${x2 + (x1 > x2 ? hw : -hw)},${y2}`, lx: (x1 + x2) / 2 + (x1 > x2 ? 10 : -10), ly: y2 - 30 }
  if (a === 'replan' && b === 'simulate') return { d: `M${x1 - hw},${y1 + 6} C${x2 - 10},${y1 + 20} ${x2 - 10},${y2 + 40} ${x2 - 10},${y2 + hh}`, lx: x2 + 40, ly: y1 + 30 }
  return { d: `M${x1},${y1} L${x2},${y2}`, lx: (x1 + x2) / 2, ly: (y1 + y2) / 2 }
}

export function StateDiagram({ ev, path, pick, onPick }: { ev: Event; path: Hop[]; pick: EventState | null; onPick: (s: EventState | null) => void }) {
  const taken = new Map<string, Hop[]>()
  for (const h of path) if (h.from) taken.set(`${h.from}>${h.to}`, [...(taken.get(`${h.from}>${h.to}`) ?? []), h])
  const visited = new Set(path.map((h) => h.to))
  const round = (ev.prior_rounds?.length ?? 0) + 1
  const loops = taken.get('replan>simulate')?.length ?? 0
  const fails = ev.consecutive_failures ?? 0
  return (
    <section className="sm" aria-label="狀態機" data-testid="state-diagram">
      <header className="sm-h">
        <h3 className="sub">狀態機<small>（點狀態篩選下方紀錄{pick ? `・目前：${STATE_LABEL[pick]}` : ''}）</small></h3>
        <div className="sm-stats">
          {/* veteran audit: the round from round 2 on, the failures only when there are any */}
          {round > 1 && <span className={loops ? 'loop' : ''} data-testid="sm-round">第 {round} 輪{ev.replans ? `・重新判斷 ${ev.replans} 次` : ''}</span>}
          {fails > 0 && <span className={fails >= MAX_CONSECUTIVE_FAILURES ? 'bad' : 'warn'}>
            連續失敗 {fails} 次，{fails >= MAX_CONSECUTIVE_FAILURES ? '已安全停止' : `再 ${MAX_CONSECUTIVE_FAILURES - fails} 次就安全停止`}</span>}
          {pick && <button className="btn-ghost" onClick={() => onPick(null)}>清除篩選</button>}
        </div>
      </header>
      <div className="sm-wrap">
        <svg viewBox={`0 0 ${W} 240`} role="img" aria-label={`狀態機：目前 ${STATE_LABEL[ev.state]}，走過 ${path.length} 步`}>
          <defs>
            <marker id="sm-arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" className="sm-arr" /></marker>
            <marker id="sm-arr-on" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" className="sm-arr-on" /></marker>
          </defs>
          {STATES.flatMap((a) => ALLOWED_TRANSITIONS[a].map((b) => {
            const k = `${a}>${b}`
            const hops = taken.get(k)
            const { d, lx, ly } = edgePath(a, b)
            const loop = LOOP.has(k)
            return (
              <g key={k} className={`sm-e ${hops ? 'on' : ''} ${loop ? 'loop' : ''} ${b === 'safe_stop' && !hops ? 'rail' : ''}`} data-edge={k}>
                <path d={d} markerEnd={`url(#${hops ? 'sm-arr-on' : 'sm-arr'})`} />
                {hops && (
                  <text x={lx} y={ly} textAnchor="middle" className="sm-t">
                    {fmtHM(hops[hops.length - 1].ts)}{hops.length > 1 ? ` ×${hops.length}` : ''}
                    {k === 'replan>simulate' ? `・第 ${hops.length + 1} 輪` : ''}
                    <title>{hops.map((h) => fmtHMS(h.ts)).join('、')}</title>
                  </text>
                )}
              </g>
            )
          }))}
          {STATES.map((s) => {
            const [x, y] = POS[s]
            const cur = ev.state === s
            return (
              <g key={s} className={`sm-n ${cur ? 'cur' : ''} ${visited.has(s) ? 'seen' : ''} ${pick === s ? 'pick' : ''} st-${s}`} role="button" tabIndex={0}
                aria-pressed={pick === s} aria-label={`${STATE_LABEL[s]}（${STATE_CODE[s]}）${cur ? '，目前' : ''}`} data-state={s}
                onClick={() => onPick(pick === s ? null : s)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(pick === s ? null : s) } }}>
                <rect x={x - NODE_W / 2} y={y - NODE_H / 2} width={NODE_W} height={NODE_H} rx={10} />
                <text x={x} y={y - 3} textAnchor="middle" className="sm-l">{STATE_LABEL[s]}</text>
                <text x={x} y={y + 13} textAnchor="middle" className="sm-c">{STATE_CODE[s]}</text>
              </g>
            )
          })}
        </svg>
      </div>
    </section>
  )
}
