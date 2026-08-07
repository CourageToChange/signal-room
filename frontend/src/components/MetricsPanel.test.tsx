import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MetricsPanel } from './MetricsPanel'

describe('MetricsPanel', () => {
  it('formats current resource and latency values', () => {
    render(
      <MetricsPanel
        asset={{ id: 'guest', label: 'Orchid', kind: 'guest', depends_on: [], parent_id: null, check_ids: ['health'], runbook_id: null, sort_order: 1, retired_at: null }}
        state={{
          asset_id: 'guest', health: 'degraded', last_observed_at: '2026-07-15T18:00:00Z',
          unhealthy_since_at: '2026-07-15T17:59:00Z', consecutive_failures: 3,
          consecutive_successes: 0, message: 'Memory pressure', latency_ms: 41.4,
          cpu_ratio: 0.51, memory_ratio: 0.97, disk_ratio: 0.34,
        }}
        samples={[]}
      />,
    )
    expect(screen.getByRole('heading', { name: 'Orchid' })).toBeInTheDocument()
    expect(screen.getByText('97%')).toBeInTheDocument()
    expect(screen.getByText('41 ms')).toBeInTheDocument()
    expect(screen.getByText(/Trend appears/)).toBeInTheDocument()
  })

  it('handles missing current values and CPU/latency-only trend samples', () => {
    render(
      <MetricsPanel
        asset={{ id: 'service', label: 'Gallery', kind: 'service', depends_on: [], parent_id: null, check_ids: ['https'], runbook_id: null, sort_order: 1, retired_at: null }}
        state={{ asset_id: 'service', health: 'unknown', last_observed_at: null, unhealthy_since_at: null, consecutive_failures: 0, consecutive_successes: 0, message: 'Awaiting telemetry', latency_ms: null, cpu_ratio: null, memory_ratio: null, disk_ratio: null }}
        samples={[
          { asset_id: 'service', observed_at: '2026-07-15T17:00:00Z', health: 'healthy', message: 'CPU', latency_ms: null, cpu_ratio: 0.4, memory_ratio: null, disk_ratio: null, details: {} },
          { asset_id: 'service', observed_at: '2026-07-15T18:00:00Z', health: 'degraded', message: 'Latency', latency_ms: 1500, cpu_ratio: null, memory_ratio: null, disk_ratio: null, details: {} },
          { asset_id: 'service', observed_at: '2026-07-15T18:01:00Z', health: 'unknown', message: 'None', latency_ms: null, cpu_ratio: null, memory_ratio: null, disk_ratio: null, details: {} },
        ]}
      />,
    )
    expect(screen.getAllByText('—')).toHaveLength(4)
    expect(screen.getByRole('img', { name: 'Recent metric trend' })).toBeInTheDocument()
    expect(screen.getByText('Last observed Never')).toBeInTheDocument()
  })
})
