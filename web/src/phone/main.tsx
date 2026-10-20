// Supervisor phone view (page 11 approval card), served at /phone.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { initTheme } from '../theme'
import '../index.css'
import { WarRoomProvider } from '../useWarRoom'
import { Phone } from './Phone'

initTheme()
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <WarRoomProvider><Phone /></WarRoomProvider>
  </StrictMode>,
)
