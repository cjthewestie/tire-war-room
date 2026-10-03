// Placeholder supervisor phone view. The web builder replaces this with the page-11 approval card.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../index.css'
import { useLiveStream } from '../api'

function Phone() {
  const { pending } = useLiveStream()
  return (
    <main className="shell">
      <h1>主管手機</h1>
      <p>待核准 {pending.length} 件</p>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Phone />
  </StrictMode>,
)
