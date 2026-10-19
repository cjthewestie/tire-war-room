// 3D factory walk-through, served at /factory.
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../index.css'
import './factory3d.css'
import { WarRoomProvider } from '../useWarRoom'
import { FactoryApp } from './FactoryApp'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <WarRoomProvider><FactoryApp /></WarRoomProvider>
  </StrictMode>,
)
