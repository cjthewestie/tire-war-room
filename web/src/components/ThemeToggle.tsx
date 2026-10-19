// The light / dark switch: one icon button at the right end of the home status line (CJ, 2026-10-20: header 「這邊不需要」).
import { useTheme } from '../theme'

export function ThemeToggle({ className = '' }: { className?: string }) {
  const [theme, toggle] = useTheme()
  return (
    <button type="button" className={`theme-tog ${className}`} onClick={toggle} aria-label="切換亮色／暗色" title={theme === 'light' ? '切換為暗色' : '切換為亮色'} data-testid="theme-toggle" data-theme-now={theme}>
      {theme === 'light'
        ? <svg viewBox="0 0 24 24" aria-hidden><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" /></svg>
        : <svg viewBox="0 0 24 24" aria-hidden><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>}
    </button>
  )
}
