import type { AssetState, DrillSnapshot, Observation } from '../types'
import { IncidentPanel } from './IncidentPanel'
import { MetricsPanel } from './MetricsPanel'
import { TopologyMap } from './TopologyMap'

export function ConsoleLayout({
  snapshot,
  selectedAssetId,
  selectedIncidentId,
  samples,
  simulation = false,
  controls,
  onSelectAsset,
  onSelectIncident,
  onAcknowledge,
  onAddNote,
  onClose,
}: {
  snapshot: DrillSnapshot
  selectedAssetId: string
  selectedIncidentId: string | null
  samples: Observation[]
  simulation?: boolean
  controls?: React.ReactNode
  onSelectAsset: (id: string) => void
  onSelectIncident: (id: string) => void
  onAcknowledge?: (id: string) => Promise<void>
  onAddNote?: (id: string, note: string) => Promise<void>
  onClose?: (id: string) => Promise<void>
}) {
  const stateById = new Map(snapshot.states.map((state) => [state.asset_id, state]))
  const selectedAsset = snapshot.assets.find((asset) => asset.id === selectedAssetId) ?? snapshot.assets[0]
  const selectedState: AssetState = stateById.get(selectedAsset.id) ?? {
    asset_id: selectedAsset.id,
    health: 'unknown',
    last_observed_at: null,
    unhealthy_since_at: null,
    consecutive_failures: 0,
    consecutive_successes: 0,
    message: 'Awaiting telemetry',
    latency_ms: null,
    cpu_ratio: null,
    memory_ratio: null,
    disk_ratio: null,
  }
  const healthy = snapshot.states.filter((state) => state.health === 'healthy').length
  const degraded = snapshot.states.filter((state) => state.health === 'degraded').length
  const down = snapshot.states.filter((state) => state.health === 'down').length
  return (
    <div className={simulation ? 'console console--simulation' : 'console'}>
      {simulation && <div className="simulation-banner">Simulation · no live systems are connected</div>}
      <header className="masthead">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true"><i /><i /><i /></span>
          <div><strong>Signal Room</strong><span>Homelab operations</span></div>
        </div>
        <div className="masthead__status">
          <span className={down ? 'pulse pulse--danger' : degraded ? 'pulse pulse--warning' : 'pulse'} aria-hidden="true" />
          <span><strong>{down ? 'Service interruption' : degraded ? 'Attention required' : 'Systems nominal'}</strong><small>Observed {new Date(snapshot.generated_at).toLocaleTimeString()}</small></span>
        </div>
        {controls}
      </header>
      <main id="main" tabIndex={-1}>
        <section className="overview" aria-label="Current posture">
          <div><span className="overview__value">{snapshot.assets.length}</span><span>Mapped assets</span></div>
          <div><span className="overview__value overview__value--healthy">{healthy}</span><span>Healthy</span></div>
          <div><span className="overview__value overview__value--warning">{degraded}</span><span>Degraded</span></div>
          <div><span className="overview__value overview__value--danger">{down}</span><span>Down</span></div>
          <div><span className="overview__value">{snapshot.incidents.filter((item) => item.state === 'open').length}</span><span>Open incidents</span></div>
          <div className={snapshot.stale ? 'overview__freshness is-stale' : 'overview__freshness'}>
            <span>{snapshot.stale ? 'Telemetry stale' : 'Collector current'}</span>
            <small>{snapshot.capabilities.data_source} source</small>
          </div>
        </section>
        <div className="workspace-grid">
          <TopologyMap assets={snapshot.assets} states={snapshot.states} selectedId={selectedAsset.id} onSelect={onSelectAsset} />
          <IncidentPanel
            incidents={snapshot.incidents}
            selectedId={selectedIncidentId}
            canMutate={snapshot.capabilities.can_mutate}
            referenceTime={simulation ? snapshot.generated_at : undefined}
            onSelect={onSelectIncident}
            onAcknowledge={onAcknowledge}
            onAddNote={onAddNote}
            onClose={onClose}
          />
          <MetricsPanel asset={selectedAsset} state={selectedState} samples={samples} />
        </div>
      </main>
      <footer><span>Read-only telemetry</span><span>No remote actions · No analytics</span></footer>
    </div>
  )
}
