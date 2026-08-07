import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { bootstrap, incident } from '../test/fixtures'
import type { DrillSnapshot } from '../types'
import { ConsoleLayout } from './ConsoleLayout'

const snapshot: DrillSnapshot = { ...bootstrap, incidents: [incident] }

describe('ConsoleLayout', () => {
  it('renders an unknown fallback selection, nominal posture, and stale state', () => {
    render(
      <ConsoleLayout
        snapshot={{ ...snapshot, stale: true, states: [], incidents: [] }}
        selectedAssetId="missing"
        selectedIncidentId={null}
        samples={[]}
        onSelectAsset={() => undefined}
        onSelectIncident={() => undefined}
      />,
    )
    expect(screen.getByText('Systems nominal')).toBeInTheDocument()
    expect(screen.getByText('Telemetry stale')).toBeInTheDocument()
    expect(screen.getAllByLabelText('Status: Unknown').length).toBeGreaterThan(0)
    expect(screen.queryByText(/Simulation ·/)).not.toBeInTheDocument()
  })

  it('distinguishes an interruption from degraded attention', () => {
    const view = render(
      <ConsoleLayout snapshot={snapshot} selectedAssetId="guest" selectedIncidentId={incident.id} samples={[]} simulation onSelectAsset={() => undefined} onSelectIncident={() => undefined} />,
    )
    expect(screen.getByText('Service interruption')).toBeInTheDocument()
    view.rerender(
      <ConsoleLayout snapshot={{ ...snapshot, states: snapshot.states.map((state) => ({ ...state, health: state.asset_id === 'guest' ? 'degraded' as const : 'healthy' as const })) }} selectedAssetId="guest" selectedIncidentId={incident.id} samples={[]} onSelectAsset={() => undefined} onSelectIncident={() => undefined} />,
    )
    expect(screen.getByText('Attention required')).toBeInTheDocument()
  })
})
