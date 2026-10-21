// Shift handover page (issue #49), served at /handover.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { initTheme } from '../theme'
import '../index.css'
import './handover.css'
import { WarRoomProvider } from '../useWarRoom'
import { Handover } from './Handover'

initTheme()
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <WarRoomProvider><Handover /></WarRoomProvider>
  </StrictMode>,
)
