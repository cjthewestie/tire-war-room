// 斷網備援的 Demo 小按鈕（CJ 2026-10-22：「把我這些想要 demo 的東西都做成小按鈕，因為可能會被斷網」）。
// 決賽現場網路可能斷：不靠別人從遠端 / SSH 觸發，每個 demo 動作都在頁面角落的一顆小「⚙」裡，預設收合。
// 全部只呼叫本機 /api（相對路徑，瀏覽器 → 這台主機的伺服器，只要 LAN 在就能用），跟其他寫入一樣走主管規則。
// 不是以前的「展示控制」大框（已移除）；/phone、/worker 不掛這個元件，3D 內嵌的 iframe 也不顯示。
import { useEffect, useRef, useState } from 'react'
import { useWarRoom } from '../useWarRoom'
import { AUTO_FAULT, AUTO_VAL } from '../demo/auto'
import './demoButtons.css'

type Act = { id: string; label: string; title: string; done: string; run?: () => Promise<unknown>; href?: string; newTab?: boolean }

export function DemoButtons({ where = 'warroom' }: { where?: 'warroom' | 'factory' }) {
  const { transport } = useWarRoom()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [toast, setToast] = useState<{ text: string; bad: boolean } | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  if (!transport) return null
  const t = transport
  const say = (text: string, bad = false) => {
    setToast({ text, bad }); clearTimeout(timer.current)
    timer.current = setTimeout(() => setToast(null), bad ? 5000 : 2500)
  }
  const need = <F,>(f: F | undefined, what: string): F => { if (!f) throw new Error(`${what}只在連到主機時可用`); return f }
  const acts: Act[] = [
    { id: 'rush', label: '＋急單卡', title: '觸發一張急單（POST /api/sim/events/fire rush_order）', done: '已送出急單卡',
      run: () => need(t.fireEvent, '急單').call(t, 'rush_order') },
    { id: 'sick', label: '＋病假卡', title: '觸發一張病假（POST /api/sim/events/fire staff_sick）', done: '已送出病假卡',
      run: () => need(t.fireEvent, '病假').call(t, 'staff_sick') },
    { id: 'cure07', label: '7 號機溫降', title: '7 號硫化機蒸汽壓不足，128 → 122°C（POST /api/sim/fault）', done: '已注入 7 號機溫降',
      run: () => need(t.injectFault, '7 號機溫降').call(t, AUTO_FAULT) },
    { id: 'normal', label: '回到正常', title: '重新載入「正常生產」：清掉故障與未結事件、速度 1×、網路回正常（任務紀錄保留）', done: '已回到正常',
      run: async () => {
        await t.setNetwork('online').catch(() => {})
        await t.control({ action: 'scenario', scenario: 'normal', speed: 1, preroll: true })
        await t.control({ action: 'play' })
      } },
    { id: '3d', label: '▶ 3D Demo', title: '開啟 3D 自動播放 Demo（/factory?demo=cure07）', done: '開啟 3D Demo', href: `/factory?demo=${AUTO_VAL}`, newTab: where === 'warroom' },
    { id: 'perf', label: '後台・效能', title: '後台的效能與追蹤', done: '後台・效能', href: '/#admin/perf' },
    { id: 'qa', label: 'QA', title: 'QA 模式', done: 'QA 模式', href: '/#qa' },
  ]
  const fire = async (a: Act) => {
    if (busy) return
    setBusy(a.id)
    try { await a.run!(); say(`✓ ${a.done}`) }
    catch (e) { say(`✕ ${a.label} 沒有成功：${String(e).replace(/^Error: /, '')}`.slice(0, 160), true) }
    finally { setBusy(null) }
  }
  const go = (a: Act) => {
    say(a.done)
    if (a.newTab) { window.open(a.href, '_blank', 'noopener'); return }
    if (where === 'warroom' && a.href!.startsWith('/#')) { window.location.hash = a.href!.slice(1); return }
    window.location.href = a.href!
  }
  return (
    <div className={`dbtn dbtn-${where} ${open ? 'open' : ''}`} data-testid="demo-buttons">
      {open && (
        <div className="dbtn-row" role="group" aria-label="Demo 小按鈕">
          {acts.map((a) => (
            <button key={a.id} type="button" title={a.title} disabled={a.run ? busy !== null : false}
              className={busy === a.id ? 'busy' : ''} data-testid={`dbtn-${a.id}`}
              onClick={() => (a.run ? void fire(a) : go(a))}>{busy === a.id ? `${a.label}…` : a.label}</button>
          ))}
        </div>
      )}
      <button type="button" className="dbtn-pill" aria-expanded={open} aria-label={open ? '收起 Demo 小按鈕' : 'Demo 小按鈕'}
        title="Demo 小按鈕（只用本機網路）" onClick={() => setOpen((o) => !o)} data-testid="dbtn-toggle">{open ? '✕' : '⚙'}</button>
      {toast && <span className={`dbtn-toast ${toast.bad ? 'bad' : ''}`} role="status">{toast.text}</span>}
    </div>
  )
}
