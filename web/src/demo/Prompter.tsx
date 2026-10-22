// 提詞機：Demo 時在角落顯示「這一段要講的話」（大字）＋下一句（淡色）＋細進度條。
// T 鍵切換顯示（在輸入框打字時不算），?prompter=0 預設隱藏；開關記在 localStorage（讀寫失敗也照常運作）。
import { useEffect, useState } from 'react'
import { lineFor, type DemoBeat } from './script'
import './prompter.css'

export type PrompterPosition = 'bottom-left' | 'bottom-right' | 'top-left' | 'top-right'

export interface PrompterProps {
  /** Current demo beat; null renders nothing. */
  beat: DemoBeat | null
  /** Corner of the screen; default bottom-left. */
  position?: PrompterPosition
}

const KEY = 'tp.prompter.visible'

function initialVisible(): boolean {
  try {
    if (new URLSearchParams(window.location.search).get('prompter') === '0') return false
  } catch { /* no location */ }
  try {
    if (window.localStorage.getItem(KEY) === '0') return false
  } catch { /* storage blocked */ }
  return true
}

function isTyping(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  if (!el || !el.tagName) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable
}

export function Prompter({ beat, position = 'bottom-left' }: PrompterProps) {
  const [visible, setVisible] = useState(initialVisible)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code !== 'KeyT' || e.repeat || e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return
      setVisible((v) => {
        const nv = !v
        try { window.localStorage.setItem(KEY, nv ? '1' : '0') } catch { /* storage blocked */ }
        return nv
      })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const line = lineFor(beat)
  if (!line || !visible) return null
  return (
    <aside className={`tp-box tp-${position}`} aria-live="polite" aria-label="提詞機">
      <div className="tp-bar" role="progressbar" aria-valuemin={0} aria-valuemax={line.total} aria-valuenow={line.index + 1}>
        <i style={{ width: `${line.progress * 100}%` }} />
      </div>
      <p key={beat} className="tp-cur">{line.current}</p>
      {line.next && <p className="tp-next"><span className="tp-lab">下一句</span>{line.next}</p>}
      <div className="tp-meta">{line.index + 1} / {line.total} · T 隱藏</div>
    </aside>
  )
}

export default Prompter
