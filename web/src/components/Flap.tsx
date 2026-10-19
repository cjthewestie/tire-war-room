// The signature move (DESIGN.md): a split-flap cell update. When `text` changes after the first render, each
// character cell turns over in turn (50 ms apart, starting `delay` ms in), like a station departure board.
// Reduced motion: the CSS swaps instantly and marks the cell with a 1 s orange underline instead.
import { FLAP_STEP_MS } from './flip'

export function Flap({ text, gen, delay = 0, className = '' }: { text: string; gen: number; delay?: number; className?: string }) {
  if (gen === 0) return <span className={`flap ${className}`}>{text}</span>
  const chars = Array.from(text)
  return (
    <span key={gen} className={`flap is-flipping ${className}`} aria-label={text}>
      {chars.map((c, i) => (
        <span key={i} className="flap-c" aria-hidden style={{ animationDelay: `${delay + Math.min(i, 16) * FLAP_STEP_MS}ms` }}>{c === ' ' ? ' ' : c}</span>
      ))}
    </span>
  )
}
