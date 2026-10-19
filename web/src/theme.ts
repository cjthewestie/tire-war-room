// Light / dark theme (CJ, 2026-10-20: 「顏色風格改回原本多加亮色暗色切換鈕」). A choice made
// with the header toggle is kept in localStorage (wrapped in try/catch: private windows and blocked storage still work).
// Default is LIGHT (CJ, 2026-10-20: 「暗色不太好看」); the system preference is not used for the default.
import { useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'
const KEY = 'warroom-theme'

function stored(): Theme | null {
  try { const v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null } catch { return null }
}
function apply(t: Theme) {
  document.documentElement.dataset.theme = t
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', t === 'light' ? '#ffffff' : '#0b0d10')
}

/** Call once before the first render. */
export function initTheme(): void { apply(stored() ?? 'light') }

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => stored() ?? ((document.documentElement.dataset.theme as Theme) || 'light'))
  useEffect(() => { apply(theme) }, [theme])
  const toggle = () => setTheme((t) => {
    const n: Theme = t === 'light' ? 'dark' : 'light'
    try { localStorage.setItem(KEY, n) } catch { /* storage blocked: the choice lasts this page only */ }
    return n
  })
  return [theme, toggle]
}
