// 提詞機：Demo 時在角落顯示「這一段要講的話」＋下一句（淡色、一行）＋細進度條。CJ 10-22：「逐字稿對話框太大了」→
// 小框（桌機約 400px 寬、目前這句 16–17px、最多 3 行）。一句太長時分頁：一次一兩個句子，依字數自動翻到下一頁（停在最後一頁）。
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
/** A page of the current line: at most this many characters (≈ 3 lines in the compact box). */
const PAGE_CHARS = 60

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

/** Sentences (ending 。！？：), grouped into pages of at most PAGE_CHARS (a longer single sentence is its own page). */
export function pagesOf(text: string): string[] {
  const parts = text.match(/[^。！？]+[。！？]?/g) ?? [text]
  const out: string[] = []
  for (const p of parts) {
    const last = out[out.length - 1]
    if (last != null && last.length + p.length <= PAGE_CHARS) out[out.length - 1] = last + p
    else out.push(p)
  }
  return out
}

export function Prompter({ beat, position = 'bottom-left' }: PrompterProps) {
  const [visible, setVisible] = useState(initialVisible)
  const [page, setPage] = useState({ beat: null as DemoBeat | null, i: 0 })

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
  const pages = line ? pagesOf(line.current) : []
  const i = page.beat === beat ? Math.min(page.i, pages.length - 1) : 0
  // turn the page by itself: about the time it takes to say it (≥ 3.5 s), never past the last page
  // (primitive deps: the parent re-renders often, the timer must not restart on every render)
  const n = pages.length, len = pages[i]?.length ?? 0
  useEffect(() => {
    if (!beat || i >= n - 1) return
    const id = setTimeout(() => setPage({ beat, i: i + 1 }), Math.max(3500, len * 230))
    return () => clearTimeout(id)
  }, [beat, i, n, len])

  if (!line || !visible) return null
  return (
    <aside className={`tp-box tp-${position}`} aria-live="polite" aria-label="提詞機">
      <div className="tp-head">
        <div className="tp-bar" role="progressbar" aria-valuemin={0} aria-valuemax={line.total} aria-valuenow={line.index + 1}>
          <i style={{ width: `${line.progress * 100}%` }} />
        </div>
        <span className="tp-meta">{line.index + 1}/{line.total}{pages.length > 1 ? ` · ${i + 1}/${pages.length}` : ''} · T</span>
      </div>
      <p key={`${beat}-${i}`} className="tp-cur">{pages[i]}</p>
      {line.next && <p className="tp-next"><span className="tp-lab">下一句</span>{line.next}</p>}
    </aside>
  )
}

export default Prompter
