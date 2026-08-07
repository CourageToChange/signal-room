import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { metrics } from '../test/fixtures'
import { MetricChart } from './MetricChart'

describe('MetricChart', () => {
  it('labels independent series, thresholds, and the table alternative', () => {
    render(<MetricChart data={metrics} />)
    expect(screen.getByText('75% complete · 1h resolution')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'CPU over the selected range' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Memory over the selected range' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Latency over the selected range' })).toBeInTheDocument()
    expect(screen.getAllByText('Warning threshold 90%')).toHaveLength(2)
    expect(screen.getAllByText('5000 ms')).toHaveLength(2)
    expect(screen.getByRole('table')).toHaveTextContent('—')
  })

  it('explains sparse and absent series without drawing a misleading trend', () => {
    render(<MetricChart data={{ ...metrics, buckets: [metrics.buckets[0]] }} />)
    expect(screen.getAllByText('Trend appears after two complete buckets')).toHaveLength(4)
    expect(screen.getByText('No data')).toBeInTheDocument()
  })
})
