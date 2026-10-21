// 「看一次怎麼運作」: pick an event, then watch every page that should change. The tracker is ticked by the server from
// backend state (audit rows, events, approvals, machine status), refreshed on every live WebSocket update and once a
// second; each tick shows the factory time of the row that proved it. 「看」 opens that page beside the tracker (wide
// screens) or in a new tab. The walkthrough pauses at the approval; CJ approves on the phone or the plan card.
import { useCallback, useEffect, useRef, useState } from 'react'
import { useWarRoom } from '../useWarRoom'
import { getRegistry, getWalk, hms, startWalk, STATE_LABEL, walkControl, type WalkKind, type WalkView } from './qaDemo'

const SPEEDS = [1, 5, 10]

function Stepper({ w }: { w: WalkView }) {
  if (!w.states.length) return <p className="qa-dim" data-testid="qa-stepper">等偵測器發現異常…</p>
  return (
    <ol className="qa-stepper" data-testid="qa-stepper" aria-label="事件狀態">
      {w.states.map((s, i) => (
        <li key={i} className={`${s.llm_calls ? 'ai' : ''} ${i === w.states.length - 1 && !w.closed ? 'now' : ''}`}>
          <b>{STATE_LABEL[s.state] ?? s.state}</b>
          <span>{hms(s.at)}・{s.seconds < 60 ? `${s.seconds} 秒` : `${(s.seconds / 60).toFixed(1)} 分`}</span>
          {s.llm_calls > 0 && <i title="這個狀態呼叫了模型">AI ×{s.llm_calls}</i>}
        </li>
      ))}
    </ol>
  )
}

export function QaWalk({ autostart, onStarted }: { autostart?: string | null; onStarted?: () => void } = {}) {
  const { state } = useWarRoom()
  const [kinds, setKinds] = useState<WalkKind[]>([])
  const [speed, setSpeed] = useState(5)
  const [pauseAt, setPauseAt] = useState(true)
  const [walk, setWalk] = useState<WalkView | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [view, setView] = useState<{ href: string; page: string } | null>(null)
  const last = useRef(0)
  const wide = typeof window !== 'undefined' && window.matchMedia?.('(min-width: 1100px)').matches

  useEffect(() => { getRegistry().then((r) => setKinds(r.walks)).catch((e) => setErr(String(e))) }, [])
  const refresh = useCallback(() => {
    if (!walk) return
    last.current = Date.now()
    getWalk(walk.id).then(setWalk).catch(() => { /* next tick */ })
  }, [walk])
  useEffect(() => {                         // every live update from the plant, at most twice a second
    if (walk && Date.now() - last.current > 500) refresh()
  }, [state, walk, refresh])
  useEffect(() => {
    if (!walk) return
    const t = setInterval(refresh, 1000)
    return () => clearInterval(t)
  }, [walk, refresh])

  const start = async (k: WalkKind, at?: number) => {
    if (busy) return
    const reg = await getRegistry().catch(() => null)
    const reload = k.how === 'scenario'
    const msg = reload
      ? `「${k.title}」會重新載入提案 p.11 的情境（目前的模擬事件與待核准會換掉）。要繼續嗎？`
      : `現在有 ${reg?.open_events.length ?? 0} 件處理中、${reg?.pending_approvals.length ?? 0} 張待核准。「${k.title}」會疊加在目前的模擬上。要繼續嗎？`
    if ((reload || reg?.busy) && !window.confirm(msg)) return
    setBusy(true); setErr(null); setView(null)
    if (at) setSpeed(at)
    try { setWalk(await startWalk(k.id, at ?? speed, pauseAt)) } catch (e) { setErr(`沒有開始：${String(e instanceof Error ? e.message : e)}`) } finally { setBusy(false) }
  }
  useEffect(() => {                         // 「▶ 跑一次」 from a proof: start that event once the cards are loaded
    const k = autostart ? kinds.find((x) => x.id === autostart) : undefined
    if (!k) return
    onStarted?.()
    queueMicrotask(() => void start(k, 10))
  }, [autostart, kinds])  // eslint-disable-line react-hooks/exhaustive-deps
  const control = async (action: 'play' | 'pause' | 'speed', s?: number) => {
    if (!walk) return
    if (s) setSpeed(s)
    try { setWalk(await walkControl(walk.id, action, s)) } catch (e) { setErr(String(e)) }
  }
  const open = (href: string, page: string, external: boolean) => {
    if (external || !wide) { window.open(href, '_blank', 'noopener'); return }
    setView({ href: href.startsWith('#') ? `/${href}` : href, page })
  }

  return (
    <section className="qa-sec" aria-labelledby="qa-walk-h" data-testid="qa-walk">
      <h3 id="qa-walk-h">看一次怎麼運作</h3>
      <p className="qa-dim">選一個事件，系統在模擬裡真的跑一次（標記「QA 展示」）。下面的清單會在每一頁真的變化時自動打勾，附上工廠時間。</p>
      <div className="qa-walk-opts">
        <span>速度</span>
        {SPEEDS.map((s) => <button key={s} className={`qa-seg ${speed === s ? 'on' : ''}`} aria-pressed={speed === s}
          onClick={() => (walk ? void control('speed', s) : setSpeed(s))}>{s}×</button>)}
        <label><input type="checkbox" checked={pauseAt} onChange={(e) => setPauseAt(e.target.checked)} />在核准前暫停，讓 CJ 說明</label>
      </div>
      <div className="qa-picker" role="list" aria-label="事件">
        {kinds.map((k) => (
          <button key={k.id} role="listitem" className={`qa-pick ${walk?.kind === k.id ? 'on' : ''}`} disabled={busy}
            onClick={() => void start(k)} data-testid="qa-walk-pick" data-kind={k.id}>
            <b>{k.title}</b><span>{k.line}</span><small>{k.path}</small>
          </button>
        ))}
      </div>
      {err && <p className="qa-err" role="alert">{err}</p>}
      {walk && (
        <div className={`qa-walk-run ${view ? 'split' : ''}`}>
          <div className="qa-walk-left" data-testid="qa-tracker">
            <div className="qa-walk-head">
              <b>{walk.title}</b>
              <span className="qa-dim">工廠時間 {hms(walk.factory_time)}{walk.event_id ? `・${walk.event_id}` : ''}・稽核 {walk.audit_rows} 列</span>
              <span className="qa-walk-ctl">
                {walk.paused ? <button onClick={() => void control('play')} data-testid="qa-walk-play">▶ 繼續</button>
                  : <button onClick={() => void control('pause')}>⏸ 暫停</button>}
              </span>
            </div>
            {walk.waiting_approval.length > 0 && (
              <p className="qa-walk-wait" role="status">等主管核准：請在手機或主控台的計劃卡上決定{walk.paused ? '（模擬已暫停，核准後按「繼續」）' : ''}。</p>
            )}
            {walk.closed && <p className="qa-walk-done" role="status">這件已結束：看「稽核／報告」的結案報告。</p>}
            <Stepper w={walk} />
            <ul className="qa-track">
              {walk.tracker.map((t) => {
                const all = t.checks.every((c) => c.done)
                return (
                  <li key={t.page} className={all ? 'done' : ''}>
                    <div className="qa-track-h">
                      <b>{t.page}</b><span className="qa-dim">{t.label}</span>
                      <button className="qa-look" onClick={() => open(t.href, t.page, t.external)} data-testid="qa-track-look">看{t.external ? ' ↗' : ''}</button>
                    </div>
                    <ul>
                      {t.checks.map((c) => (
                        <li key={c.what} className={c.done ? 'ok' : ''}>
                          <span className="qa-tick">{c.done ? '✓' : '○'}</span>{c.what}{c.at && <time>{hms(c.at)}</time>}
                        </li>
                      ))}
                    </ul>
                  </li>
                )
              })}
            </ul>
          </div>
          {view && (
            <div className="qa-walk-right">
              <div className="qa-walk-frame-h"><b>{view.page}</b><a href={view.href} target="_blank" rel="noopener">另開 ↗</a>
                <button className="x" onClick={() => setView(null)} aria-label="關閉">✕</button></div>
              <iframe title={view.page} src={view.href} />
            </div>
          )}
        </div>
      )}
    </section>
  )
}
