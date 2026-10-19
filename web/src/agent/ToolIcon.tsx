import type { ReactNode } from 'react'

const PATHS: Record<string, ReactNode> = {
  read_sensors: <><path d="M4 18a8 8 0 1 1 16 0" /><path d="M12 18l4-6" /></>,
  predict_anomaly: <path d="M3 17l6-6 4 4 8-8M15 7h6v6" />,
  cure_formula: <path d="M9 3h6M10 3v6l-5 9a1 1 0 0 0 1 1.5h12a1 1 0 0 0 1-1.5l-5-9V3" />,
  solve_schedule: <path d="M4 6h9M8 12h12M4 18h7" />,
  sop_search: <><circle cx="11" cy="11" r="6" /><path d="M16 16l4 4" /></>,
  query_orders_shifts: <><rect x="6" y="5" width="12" height="15" rx="2" /><path d="M9 3h6v4H9zM9 12h6M9 16h4" /></>,
  confirm_schedule: <path d="M5 12l5 5 9-10" />,
  set_cure_time: <><circle cx="12" cy="12" r="8" /><path d="M12 8v4l3 2" /></>,
  notify_teams: <path d="M6 16v-5a6 6 0 0 1 12 0v5l2 2H4zM10 21h4" />,
  gate: <path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" />,
  think: <><circle cx="12" cy="12" r="3" /><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1" /></>,
  blocked: <><circle cx="12" cy="12" r="8" /><path d="M6.5 6.5l11 11" /></>,
  human: <><circle cx="12" cy="8" r="3.5" /><path d="M5 20c1-4 4-6 7-6s6 2 7 6" /></>,
  safety: <path d="M12 3l9 16H3zM12 10v4M12 17v.5" />,
}

export function ToolIcon({ name, className = '' }: { name: string; className?: string }) {
  return (
    <svg className={`ticon ${className}`} viewBox="0 0 24 24" aria-hidden>
      {PATHS[name] ?? <circle cx="12" cy="12" r="4" />}
    </svg>
  )
}
