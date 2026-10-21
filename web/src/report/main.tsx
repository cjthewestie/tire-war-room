// Incident report page (issue #59), served at /report.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { initTheme } from '../theme'
import '../index.css'
import './report.css'
import { WarRoomProvider } from '../useWarRoom'
import { Report } from './Report'

initTheme()
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <WarRoomProvider><Report /></WarRoomProvider>
  </StrictMode>,
)
