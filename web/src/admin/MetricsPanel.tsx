// 後台 狀態: live machine + model numbers, polled every 3 s from /api/admin/metrics (warroom/api/metrics.py), with
// sparklines for the last 10 minutes. Every number's source is in its tooltip; a number the platform cannot give
// shows 「此平台無法取得」 with the reason, never a guess. CJ, 2026-10-22: 「系統部分是要實時報告模型跟機器記憶體運做之類的數據」.
import { useEffect, useState } from 'react'
import { adminFetch } from '../transport'
import { Sparkline } from '../components/common'

interface M { v: number | boolean | null; src: string; na?: string; total?: number | null; cores?: number; context?: number | null; others?: string[] }
interface Gpu { name: string; util_pct: M; temp_c: M; power_w: M; mem_used: M }
interface Run { work: string; label: string; priority: number; secs: number }
interface Metrics {
  interval_s: number; at: string
  machine: { platform: string; cpu_pct: M; ram_used: M; disk_free: M; rss: M; proc_cpu: M; uptime_s: M; boot_s: M; sampler_ms: M; gpus: Gpu[]; gpu_na: string | null; wired: M | null }
  model: {
    mode: string; model: string; host: string | null; server: string | null; loaded: M; memory: M; vram: M; cache_pct: M; vllm_running: M; vllm_waiting: M
    p50_ms: M; p90_ms: M; budget_refused: M; tps_last: M; tps_live: M; calls_last_min: M; calls_seen: M; fallbacks: M; errors: M
    queue: { running: Run[]; waiting: number; slots: number | null; src: string }
  }
  history: Record<'t' | 'cpu' | 'ram' | 'gpu' | 'model_mem' | 'tps' | 'queue' | 'calls_min', (number | null)[]>
}

const POLL_MS = 3000
const NA = '此平台無法取得'
const gb = (b: number) => `${(b / 2 ** 30).toFixed(1)} GB`
const sec = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒` : `${Math.round(ms)} ms`)
function dur(s: number): string {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  return d ? `${d} 天 ${h} 小時` : h ? `${h} 小時 ${m} 分` : `${m} 分 ${Math.floor(s % 60)} 秒`
}
const num = (m: M | null | undefined): number | null => (m && typeof m.v === 'number' ? m.v : null)

function Stat({ label, m, fmt, spark, lo, hi, testid }: {
  label: string; m: M | null | undefined; fmt: (v: number, m: M) => string; spark?: (number | null)[]; lo?: number; hi?: number; testid?: string
}) {
  const v = num(m)
  return (
    <div className="mx-stat" title={m ? `來源：${m.src}${m.na ? `\n${m.na}` : ''}` : ''} data-testid={testid}>
      <span className="mx-label">{label}</span>
      {v != null && m ? <b className="mx-val">{fmt(v, m)}</b> : <span className="mx-na">{NA}{m?.na && m.na !== NA ? <small>{m.na}</small> : null}</span>}
      {spark && <Sparkline values={spark} height={28} lo={lo} hi={hi} />}
    </div>
  )
}

export function MetricsPanel() {
  const [d, setD] = useState<Metrics | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let off = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      if (document.hidden) { timer = setTimeout(tick, POLL_MS); return }
      try {
        const m = await adminFetch<Metrics>('/api/admin/metrics')
        if (off) return
        setD(m); setErr(null)
      } catch (e) {
        if (off) return
        setErr(String(e).replace(/^Error: /, ''))
        if (/^40[13]/.test(String(e).replace(/^Error: /, ''))) return     // no access: stop, never prompt for a PIN every 3 s
      }
      timer = setTimeout(tick, POLL_MS)
    }
    void tick()
    return () => { off = true; clearTimeout(timer) }
  }, [])

  if (!d) return <section className="mx" data-testid="metrics">{err ? <p className="mx-err">即時數據讀不到：{err}</p> : <p className="muted">讀取即時數據…</p>}</section>
  const { machine: mc, model: md, history: h } = d
  const ramTotal = mc.ram_used.total ?? 0
  const live = md.mode !== 'mock'
  const q = md.queue
  return (
    <section className="mx" data-testid="metrics" aria-live="off">
      <header className="mx-head">
        <h2>即時數據</h2>
        <span className="mx-tick" title={`每 ${d.interval_s} 秒更新；曲線是最近 10 分鐘（打開這頁之後開始累積）`}>
          <i className={err ? 'stale' : ''} />{err ? `更新失敗：${err}` : `每 ${d.interval_s} 秒更新・${new Date(d.at).toLocaleTimeString('zh-TW', { hour12: false })}`}
        </span>
      </header>

      <div className="mx-grid">
        <article className="mx-card" data-testid="metrics-machine">
          <h3>機器 <small>{mc.platform}</small></h3>
          <Stat label="CPU" m={mc.cpu_pct} fmt={(v, m) => `${v.toFixed(0)}%${m.cores ? `・${m.cores} 核` : ''}`} spark={h.cpu} lo={0} hi={100} testid="mx-cpu" />
          <Stat label="記憶體" m={mc.ram_used} fmt={(v) => `${gb(v)} / ${gb(ramTotal)}`} spark={h.ram} lo={0} hi={ramTotal} testid="mx-ram" />
          {mc.wired && <Stat label="Wired（含 GPU）" m={mc.wired} fmt={(v) => `${gb(v)} / ${gb(ramTotal)}`} />}
          {mc.gpus.map((g, i) => (
            <div key={i} className="mx-sub">
              <span className="mx-gpu">{g.name}</span>
              <Stat label="GPU 使用率" m={g.util_pct} fmt={(v) => `${v.toFixed(0)}%`} spark={i === 0 ? h.gpu : undefined} lo={0} hi={100} testid="mx-gpu" />
              <Stat label="GPU 溫度" m={g.temp_c} fmt={(v) => `${v.toFixed(0)}°C`} />
              <Stat label="GPU 功耗" m={g.power_w} fmt={(v) => `${v.toFixed(0)} W`} />
              <Stat label="GPU 記憶體" m={g.mem_used} fmt={(v, m) => `${gb(v)}${m.total ? ` / ${gb(m.total)}` : ''}`} />
            </div>
          ))}
          {mc.gpu_na && (
            <div className="mx-stat" title={`來源：${mc.gpu_na}`} data-testid="mx-gpu-na">
              <span className="mx-label">GPU 使用率／溫度</span><span className="mx-na">{NA}<small>{mc.gpu_na}</small></span>
            </div>
          )}
          <Stat label="磁碟剩餘" m={mc.disk_free} fmt={(v, m) => `${gb(v)}${m.total ? ` / ${gb(m.total)}` : ''}`} />
          <Stat label="伺服器記憶體 (RSS)" m={mc.rss} fmt={gb} />
          <Stat label="伺服器 CPU" m={mc.proc_cpu} fmt={(v) => `${v.toFixed(1)}%`} />
          <Stat label="伺服器運轉" m={mc.uptime_s} fmt={dur} />
          <Stat label="機器開機" m={mc.boot_s} fmt={dur} />
          <Stat label="取樣耗時" m={mc.sampler_ms} fmt={(v) => `${v} ms`} />
        </article>

        <article className="mx-card" data-testid="metrics-model">
          <h3>模型 <small title="模型伺服器（BASE_URL 主機）">{live ? `${md.server ?? '?'}・${md.host ?? '—'}` : '規則模式'}</small></h3>
          <p className="mx-model" title="MODEL 設定">{md.model}</p>
          <div className="mx-stat" title={`來源：${md.loaded.src}${md.loaded.na ? `\n${md.loaded.na}` : ''}`} data-testid="mx-loaded">
            <span className="mx-label">載入狀態</span>
            {md.loaded.v == null ? <span className="mx-na">{md.loaded.na ?? NA}</span>
              : <b className={`mx-val ${md.loaded.v ? 'ok' : 'warn'}`}>{md.loaded.v ? '已載入' : '未載入（下一次呼叫會先載入，較慢）'}{md.loaded.context ? `・context ${md.loaded.context}` : ''}</b>}
          </div>
          <Stat label="模型記憶體" m={md.memory} fmt={gb} spark={h.model_mem} lo={0} testid="mx-model-mem" />
          {md.server !== 'vllm' && <Stat label="在 GPU 的部分" m={md.vram} fmt={gb} />}
          {md.server === 'vllm' && <>
            <Stat label="KV cache 使用率" m={md.cache_pct} fmt={(v) => `${v}%`} />
            <Stat label="vLLM 執行中／排隊" m={md.vllm_running} fmt={(v) => `${v} / ${num(md.vllm_waiting) ?? '—'}`} />
          </>}
          <Stat label="tokens/s" m={md.tps_live} fmt={(v) => `${v}${num(md.tps_last) != null ? `（最近一次 ${num(md.tps_last)}）` : ''}`} spark={h.tps} lo={0} testid="mx-tps" />
          <Stat label="延遲 p50 / p90" m={md.p50_ms} fmt={(v) => `${sec(v)} / ${num(md.p90_ms) != null ? sec(num(md.p90_ms)!) : '—'}`} />
          <Stat label="最近一分鐘呼叫" m={md.calls_last_min} fmt={(v) => `${v} 次`} spark={h.calls_min} lo={0} testid="mx-calls" />
          <Stat label="改用規則（備援）" m={md.fallbacks} fmt={(v) => `${v} 次${md.calls_seen.v ? `／${md.calls_seen.v} 次呼叫` : ''}${num(md.errors) ? `・失敗 ${num(md.errors)}` : ''}${num(md.budget_refused) ? `・用量上限擋下 ${num(md.budget_refused)}` : ''}`} />
          <div className="mx-stat mx-queue" title={`來源：${q.src}`} data-testid="mx-queue">
            <span className="mx-label">模型排隊 {q.slots != null ? <small>（同時 {q.slots} 個）</small> : null}</span>
            <b className="mx-val">{q.running.length ? q.running.map((r) => `${r.work}${r.label && r.label !== r.work ? `「${r.label}」` : ''} ${r.secs.toFixed(0)} 秒`).join('、') : '閒置'}{q.waiting ? `・等待 ${q.waiting}` : ''}</b>
            <Sparkline values={h.queue} height={28} lo={0} />
          </div>
        </article>
      </div>
    </section>
  )
}
