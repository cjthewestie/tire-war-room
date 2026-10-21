// Agent Nemo 異常報告 (Kyle 10-21, option A; warroom/agent/reporter.py): written by Agent Nemo (the local model) during the
// 10–15 s repair, the 「結果」 line appended by code when the machine is back to normal (Event.ai_report). Shown on top of
// the Agent page's 處理報告 (ReportBlock) and on /report. Tagged AI 撰寫, or 規則備援 when mock mode / a model failure
// left the code-written draft.
import type { Event } from '../contracts'
import { fmtHM } from '../format'
import { plain } from '../plain'
import './aiReport.css'

export function AiReport({ ev }: { ev: Event }) {
  const r = ev.ai_report
  if (!r) return null
  const [head = '', ...rest] = r.text.split('\n').filter((l) => l.trim())
  const ai = r.by === 'model'
  const secs = r.drafted_ms != null ? `撰寫 ${(r.drafted_ms / 1000).toFixed(1)} 秒` : ''
  const late = r.late_ms ? `・晚 ${(r.late_ms / 1000).toFixed(1)} 秒送達` : ''
  return (
    <section className="ai-report" data-testid="ai-report" data-by={r.by} aria-label="Agent Nemo 異常報告">
      <header>
        <b>Agent Nemo 異常報告</b>
        <span className={`ai-tag${ai ? ' ai' : ''}`} data-testid="ai-report-tag">{ai ? 'AI 撰寫' : '規則備援'}</span>
        <small>{fmtHM(r.at)}{ai && secs ? `・${secs}` : ''}{late}</small>
      </header>
      <h4>{plain(head.replace(/^【異常報告】/, ''))}</h4>
      <dl>
        {rest.map((line, i) => {
          const m = /^([^：]{1,4})：(.*)$/.exec(line)
          return (
            <div key={i} className={m?.[1] === '結果' ? 'ai-res' : ''}>
              <dt>{m ? m[1] : ''}</dt><dd>{plain(m ? m[2] : line)}</dd>
            </div>
          )
        })}
      </dl>
      <p className="ai-foot">{ai ? '由 Agent Nemo 依系統紀錄撰寫；「結果」由程式在機台恢復正常時補上' : '規則備援：模擬模式或模型沒有及時回應，由程式依同一份紀錄整理；「結果」由程式補上'}</p>
    </section>
  )
}
