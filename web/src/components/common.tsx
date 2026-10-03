import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { EventState, MachineStatus, RiskLevel, TimelineItem } from '../contracts'
import { fmtHMS } from '../format'

export const STATE_LABEL: Record<EventState, string> = {
  detected: '偵測', classify: '分類', plan: '規劃', simulate: '模擬', gate: '把關', await_approval: '待核准',
  execute: '執行', verify: '驗證', replan: '換假設', closed: '結案', safe_stop: '安全停止',
}
export const STATUS_LABEL: Record<MachineStatus, string> = {
  running: '運轉', idle: '待機', warning: '注意', alarm: '異常', stopped: '停機', out_of_service: '停用', offline: '離線',
}
export const RISK_LABEL: Record<RiskLevel, string> = { R0: 'R0 讀取／計算', R1: 'R1 自動', R2: 'R2 需核准', R3: 'R3 需核准＋理由' }

/** Highlights the proposal's inline markers: [工具] tool call, [人工] human-in-the-loop. */
export function MarkedText({ text }: { text: string }) {
  const parts = text.split(/(\[工具\]|\[人工\]|\d+(?:\.\d+)?\s?°C|\+\d+(?:\.\d+)? h)/g)
  return (
    <>
      {parts.map((p, i) => {
        if (p === '[工具]') return <span key={i} className="mk-tool">[工具]</span>
        if (p === '[人工]') return <span key={i} className="mk-human">[人工]</span>
        if (/^\+\d/.test(p)) return <b key={i} className="mk-num">{p}</b>
        return <span key={i}>{p}</span>
      })}
    </>
  )
}

const TAG_LABEL: Record<TimelineItem['tag'], string> = {
  tool: '工具調用', safety: '安全路徑', decision: '自主決策', info: '任務分解', human: '權限＋人工', dynamic: '動態調整',
}

export function TimelineTable({ items, compact = false }: { items: TimelineItem[]; compact?: boolean }) {
  if (!items.length) return <p className="muted">尚無事件紀錄。</p>
  return (
    <div className={`timeline ${compact ? 'compact' : ''}`} role="table" aria-label="事件時間軸">
      {items.map((it, i) => (
        <div key={i} className={`tl-row tag-${it.tag}`} role="row">
          <span className="tl-time" role="cell">{fmtHMS(it.ts)}</span>
          <span className={`tl-actor actor-${actorClass(it.actor)}`} role="cell">{it.actor}</span>
          <span className="tl-text" role="cell"><MarkedText text={it.text} /></span>
          {!compact && <span className={`tl-tag tag-${it.tag}`} role="cell">{TAG_LABEL[it.tag]}</span>}
        </div>
      ))}
    </div>
  )
}
function actorClass(a: TimelineItem['actor']): string {
  return a === '安全核心' ? 'safety' : a === '指揮官' ? 'cmd' : a === '主管' ? 'human' : a.startsWith('驗證') ? 'verify' : 'plain'
}

export function Panel({ title, num, children, className = '', right }: { title: string; num?: string; children: ReactNode; className?: string; right?: ReactNode }) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-h">
        <h2>{num && <span className="num">{num}</span>}{title}</h2>
        {right}
      </header>
      {children}
    </section>
  )
}

export function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null)
  const [w, setW] = useState(800)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.floor(e.contentRect.width))))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

export function Sparkline({ values, width = 260, height = 56, lo, hi, band, color = 'var(--accent)', threshold }: {
  values: (number | null)[]; width?: number; height?: number; lo?: number; hi?: number
  band?: [number, number]; color?: string; threshold?: { v: number; label: string }[]
}) {
  const vals = values.filter((v): v is number => v != null && isFinite(v))
  if (vals.length < 2) return <svg width={width} height={height} className="spark" aria-hidden />
  let min = lo ?? Math.min(...vals), max = hi ?? Math.max(...vals)
  for (const t of threshold ?? []) { min = Math.min(min, t.v); max = Math.max(max, t.v) }
  if (band) { min = Math.min(min, band[0]); max = Math.max(max, band[1]) }
  const pad = (max - min) * 0.12 || 1
  min -= pad; max += pad
  const x = (i: number) => (i / (values.length - 1)) * width
  const y = (v: number) => height - ((v - min) / (max - min)) * height
  let d = ''
  values.forEach((v, i) => { if (v == null || !isFinite(v)) return; d += `${d ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}` })
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="spark" role="img">
      {band && <rect x={0} y={y(band[1])} width={width} height={Math.max(0, y(band[0]) - y(band[1]))} className="spark-band" />}
      {(threshold ?? []).map((t) => (
        <g key={t.label}>
          <line x1={0} x2={width} y1={y(t.v)} y2={y(t.v)} className="spark-th" />
          <text x={width - 2} y={y(t.v) - 2} textAnchor="end" className="spark-th-t">{t.label}</text>
        </g>
      ))}
      <path d={d} fill="none" stroke={color} strokeWidth={1.6} vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
