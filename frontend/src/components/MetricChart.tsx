import type { MetricBucket, MetricsResponse } from '../types'

type MetricKey = 'cpu_ratio' | 'memory_ratio' | 'disk_ratio' | 'latency_ms'

const series: Array<{
  key: MetricKey
  label: string
  colour: string
  threshold: (data: MetricsResponse) => number | null
  format: (value: number) => string
}> = [
  { key: 'cpu_ratio', label: 'CPU', colour: '#45d7d0', threshold: (data) => data.thresholds.cpu_warning_ratio, format: (value) => `${Math.round(value * 100)}%` },
  { key: 'memory_ratio', label: 'Memory', colour: '#6da7ff', threshold: (data) => data.thresholds.memory_warning_ratio, format: (value) => `${Math.round(value * 100)}%` },
  { key: 'disk_ratio', label: 'Disk', colour: '#ffbe55', threshold: (data) => data.thresholds.disk_warning_ratio, format: (value) => `${Math.round(value * 100)}%` },
  { key: 'latency_ms', label: 'Latency', colour: '#ff7ca8', threshold: () => null, format: (value) => `${Math.round(value)} ms` },
]

function SeriesChart({
  data,
  item,
}: {
  data: MetricsResponse
  item: (typeof series)[number]
}) {
  const values = data.buckets
    .map((bucket, index) => ({ index, value: bucket[item.key] }))
    .filter((entry): entry is { index: number; value: number } => entry.value != null)
  const threshold = item.threshold(data)
  const maximum = item.key === 'latency_ms'
    ? Math.max(1, ...values.map((entry) => entry.value))
    : 1
  const points = values.map(({ index, value }) => {
    const x = data.buckets.length <= 1 ? 0 : (index / (data.buckets.length - 1)) * 300
    const y = 76 - Math.min(1, value / maximum) * 68
    return `${x},${y}`
  }).join(' ')
  const latest = values.at(-1)?.value
  return (
    <section className="metric-series" aria-labelledby={`series-${item.key}`}>
      <div className="metric-series__heading">
        <h3 id={`series-${item.key}`}><i style={{ background: item.colour }} />{item.label}</h3>
        <strong>{latest == null ? 'No data' : item.format(latest)}</strong>
      </div>
      {values.length < 2 ? (
        <div className="sparkline sparkline--empty">Trend appears after two complete buckets</div>
      ) : (
        <svg className="sparkline" viewBox="0 0 300 84" role="img" aria-label={`${item.label} over the selected range`}>
          <path className="sparkline__grid" d="M0 20H300 M0 42H300 M0 64H300" />
          {threshold != null && <path className="metric-series__threshold" d={`M0 ${76 - threshold * 68}H300`} />}
          <polyline points={points} fill="none" style={{ stroke: item.colour }} />
        </svg>
      )}
      {threshold != null && <small>Warning threshold {item.format(threshold)}</small>}
    </section>
  )
}

function cell(bucket: MetricBucket, key: MetricKey, format: (value: number) => string) {
  const value = bucket[key]
  return value == null ? '—' : format(value)
}

export function MetricChart({ data }: { data: MetricsResponse }) {
  return (
    <div className="metric-explorer">
      <p className="metrics__completeness">{Math.round(data.completeness * 100)}% complete · {data.resolution} resolution</p>
      <div className="metric-series-grid">
        {series.map((item) => <SeriesChart key={item.key} data={data} item={item} />)}
      </div>
      <details className="metric-table">
        <summary>View metric data table</summary>
        <div className="table-scroll">
          <table>
            <caption>Accessible metric values for the selected range</caption>
            <thead><tr><th scope="col">Time</th>{series.map((item) => <th scope="col" key={item.key}>{item.label}</th>)}<th scope="col">Complete</th></tr></thead>
            <tbody>
              {data.buckets.map((bucket) => (
                <tr key={bucket.started_at}>
                  <th scope="row">{new Date(bucket.started_at).toLocaleString()}</th>
                  {series.map((item) => <td key={item.key}>{cell(bucket, item.key, item.format)}</td>)}
                  <td>{Math.round(bucket.completeness * 100)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}
