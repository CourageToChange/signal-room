import type { HealthState } from '../types'

const labels: Record<HealthState, string> = {
  healthy: 'Healthy',
  degraded: 'Degraded',
  down: 'Down',
  unknown: 'Unknown',
}

export function StatusMark({ health, compact = false }: { health: HealthState; compact?: boolean }) {
  return (
    <span className={`status-mark status-mark--${health}`} aria-label={`Status: ${labels[health]}`}>
      <span className="status-mark__glyph" aria-hidden="true" />
      {!compact && <span>{labels[health]}</span>}
    </span>
  )
}
