// The war-room context object, kept apart from its provider so a unit test can feed a state (e2e/approval-alert-unit.mjs).
import { createContext } from 'react'
import { initialState, type WarRoomState } from './store'
import type { Transport } from './transport'

export interface Ctx { state: WarRoomState; transport: Transport | null; connected: boolean }
export const WarRoomCtx = createContext<Ctx>({ state: initialState, transport: null, connected: false })
