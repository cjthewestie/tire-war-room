// 工廠全貌 (issue #11): the whole plant around the 28 machines — order pipeline, raw materials against their
// reorder points, the loading dock, people by zone, energy, quality and maintenance. Read only; data comes from
// GET /api/world and the WS "world" deltas (warroom/sim/world.py).
import type { WorldSnapshot, WorldZone, WorkerState } from '../contracts'
import { LINE_LABELS } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD, fmtNum } from '../format'
import { useWarRoom } from '../useWarRoom'
import { Panel } from './common'

const STAGES = ['待排程', '生產中', '完成', '待出貨', '已出貨'] as const
const SIZE_LABEL = { S: '小胎', M: '中胎', L: '大胎' } as const
const ZONE_LABEL: Record<WorldZone, string> = {
  war_room: '戰情室', staff: '員工待命區', line_A: 'A 胎體／胎面膠', line_B: 'B 胎側膠', line_C: 'C 鋼絲圈', line_D: 'D 尼龍布',
  line_BOND: '貼合', line_CURE: '硫化區', break: '休息區', toilet: '廁所', dock: '碼頭', off_site: '廠外',
}
const ZONE_ORDER: WorldZone[] = ['war_room', 'line_A', 'line_B', 'line_C', 'line_D', 'line_BOND', 'line_CURE', 'dock', 'staff', 'break', 'toilet']
const STATE_ORDER: WorkerState[] = ['在機台', '搬運', '待命', '休息', '如廁', '請假', '下班']
const MAT_STATUS = { ok: '充足', reorder: '已達再訂購點', low: '偏低', out: '缺料' } as const

export function WorldTab() {
  const { state, transport } = useWarRoom()
  const w = state.world
  if (!w) {
    return <div className="loading">{transport?.mode === 'mock' ? '工廠全貌需要連線後端（瀏覽器模擬資料不含這部分）。' : '等待工廠全貌資料…'}</div>
  }
  return (
    <div className="world-wrap" data-testid="world-tab" data-rev={w.rev}>
      <WorldKpis w={w} />
      <div className="world">
        <OrdersPanel w={w} />
        <MaterialsPanel w={w} />
        <DockPanel w={w} />
        <PeoplePanel w={w} />
        <EnergyPanel w={w} />
        <QualityPanel w={w} />
        <MaintenancePanel w={w} />
        <FeedPanel w={w} />
      </div>
    </div>
  )
}

function WorldKpis({ w }: { w: WorldSnapshot }) {
  const low = w.materials.filter((m) => m.status === 'low' || m.status === 'out')
  const e = w.energy
  const q = w.quality
  return (
    <section className="kpis" aria-label="工廠全貌重點">
      <div className="kpi">
        <span className="kpi-l">在廠人員</span>
        <span className="kpi-v" data-testid="kpi-people">{w.people.on_duty}<small> / {w.people.total} 人</small></span>
        <span className="kpi-s">在機台 {w.people.by_state['在機台'] ?? 0}・休息 {w.people.by_state['休息'] ?? 0}</span>
      </div>
      <div className={`kpi ${e.peak ? 'is-warn' : 'is-live'}`}>
        <span className="kpi-l">用電{e.peak ? '・尖峰時段' : ''}</span>
        <span className="kpi-v" data-testid="kpi-power">{fmtNum(e.total_kw, 0)}<small> kW</small></span>
        <span className="kpi-s">契約 {fmtNum(e.contract_kw, 0)} kW・今日 {fmtNum(e.kwh_today, 0)} kWh</span>
      </div>
      <div className={`kpi ${low.length ? 'is-alarm' : ''}`}>
        <span className="kpi-l">原料</span>
        <span className={`kpi-v ${low.length ? 'kpi-word' : ''}`}>{low.length ? `${low.map((m) => m.name).join('、')} 偏低` : <>正常<small> {w.materials.length} 項</small></>}</span>
        <span className="kpi-s">在途採購 {w.purchase_orders.filter((p) => p.status !== '已收貨').length} 筆</span>
      </div>
      <div className={`kpi ${q.defects_today ? 'is-warn' : ''}`}>
        <span className="kpi-l">今日良率</span>
        <span className="kpi-v">{q.yield_pct == null ? '—' : fmtNum(q.yield_pct, 1)}<small>{q.yield_pct == null ? '' : ' %'}</small></span>
        <span className="kpi-s">檢驗 {q.inspected_today} 批・不良 {q.defects_today}</span>
      </div>
    </section>
  )
}

function OrdersPanel({ w }: { w: WorldSnapshot }) {
  const open = w.orders.items.filter((o) => o.status === '待排程' || o.status === '生產中').slice(0, 6)
  return (
    <Panel title="訂單流程" className="w-orders" right={<span className="muted small">今日新單 {w.orders.new_today}{w.orders.rush_open ? `・急單 ${w.orders.rush_open}` : ''}</span>}>
      <ol className="pipe" data-testid="order-pipeline">
        {STAGES.map((s) => (
          <li key={s} className={w.orders.pipeline[s] ? 'has' : ''}><b>{w.orders.pipeline[s] ?? 0}</b><span>{s}</span></li>
        ))}
      </ol>
      <ul className="w-list">
        {open.map((o) => (
          <li key={o.id}>
            <div className="w-row">
              <span><b>{o.id}</b>{o.rush && <i className="tag rush">急單</i>}<span className="muted"> {o.customer}・{SIZE_LABEL[o.size]}</span></span>
              <span className="muted small">{o.done}/{o.qty}・交期 {fmtMD(o.due)}</span>
            </div>
            <div className="bar"><i style={{ width: `${Math.min(100, (100 * o.done) / Math.max(1, o.qty))}%` }} /></div>
          </li>
        ))}
      </ul>
      <p className="muted small w-note">庫存成品 {w.finished_goods.total}/{w.finished_goods.capacity} 條（小 {w.finished_goods.by_size.S}・中 {w.finished_goods.by_size.M}・大 {w.finished_goods.by_size.L}）・待出貨 {w.finished_goods.waiting_ship} 條</p>
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
  const max = Math.max(1, ...Object.values(p.by_zone).map((n) => n ?? 0))
  return (
    <Panel title="人員分布" className="w-people" right={<span className="muted small">在廠 {p.on_duty}・全員 {p.total}</span>}>
      <ul className="zones" data-testid="people-zones">
        {ZONE_ORDER.filter((z) => p.by_zone[z]).map((z) => (
          <li key={z}><span>{ZONE_LABEL[z]}</span><span className="zbar"><i style={{ width: `${(100 * (p.by_zone[z] ?? 0)) / max}%` }} /></span><b>{p.by_zone[z]}</b></li>
        ))}
      </ul>
      <div className="states">
        {STATE_ORDER.filter((s) => p.by_state[s]).map((s) => <span key={s} className={`st st-${s}`}>{s} {p.by_state[s]}</span>)}
      </div>
      <ul className="w-mini">
        {p.workers.filter((x) => x.role === '主管' || x.state === '請假' || x.role === '維修').filter((x) => x.state !== '下班').slice(0, 4).map((x) => (
          <li key={x.id}><b>{x.name}</b> <span className="muted">{x.team_label}・{x.shift}</span>・{x.state}{x.state !== '請假' ? `（${ZONE_LABEL[x.zone]}${x.machine_id ? ` ${machineLabel(x.machine_id)}` : ''}）` : ''}</li>
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
        {m.upcoming.slice(0, 4).map((x) => (
          <li key={x.id}><span className="muted">{fmtMD(x.start)} {fmtHM(x.start)}</span> {machineLabel(x.machine_id)}・{x.title}</li>
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
