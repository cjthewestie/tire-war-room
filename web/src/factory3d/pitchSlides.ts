// The pitch deck on the 3D whiteboard. CJ's slides live in web/public/pitch/ (01.png, 02.png, …), listed in
// web/public/pitch/slides.json: { "slides": [{ "file": "01.png", "title": "封面", "seconds": 15 }, …] } — plain file
// names ("01.png") work too (see web/public/pitch/README.md). `seconds` paces the pitch: the card shows each slide's
// share of the 3 minutes. If the manifest is missing or empty, three placeholder slides are drawn here.
import { useEffect, useState } from 'react'

export interface Deck { slides: string[]; titles: (string | null)[]; seconds: (number | null)[]; placeholder: boolean
  /** the presenter's line for each slide (slides.json `say`), shown on the prompter while the slides are open */
  says?: (string | null)[] }
type Entry = string | { file?: unknown; title?: unknown; seconds?: unknown; say?: unknown }

const W = 1600, H = 900
const FONT = '"Noto Sans TC", "PingFang TC", "Heiti TC", sans-serif'

function draw(lines: { text: string; size: number; color?: string; weight?: number }[], footer: string, accent: string): string {
  if (typeof document === 'undefined') return ''
  const c = document.createElement('canvas')
  c.width = W; c.height = H
  const g = c.getContext('2d')
  if (!g) return ''
  g.fillStyle = '#fbf8f1'; g.fillRect(0, 0, W, H)
  g.fillStyle = accent; g.fillRect(0, 0, W, 18)
  g.strokeStyle = '#d9cfbd'; g.lineWidth = 6; g.setLineDash([26, 18]); g.strokeRect(70, 90, W - 140, H - 220); g.setLineDash([])
  g.textAlign = 'center'; g.textBaseline = 'middle'
  const total = lines.reduce((a, l) => a + l.size * 1.45, 0)
  let y = 90 + (H - 220) / 2 - total / 2
  for (const l of lines) {
    y += l.size * 0.725
    g.font = `${l.weight ?? 700} ${l.size}px ${FONT}`
    g.fillStyle = l.color ?? '#2f3338'
    g.fillText(l.text, W / 2, y)
    y += l.size * 0.725
  }
  g.font = `500 34px ${FONT}`; g.fillStyle = '#8a8f96'; g.fillText(footer, W / 2, H - 70)
  return c.toDataURL('image/png')
}

/** The three placeholder slides (drawn once). */
let placeholders: string[] | null = null
export function placeholderSlides(): string[] {
  placeholders ??= [
    draw([{ text: 'Pitch 投影片放這裡', size: 110 }, { text: '← / → 換頁・點白板全螢幕', size: 46, color: '#6b7078', weight: 500 }], '1 / 3', '#f2c14e'),
    draw([{ text: '實心胎智慧製造戰情室', size: 104 }, { text: 'Team Westie・2026 Agentic AI on the Edge Hackathon', size: 44, color: '#6b7078', weight: 500 }], '2 / 3', '#e07a74'),
    draw([{ text: '待 CJ 嵌入', size: 120 }, { text: '把投影片放進 web/public/pitch/，寫進 slides.json', size: 44, color: '#6b7078', weight: 500 }], '3 / 3', '#6fa8dc'),
  ]
  return placeholders
}

/** The deck: the manifest's slides (as URLs under /pitch/), or the placeholders. */
/** Parse a manifest: { slides: [...] } or a bare array; each entry a file name or { file, title, seconds }. */
export function parseDeck(m: unknown): Deck | null {
  const list: Entry[] = Array.isArray(m) ? m : Array.isArray((m as { slides?: unknown } | null)?.slides) ? (m as { slides: Entry[] }).slides : []
  const rows = list.map((e) => (typeof e === 'string' ? { file: e, title: null, seconds: null, say: null } : {
    say: typeof e?.say === 'string' && e.say.trim() ? e.say.trim() : null,
    file: typeof e?.file === 'string' ? e.file : '',
    title: typeof e?.title === 'string' ? e.title : null,
    seconds: typeof e?.seconds === 'number' && e.seconds > 0 ? e.seconds : null,
  })).filter((r) => r.file.length > 0)
  if (!rows.length) return null
  return {
    slides: rows.map((r) => (/^(https?:|data:|\/)/.test(r.file) ? r.file : `/pitch/${r.file}`)),
    titles: rows.map((r) => r.title), seconds: rows.map((r) => r.seconds), says: rows.map((r) => r.say), placeholder: false,
  }
}

/** The deck: the manifest's slides (as URLs under /pitch/), or the placeholders. */
export function useDeck(): Deck {
  const [deck, setDeck] = useState<Deck>(() => {
    const slides = placeholderSlides()
    return { slides, titles: slides.map(() => null), seconds: slides.map(() => null), placeholder: true }
  })
  useEffect(() => {
    let live = true
    fetch('/pitch/slides.json', { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : null))
      .then((m: unknown) => { const d = parseDeck(m); if (live && d) setDeck(d) })
      .catch(() => { /* no manifest: placeholders */ })
    return () => { live = false }
  }, [])
  return deck
}
