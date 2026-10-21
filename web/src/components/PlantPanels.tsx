// The whole plant around the 28 machines (issue #11; was the 工廠全貌 tab, merged into 產線監控 by issue #37):
// goods, people by zone, worker tasks, raw materials against their reorder points, the loading dock, quality,
// energy and maintenance. Orders live in 產線監控's 訂單 panel and the 訂單 tab. Read only; data comes from
// GET /api/world and the WS "world" deltas (warroom/sim/world.py).
import type { WorldSnapshot, WorldZone, WorkerState } from '../contracts'
import { LINE_LABELS } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD, fmtNum } from '../format'
import { useWarRoom } from '../useWarRoom'
import { PHONE_QUERY, useMediaQuery } from '../media'
import { Panel } from './common'
import { GoodsCounts, GoodsQueue } from './GoodsQueue'
import { movedText } from './goods'
import { ProgressRing } from './ProgressRing'
import { TaskList } from './TaskList'

const ZONE_LABEL: Record<WorldZone, string> = {
  war_room: '戰情室', staff: '員工待命區', line_A: 'A 胎體／胎面膠', line_B: 'B 胎側膠', line_C: 'C 鋼絲圈', line_D: 'D 尼龍布',
  line_BOND: '貼合', line_CURE: '硫化區', break: '休息區', toilet: '廁所', dock: '碼頭', off_site: '廠外',
}
// HY, 2026-10-21: a supervisor does not monitor toilet breaks — 廁所／如廁 are counted as 休息區／休息 on this panel
const ZONE_ORDER: WorldZone[] = ['war_room', 'line_A', 'line_B', 'line_C', 'line_D', 'line_BOND', 'line_CURE', 'dock', 'staff', 'break']
const STATE_ORDER: WorkerState[] = ['在機台', '搬運', '待命', '休息', '請假', '下班']
const MAT_STATUS = { ok: '充足', reorder: '已達再訂購點', low: '偏低', out: '缺料' } as const

export function PlantPanels() {
  const { state, transport } = useWarRoom()
  const phone = useMediaQuery(PHONE_QUERY)
  const w = state.world
  if (!w) {
    return <p className="muted plant-wait">{transport?.mode === 'mock' ? '人員、原料、碼頭與貨品需要連線後端（瀏覽器模擬資料不含這部分）。' : '等待全廠資料…'}</p>
  }
  // UX declutter: 人員分布 and 原料庫存 stay open (a supervisor's first look); the other seven fold into
  // 「更多全廠資訊」, closed by default. On a phone the whole section folds into its heading.
  const body = (
    <>
      <div className="world world-main">
        <PeoplePanel w={w} />
        <MaterialsPanel w={w} />
      </div>
      <details className="world-more" data-testid="plant-more">
        <summary>更多全廠資訊（7 項）：貨品、任務、碼頭、品質、用電、保養、動態</summary>
        <div className="world">
          <GoodsPanel w={w} />
          <TasksPanel w={w} />
          <DockPanel w={w} />
          <QualityPanel w={w} />
          <EnergyPanel w={w} />
          <MaintenancePanel w={w} />
          <FeedPanel w={w} />
        </div>
      </details>
    </>
  )
  return (
    <section className="world-wrap" data-testid="plant-panels" data-rev={w.rev} aria-label="全廠：人、料、貨、車">
      {phone
        ? <details className="world-fold"><summary className="section-h">全廠：人、料、貨、車</summary>{body}</details>
        : <><h2 className="section-h">全廠：人、料、貨、車</h2>{body}</>}
      <p className="foot-note" data-testid="world-sources">資料來源：人員、班表、換模、異常紀錄、每日產出來自<b>團隊資料集</b>；訂單、採購、出貨、用電、檢驗、保養為<b>模擬資料</b>。</p>
    </section>
  )
}

// issue #17: per-order ring (done / qty) and where its tyres are; moved_h = how far the last schedule change moved it
export function OrderRing({ w, id }: { w: WorldSnapshot; id: string }) {
  const g = w.goods?.orders.find((x) => x.order_id === id)
  if (!g) return null
  return <span className="order-ring" data-testid="order-ring" data-order={id}><ProgressRing value={g.progress} size={20} tone={g.late ? 'alarm' : 'live'} title={`${g.done}/${g.qty}`} /> </span>
}

export function OrderGoodsLine({ w, id }: { w: WorldSnapshot; id: string }) {
  const g = w.goods?.orders.find((x) => x.order_id === id)
  if (!g) return null
  const mv = movedText(g.moved_h)
  return (
    <p className="muted small w-note">硫化中 {g.in_press}・24 h 內排入 {g.queued}{g.next_mold_in ? `・下一模 ${fmtMD(g.next_mold_in)} ${fmtHM(g.next_mold_in)}` : ''}
      {mv && <i className={`tag moved ${g.moved_h! > 0 ? 'later' : 'earlier'}`}>{mv}</i>}{g.late && <i className="tag rush">會誤交期</i>}</p>
  )
}

function TasksPanel({ w }: { w: WorldSnapshot }) {
  const t = w.tasks
  if (!t) return null
  return (
    <Panel title="人員任務" className="w-tasks" right={<span className="muted small">待派 {t.counts['待派'] ?? 0}・前往 {t.counts['前往中'] ?? 0}・執行 {t.counts['執行中'] ?? 0}・完成 {t.counts['完成'] ?? 0}</span>}>
      <div data-testid="world-tasks"><TaskList tasks={t.items} limit={10} /></div>
      <p className="muted small w-note">核准後由程式把每個步驟轉成任務（誰、去哪、做什麼），人員走過去執行</p>
    </Panel>
  )
}

function GoodsPanel({ w }: { w: WorldSnapshot }) {
  const g = w.goods
  if (!g) return null
  return (
    <Panel title="貨品狀態" className="w-goods">
      <div data-testid="world-goods">
        <GoodsCounts goods={g} />
        <GoodsQueue goods={g} />
      </div>
    </Panel>
  )
}

function MaterialsPanel({ w }: { w: WorldSnapshot }) {
  return (
    <Panel title="原料庫存" className="w-mat">
      <ul className="mat-list" data-testid="materials">
        {w.materials.map((m) => {
          const pct = (v: number) => `${Math.max(0, Math.min(100, (100 * v) / m.capacity))}%`
          return (
            <li key={m.key} className={`mat ${m.status}`}>
              <div className="w-row">
                <span>{m.name}{m.on_order > 0 && <i className="tag">在途 {fmtNum(m.on_order, 0)}</i>}</span>
                <span className="small"><b>{fmtNum(m.stock, 0)}</b> {m.unit}<span className="muted">・約 {m.days_left == null ? '—' : fmtNum(m.days_left, 1)} 天</span></span>
              </div>
              <div className="mat-bar" title={`再訂購點 ${m.reorder_point} ${m.unit}・${MAT_STATUS[m.status]}`}>
                <i style={{ width: pct(m.stock) }} /><em style={{ left: pct(m.reorder_point) }} aria-label="再訂購點" />
              </div>
            </li>
          )
        })}
      </ul>
      <p className="muted small w-note">細線＝再訂購點；低於就自動下採購單，貨車到碼頭收貨</p>
    </Panel>
  )
}

function DockPanel({ w }: { w: WorldSnapshot }) {
  const d = w.dock
  const pos = w.purchase_orders.filter((p) => p.status === '在途' || p.status === '延遲')
  return (
    <Panel title="碼頭進出" className="w-dock" right={<span className="muted small">下一班出車 {d.next_departure ? `${fmtMD(d.next_departure)} ${fmtHM(d.next_departure)}` : '—'}</span>}>
      <div className="trucks" data-testid="dock-trucks">
        {d.trucks.length === 0 && <p className="muted small">目前碼頭沒有車</p>}
        {d.trucks.map((t) => (
          <div key={t.id} className={`truck ${t.kind}`}>
            <b>{t.kind === 'inbound' ? '進貨卸貨中' : '出貨裝車中'}</b><span>{t.plate}・{t.cargo}</span>
            <span className="muted small">{fmtHM(t.arrived_at)} 到・{fmtHM(t.leaves_at)} 離</span>
          </div>
        ))}
      </div>
      {pos.length > 0 && (
        <ul className="w-mini">
          {pos.slice(0, 3).map((p) => (
            <li key={p.id}><span className={p.status === '延遲' ? 'warn' : 'muted'}>{p.status}</span> {p.id} {p.name} {fmtNum(p.qty, 0)} {p.unit}・預計 {fmtMD(p.eta)} {fmtHM(p.eta)}</li>
          ))}
        </ul>
      )}
      <ul className="w-log">
        {d.log.slice(0, 4).map((e, i) => (
          <li key={i}><span className="muted">{fmtHM(e.ts)}</span> <span className={`dir ${e.direction}`}>{e.direction === 'inbound' ? '進' : '出'}</span> {e.text}</li>
        ))}
      </ul>
    </Panel>
  )
}

function PeoplePanel({ w }: { w: WorldSnapshot }) {
  const p = w.people
  const zone: Partial<Record<WorldZone, number>> = { ...p.by_zone, break: (p.by_zone.break ?? 0) + (p.by_zone.toilet ?? 0) }
  const st: Partial<Record<WorkerState, number>> = { ...p.by_state, 休息: (p.by_state.休息 ?? 0) + (p.by_state.如廁 ?? 0) }
  const max = Math.max(1, ...Object.values(zone).map((n) => n ?? 0))
  return (
    <Panel title="人員分布" className="w-people" right={<span className="muted small">在廠 {p.on_duty}・全員 {p.total}</span>}>
      <ul className="zones" data-testid="people-zones">
        {ZONE_ORDER.filter((z) => zone[z]).map((z) => (
          <li key={z}><span>{ZONE_LABEL[z]}</span><span className="zbar"><i style={{ width: `${(100 * (zone[z] ?? 0)) / max}%` }} /></span><b>{zone[z]}</b></li>
        ))}
      </ul>
      <div className="states">
        {STATE_ORDER.filter((s) => st[s]).map((s) => <span key={s} className={`st st-${s}`}>{s} {st[s]}</span>)}
      </div>
      <ul className="w-mini">
        {p.workers.filter((x) => x.role === '主管' || x.state === '請假' || x.role === '維修' || x.note).filter((x) => x.state !== '下班')
          .sort((a, b) => Number(b.state === '請假') - Number(a.state === '請假')).slice(0, 4).map((x) => (
          <li key={x.id} className={x.state === '請假' ? 'warn' : ''}><b>{x.name}</b> <span className="muted">{x.team_label}・{x.shift}</span>・{x.state === '如廁' ? '休息' : x.state}
            {x.state !== '請假' ? `（${ZONE_LABEL[x.zone === 'toilet' ? 'break' : x.zone]}${x.machine_id ? ` ${machineLabel(x.machine_id)}` : ''}）` : ''}{x.note ? `・${x.note}` : ''}
            {x.source === 'team_dataset' && <i className="tag">資料集</i>}</li>
        ))}
      </ul>
    </Panel>
  )
}

function Spark({ pts }: { pts: [string, number][] }) {
  if (pts.length < 2) return null
  const vs = pts.map((p) => p[1])
  const lo = Math.min(...vs) * 0.95, hi = Math.max(...vs) * 1.02
  const d = vs.map((v, i) => `${(i / (vs.length - 1)) * 100},${30 - ((v - lo) / Math.max(1, hi - lo)) * 28}`).join(' ')
  return <svg className="spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-label="近 6 小時用電"><polyline points={d} /></svg>
}

function EnergyPanel({ w }: { w: WorldSnapshot }) {
  const e = w.energy
  const lines = Object.entries(e.by_line).sort((a, b) => b[1] - a[1])
  const top = Math.max(1, ...lines.map(([, v]) => v))
  return (
    <Panel title="用電" className="w-energy" right={e.peak ? <span className="tag warn">尖峰時段</span> : <span className="muted small">離峰</span>}>
      <div className="w-row"><span><b className="big">{fmtNum(e.total_kw, 0)}</b> kW</span><span className="muted small">契約 {fmtNum(e.contract_kw, 0)} kW（{fmtNum((100 * e.total_kw) / e.contract_kw, 0)}%）</span></div>
      <div className="bar"><i style={{ width: `${Math.min(100, (100 * e.total_kw) / e.contract_kw)}%` }} /></div>
      <Spark pts={e.history} />
      <ul className="zones">
        {lines.map(([l, v]) => (
          <li key={l}><span>{LINE_LABELS[l as keyof typeof LINE_LABELS] ?? l}</span><span className="zbar"><i style={{ width: `${(100 * v) / top}%` }} /></span><b>{fmtNum(v, 0)}</b></li>
        ))}
      </ul>
      <p className="muted small w-note">公用設備 {fmtNum(e.base_kw, 0)} kW 另計；硫化機用蒸汽加熱，只計液壓與控制</p>
    </Panel>
  )
}

function QualityPanel({ w }: { w: WorldSnapshot }) {
  const q = w.quality
  const recent = q.daily.slice(-5)
  const avg = recent.length ? recent.reduce((a, d) => a + d.tires_done, 0) / recent.length : null
  return (
    <Panel title="品質檢驗" className="w-quality" right={<span className="muted small">今日 {q.inspected_today} 批・不良 {q.defects_today}</span>}>
      <ul className="w-log" data-testid="inspections">
        {q.recent.slice(0, 6).map((i) => (
          <li key={i.id} className={i.result === '不良' ? 'bad' : ''}>
            <span className="muted">{fmtHM(i.ts)}</span> <span className={`res ${i.result === '不良' ? 'bad' : 'ok'}`}>{i.result}</span>{' '}
            {i.stage === 'final' ? '成品' : '製程'}・{machineLabel(i.machine_id)}{i.cause ? `：${i.cause}` : ''}{i.disposition ? `（${i.disposition}）` : ''}
          </li>
        ))}
      </ul>
      {q.incidents.length > 0 && <>
        <p className="sub">歷史異常 <i className="tag">資料集</i></p>
        <ul className="w-log" data-testid="incidents">
          {q.incidents.slice(0, 3).map((i) => (
            <li key={i.id}><span className="muted">{fmtMD(i.date)}</span> {i.machine_id ? machineLabel(i.machine_id) : i.equipment}・{i.symptom} → {i.action}（停 {i.downtime_min} 分・{i.outcome}）</li>
          ))}
        </ul>
      </>}
      {avg != null && <p className="muted small w-note">近 {recent.length} 個工作日平均產出 {fmtNum(avg, 1)} 條／日・停機 {recent.reduce((a, d) => a + d.downtime_min, 0)} 分（資料集）</p>}
    </Panel>
  )
}

function MaintenancePanel({ w }: { w: WorldSnapshot }) {
  const m = w.maintenance
  return (
    <Panel title="保養與維修" className="w-maint">
      {m.in_progress.length > 0 && (
        <ul className="w-log">
          {m.in_progress.map((x) => (
            <li key={x.id} className={x.kind === 'repair' ? 'bad' : ''}><span className={`res ${x.kind === 'repair' ? 'bad' : 'warn'}`}>{x.kind === 'repair' ? '維修中' : '保養中'}</span> {machineLabel(x.machine_id)}・{x.title}{x.assignee ? `（${x.assignee}）` : ''}</li>
          ))}
        </ul>
      )}
      <p className="sub">接下來的計畫保養</p>
      <ul className="w-log">
        {[...m.upcoming.slice(0, 4), ...m.upcoming.slice(4).filter((x) => x.kind === 'mold_change')].map((x) => (
          <li key={x.id}><span className="muted">{fmtMD(x.start)} {fmtHM(x.start)}</span> {machineLabel(x.machine_id)}・{x.title}{x.source === 'team_dataset' && <i className="tag">資料集</i>}</li>
        ))}
      </ul>
    </Panel>
  )
}

function FeedPanel({ w }: { w: WorldSnapshot }) {
  return (
    <Panel title="工廠動態" className="w-feed">
      <ul className="w-log" data-testid="world-feed">
        {w.events.slice(0, 8).map((e, i) => (
          <li key={i} className={`k-${e.kind}`}><span className="muted">{fmtMD(e.ts)} {fmtHM(e.ts)}</span> {e.text}</li>
        ))}
        {w.events.length === 0 && <li className="muted">尚無動態</li>}
      </ul>
    </Panel>
  )
}
