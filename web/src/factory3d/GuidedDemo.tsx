// The guided demo's on-screen parts (the hook is useGuidedDemo.ts): the cue card with its 「開啟」 buttons, the
// full-screen cue (3:00 pitch, 6:00 end), the pitch countdown, the control bar, and the full-screen slide view.
import { useEffect, useMemo, useState } from 'react'
import { DEMO_TOTAL, PITCH_AT, WARN_AT, mss, type CueLink } from './demoCues'
import type { GuidedDemo } from './useGuidedDemo'

const LINK: Record<CueLink, { label: string; win: string }> = {
  console: { label: '開啟主控台', win: 'tw-console' }, phone: { label: '開啟手機頁', win: 'tw-phone' }, agent: { label: '開啟 Agent 頁面', win: 'tw-agent' },
}

/** Where a cue's 「開啟」 button goes. */
function href(link: CueLink, eventId: string | null) {
  if (link === 'console') return '/#agent'
  if (link === 'phone') return '/phone'
  return eventId ? `/#console?event=${encodeURIComponent(eventId)}` : '/#console'
}

/** The cue card, the full-screen cue, the pitch countdown and the control bar. */
export function DemoOverlay({ demo, slides, onPhone }: { demo: GuidedDemo; slides: number; onPhone: () => void }) {
  const { running, t, cue, next, paused } = demo
  const [now, setNow] = useState(() => performance.now())
  useEffect(() => { if (!running) return; const id = setInterval(() => setNow(performance.now()), 250); return () => clearInterval(id) }, [running])
  const big = cue.big && now < demo.bigUntil
  const pitch = t >= PITCH_AT
  const left = DEMO_TOTAL - t
  const warn = t >= WARN_AT
  const links = useMemo(() => cue.links ?? [], [cue])
  useEffect(() => {
    if (!demo.note) return
    const id = setTimeout(() => demo.setNote(null), 5000)
    return () => clearTimeout(id)
  }, [demo])
  if (!running) return null
  return (
    <>
      {big ? (
        <div className={`f3-cue-big ${cue.id}`} role="status" data-testid="demo-cue-big"><div><b>{cue.title}</b><span>{cue.body}</span></div></div>
      ) : (
        <div className={`f3-cue ${pitch ? 'pitch' : ''} ${warn ? 'warn' : ''}`} role="status" data-testid="demo-cue" key={cue.id}>
          <i>{mss(t)}</i>
          <b>{cue.title}</b>
          <span>{cue.body}</span>
          {(links.length > 0 || cue.id === 'phone') && (
            <div className="f3-cue-links">
              {cue.id === 'phone' && <button onClick={onPhone} data-testid="demo-raise-phone">拿起手機（P）</button>}
              {links.map((l) => (
                <button key={l} data-testid={`demo-open-${l}`} onClick={() => window.open(href(l, demo.incidentEventId), LINK[l].win)}>開啟・{LINK[l].label.replace('開啟', '')}</button>
              ))}
            </div>
          )}
          {pitch && slides > 0 && <em>投影片 {demo.slide + 1} / {slides}・←／→ 換頁・點白板全螢幕</em>}
        </div>
      )}
      {pitch && <div className={`f3-pitch-clock ${warn ? 'warn' : ''}`} data-testid="demo-pitch-clock">{left > 0 ? <>Pitch 剩 <b>{mss(left)}</b></> : <b>時間到</b>}</div>}
      <div className="f3-demo-bar" data-testid="demo-bar">
        <span className="f3-demo-time"><b>{mss(t)}</b> / 6:00</span>
        <span className="f3-demo-next">{next ? <>下一步 <b>{mss(next.at)}</b> {next.title}</> : '最後一步'}</span>
        {demo.note && <span className="f3-demo-warn">{demo.note}</span>}
        <button onClick={demo.togglePause} title="暫停／繼續（B）" data-testid="demo-pause">{paused ? '▶ 繼續' : '❚❚ 暫停'}</button>
        <button onClick={demo.skip} title="下一步（N）" data-testid="demo-next" disabled={!next}>⏭ 下一步</button>
        <button onClick={() => void demo.begin()} title="重來（G）" data-testid="demo-restart">↻ 重來</button>
        <button onClick={demo.stop} title="結束 Demo" data-testid="demo-stop">✕</button>
      </div>
    </>
  )
}

/** Full-screen slide view (click the whiteboard). */
export function SlideView({ slides, index, onIndex, onClose }: { slides: string[]; index: number; onIndex: (i: number) => void; onClose: () => void }) {
  const go = (d: number) => onIndex(Math.max(0, Math.min(slides.length - 1, index + d)))
  return (
    <div className="f3-slides" data-testid="demo-slides" onClick={onClose}>
      <img src={slides[index]} alt={`投影片 ${index + 1}`} onClick={(e) => { e.stopPropagation(); go(1) }} />
      <div className="f3-slides-bar" onClick={(e) => e.stopPropagation()}>
        <button onClick={() => go(-1)} disabled={index === 0}>←</button>
        <span>{index + 1} / {slides.length}</span>
        <button onClick={() => go(1)} disabled={index >= slides.length - 1}>→</button>
        <button onClick={onClose}>✕ 關閉（Esc）</button>
      </div>
    </div>
  )
}

