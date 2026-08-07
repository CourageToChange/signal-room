import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Asset, AssetState } from '../types'
import { TopologyMap } from './TopologyMap'

// The real graph library must never load in unit tests: importing it hangs the
// vitest process with open handles. This stub renders nodes through the real
// custom node component and exposes edges as plain paths.
vi.mock('@xyflow/react', async () => {
  const React = await import('react')
  type NodeViewProps = { id: string; data: Record<string, unknown> }
  type ReactFlowProps = {
    nodes: Array<{ id: string; type?: string; data: Record<string, unknown> }>
    edges: Array<{ id: string; className?: string }>
    nodeTypes: Record<string, React.ComponentType<NodeViewProps>>
  }
  return {
    Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
    Handle: () => null,
    ReactFlow: ({ nodes, edges, nodeTypes }: ReactFlowProps) => React.createElement(
      'div',
      { 'data-testid': 'react-flow' },
      React.createElement(
        'svg',
        { 'aria-hidden': true },
        edges.map((edge) => React.createElement('path', { key: edge.id, className: edge.className })),
      ),
      nodes.map((node) => {
        const NodeView = nodeTypes[node.type ?? '']
        return NodeView ? React.createElement(NodeView, { key: node.id, id: node.id, data: node.data }) : null
      }),
    ),
  }
})

const assets: Asset[] = [
  { id: 'host', label: 'Atlas', kind: 'node', depends_on: [], parent_id: null, check_ids: ['node'], runbook_id: null, sort_order: 1, retired_at: null },
  { id: 'guest', label: 'Orchid', kind: 'guest', depends_on: ['host'], parent_id: 'host', check_ids: ['guest'], runbook_id: null, sort_order: 2, retired_at: null },
]
const states: AssetState[] = assets.map((asset, index) => ({
  asset_id: asset.id,
  health: index ? 'degraded' : 'healthy',
  last_observed_at: '2026-07-15T18:00:00Z',
  unhealthy_since_at: index ? '2026-07-15T17:59:00Z' : null,
  consecutive_failures: index,
  consecutive_successes: index ? 0 : 2,
  message: index ? 'Memory pressure' : 'Host healthy',
  latency_ms: null,
  cpu_ratio: 0.2,
  memory_ratio: 0.5,
  disk_ratio: 0.4,
}))

describe('TopologyMap', () => {
  it('exposes every asset as a keyboard-operable selection', () => {
    const select = vi.fn()
    render(<TopologyMap assets={assets} states={states} selectedId="host" onSelect={select} />)
    expect(screen.getByRole('group', { name: /dependency map/i })).toBeInTheDocument()
    const orchidButtons = screen.getAllByRole('button', { name: /Orchid/i })
    fireEvent.click(orchidButtons[0])
    expect(select).toHaveBeenCalledWith('guest')
    expect(screen.getAllByLabelText('Status: Degraded').length).toBeGreaterThan(0)
  })

  it('renders assets without telemetry as unknown and filters the mobile hierarchy', () => {
    render(<TopologyMap assets={assets} states={[]} selectedId="host" query="Orchid" onSelect={() => undefined} />)
    expect(screen.getAllByLabelText('Status: Unknown').length).toBeGreaterThan(0)
    expect(screen.getAllByRole('button', { name: /Orchid/i })).toHaveLength(2)
  })

  it('tolerates missing and cyclic dependencies and highlights confirmed paths', () => {
    const unusual: Asset[] = [
      { ...assets[0], depends_on: ['guest'], sort_order: 1 },
      { ...assets[1], depends_on: ['host', 'missing'], sort_order: 1 },
    ]
    render(<TopologyMap assets={unusual} states={states} selectedId="guest" affectedIds={new Set(['host', 'guest'])} query="guest" onSelect={() => undefined} />)
    expect(document.querySelectorAll('path.is-affected')).toHaveLength(2)
    expect(document.querySelectorAll('.is-dimmed').length).toBeGreaterThan(0)
    expect(screen.getAllByRole('button', { name: /Orchid/i })).toHaveLength(2)
  })
})
