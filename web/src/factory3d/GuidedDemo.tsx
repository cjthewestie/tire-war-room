// The guided demo's on-screen parts (the hook is useGuidedDemo.ts): the 「🎬 Demo 模式・視角鎖定」 badge, the cue card
// with its 「開啟」 buttons and the checkpoint prompt 「按 A 繼續」, the full-screen cue (pitch, end), the story clock with
// its soft target, the pitch countdown with the slide's pacing, the control bar, the exit confirm and the full-screen
// slide view.
import { useEffect, useMemo, useState } from 'react'
import { mss, type CueLink } from '../demo/cues'
import { PAD_HELP } from './gamepad'
import type { GuidedDemo } from './useGuidedDemo'

const LINK: Record<CueLink, { label: string; win: string }> = {
  console: { label: '主控台', win: 'tw-console' }, phone: { label: '手機頁', win: 'tw-phone' }, agent: { label: 'Agent 頁面', win: 'tw-agent' },
}
const CIRCLED = '①②③④⑤⑥⑦⑧⑨'

/** Where a cue's 「開啟」 button goes. */
function href(link: CueLink, eventId: string | null) {
  if (link === 'console') return '/#agent'
  if (link === 'phone') return '/phone'
  return eventId ? `/#console?event=${encodeURIComponent(eventId)}` : '/#console'
}

export function DemoOverlay({ demo, deck, onPhone, pad }: {
  demo: GuidedDemo
  deck: { slides: string[]; titles: (string | null)[]; seconds: (number | null)[] }
  onPhone: () => void
  /** a gamepad is connected: show its button names (A, Menu…) instead of the keys */
  pad: boolean
}) {
  const { running, t, cue, next, paused, waiting } = demo
  const [now, setNow] = useState(() => performance.now())
  useEffect(() => { if (!running) return; const id = setInterval(() => setNow(performance.now()), 250); return () => clearInterval(id) }, [running])
  const big = cue.big && now < demo.bigUntil
  const pitch = demo.pitchLeft != null
  const left = demo.pitchLeft ?? 0
  const links = useMemo(() => cue.links ?? [], [cue])
  useEffect(() => {
    if (!demo.note) return
    const id = setTimeout(() => demo.setNote(null), 5000)
    return () => clearTimeout(id)
  }, [demo])
  if (!running) return null
  const contKey = pad ? 'A' : 'Space'
  const secs = deck.seconds[demo.slide]
  return (
    <>
      <div className="f3-demo-badge" data-testid="demo-badge">🎬 Demo 模式・視角鎖定{demo.checkpoint >= 0 && <b>{CIRCLED[demo.checkpoint]} / {demo.checkpoints}</b>}</div>
      {big ? (
        <div className={`f3-cue-big ${cue.id}`} role="status" data-testid="demo-cue-big"><div><b>{cue.title}</b><span>{cue.body}</span></div></div>
      ) : (
        <div className={`f3-cue ${pitch ? 'pitch' : ''} ${pitch && left <= 60 ? 'warn' : ''}`} role="status" data-testid="demo-cue" key={cue.id}>
          <i>{mss(t)}{demo.checkpoint >= 0 && ` ・ ${CIRCLED[demo.checkpoint]}`}</i>
          <b>{cue.title}</b>
          <span>{cue.body}</span>
          {(links.length > 0 || cue.id === 'phone') && (
            <div className="f3-cue-links">
              {cue.id === 'phone' && <button onClick={onPhone} data-testid="demo-raise-phone">拿起手機（P{pad ? '／X' : ''}）</button>}
              {links.map((l) => (
                <button key={l} data-testid={`demo-open-${l}`} onClick={() => window.open(href(l, demo.incidentEventId), LINK[l].win)}>開啟・{LINK[l].label}</button>
              ))}
            </div>
          )}
          {pitch && deck.slides.length > 0 && (
            <em>投影片 {demo.slide + 1} / {deck.slides.length}{deck.titles[demo.slide] ? `・${deck.titles[demo.slide]}` : ''}{secs ? `・約 ${secs} 秒` : ''}・{pad ? 'LB／RB' : '←／→'} 換頁</em>
          )}
          {waiting === 'continue' && demo.ready && (
            <div className="f3-cue-go" data-testid="demo-continue-prompt"><kbd>{contKey}</kbd> 按 {contKey} 繼續{!pad && <small>（或 N）</small>}{!pitch && <small>・工廠時間暫停中</small>}</div>
          )}
          {waiting === 'approval' && <div className="f3-cue-wait" data-testid="demo-approval-wait">⏳ 等主管核准…核准後自動往下</div>}
          {waiting === 'event' && next?.enter?.on && <div className="f3-cue-wait" data-testid="demo-event-wait">⏳ 等工廠下一件事：{next.title}</div>}
        </div>
      )}
      {demo.overTarget && !pitch && (
        <div className="f3-pitch-nudge" data-testid="demo-pitch-nudge">🎤 該換 Pitch 了・{demo.cue.id === 'robot' ? (demo.autoPitchIn > 0 ? `${Math.ceil(demo.autoPitchIn)} 秒後自動換` : '換了') : '講快一點，機器人之後就換'}</div>
      )}
      {pitch && <div className={`f3-pitch-clock ${left <= 60 ? 'warn' : ''}`} data-testid="demo-pitch-clock">{left > 0 ? <>Pitch 剩 <b>{mss(left)}</b></> : <b>時間到</b>}</div>}
      <div className="f3-demo-bar" data-testid="demo-bar">
        <span className="f3-demo-time"><b>{mss(t)}</b>{!pitch && <small> ・故事目標 {mss(demo.storyTarget)}</small>}</span>
        <span className="f3-demo-next">{next ? <>下一步 {next.title}</> : '最後一步'}</span>
        {demo.note && <span className="f3-demo-warn">{demo.note}</span>}
        <button onClick={() => void demo.cont()} title={pad ? '繼續（A）' : '繼續（Space／N）'} data-testid="demo-next" disabled={!next && !demo.ready}>▶ 繼續</button>
        <button onClick={demo.togglePause} title={pad ? '暫停／繼續（Menu）' : '暫停／繼續（B）'} data-testid="demo-pause">{paused ? '▶ 播放' : '❚❚ 暫停'}</button>
        <button onClick={() => demo.setConfirmExit(true)} title={pad ? '離開（按住 B）' : '離開（Esc）'} data-testid="demo-stop">✕</button>
      </div>
      {paused && <div className="f3-demo-paused" data-testid="demo-paused">❚❚ 暫停中・{pad ? 'Menu' : 'B'} 繼續</div>}
      {demo.confirmExit && (
        <div className="f3-confirm" role="dialog" data-testid="demo-confirm-exit">
          <div>
            <b>離開 Demo？</b>
            <span>視角會交還給你，工廠繼續跑。</span>
            <div>
              <button onClick={() => void demo.exit()} data-testid="demo-confirm-yes">離開（{pad ? 'A' : 'Enter'}）</button>
              <button onClick={() => demo.setConfirmExit(false)}>繼續 Demo（{pad ? 'B' : 'Esc'}）</button>
            </div>
          </div>
        </div>
      )}
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

/** The Xbox mapping, while View is held (gamepad.ts PAD_HELP). */
export function PadHelp({ demo }: { demo: boolean }) {
  return (
    <div className="f3-pad-help" data-testid="f3-pad-help">
      <b>🎮 Xbox 搖桿{demo ? '・Demo 模式' : ''}</b>
      <table><tbody>
        {PAD_HELP.map(([btn, free, inDemo]) => <tr key={btn}><th>{btn}</th><td className={demo ? 'dim' : ''}>{free}</td><td className={demo ? '' : 'dim'}>{inDemo}</td></tr>)}
      </tbody></table>
      <small>左欄：自由走動・右欄：Demo 中（視角鎖定）</small>
    </div>
  )
}
