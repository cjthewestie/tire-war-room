// Auto-play demo overlay (?mock=1&demo=cure07): one caption line on top, a pause / replay bar at the bottom.
// Nothing here can block the run: no "press Space", no next-step hints, no audit prompts.
import { AUTO_TEXT } from '../demo/cues'
import type { GuidedDemo } from './useGuidedDemo'

export function AutoOverlay({ demo }: { demo: GuidedDemo }) {
  const title = AUTO_TEXT[demo.cue.id]?.title ?? demo.cue.title
  return (
    <>
      <div className="f3-auto-cap" data-testid="auto-cap">{title}</div>
      <div className="f3-auto-bar" data-testid="auto-bar">
        <b>{demo.autoStatus || '準備中'}</b>
        <span>
          <button type="button" onClick={demo.togglePause}>{demo.paused ? '▶ 播放' : '❚❚ 暫停'}</button>
          <button type="button" onClick={async () => { await demo.exit(); void demo.begin() }}>↻ 重新播放</button>
        </span>
      </div>
    </>
  )
}
