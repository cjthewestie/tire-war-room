// Floor worker's task page (issue #46), served at /worker.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { initTheme } from '../theme'
import '../index.css'
import './worker.css'
import { WarRoomProvider } from '../useWarRoom'
import { Worker } from './Worker'

initTheme()
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <WarRoomProvider><Worker /></WarRoomProvider>
  </StrictMode>,
)
