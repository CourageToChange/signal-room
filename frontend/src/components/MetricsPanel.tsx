import type { Asset, AssetState, Observation } from '../types'
import { StatusMark } from './StatusMark'

function formatMetric(value: number | null, kind: 'ratio' | 'latency'): string {
  if (value == null) return '—'
  return kind === 'ratio' ? `${Math.round(value * 100)}%` : `${Math.round(value)} ms`
}

function metricValue(sample: Observation): number | null {
  return sample.memory_ratio ?? sample.cpu_ratio ?? (sample.latency_ms != null ? Math.min(sample.latency_ms / 1000, 1) : null)
}

function Sparkline({ samples }: { samples: Observation[] }) {
  const values = samples.map(metricValue).filter((value): value is number => value != null)
  if (values.length < 2) {
    return <div className="sparkline sparkline--empty">Trend appears after two observations</div>
  }
  const points = values
    .map((value, index) => `${(index / (values.length - 1)) * 300},${76 - Math.min(value, 1) * 68}`)
    .join(' ')
  return (
    <svg className="sparkline" viewBox="0 0 300 84" role="img" aria-label="Recent metric trend">
      <path className="sparkline__grid" d="M0 20H300 M0 42H300 M0 64H300" />
      <polyline points={points} fill="none" />
    </svg>
  )
}

export function MetricsPanel({
  asset,
  state,
  samples,
}: {
  asset: Asset
  state: AssetState
  samples: Observation[]
}) {
  const lastSeen = state.last_observed_at ? new Date(state.last_observed_at).toLocaleTimeString() : 'Never'
  return (
    <section className="panel metrics" aria-labelledby="metrics-title">
      <div className="panel__heading">
        <div>
          <p className="eyebrow">Selected asset</p>
          <h2 id="metrics-title">{asset.label}</h2>
        </div>
        <StatusMark health={state.health} />
      </div>
      <p className="metrics__message">{state.message}</p>
      <dl className="metric-grid">
        <div><dt>CPU</dt><dd>{formatMetric(state.cpu_ratio, 'ratio')}</dd></div>
        <div><dt>Memory</dt><dd>{formatMetric(state.memory_ratio, 'ratio')}</dd></div>
        <div><dt>Disk</dt><dd>{formatMetric(state.disk_ratio, 'ratio')}</dd></div>
        <div><dt>Latency</dt><dd>{formatMetric(state.latency_ms, 'latency')}</dd></div>
      </dl>
      <Sparkline samples={samples} />
      <p className="metrics__seen">Last observed {lastSeen}</p>
    </section>
  )
}
