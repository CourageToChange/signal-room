import type { CSSProperties } from 'react'
import { Handle, Position, ReactFlow } from '@xyflow/react'
import type { Edge, Node, NodeProps } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { Asset, AssetState } from '../types'
import { StatusMark } from './StatusMark'
import { layoutTopology } from './topologyLayout'

const NODE_WIDTH = 185
const NODE_HEIGHT = 72

const unknownState = (assetId: string): AssetState => ({
  asset_id: assetId,
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
})

type TopologyNodeData = {
  asset: Asset
  state: AssetState
  selected: boolean
  affected: boolean
  dimmed: boolean
  onSelect: (assetId: string) => void
}

type TopologyNode = Node<TopologyNodeData, 'asset'>

function AssetNode({ data }: NodeProps<TopologyNode>) {
  const { asset, state, selected, affected, dimmed, onSelect } = data
  return (
    <>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <button
        type="button"
        className={`asset-node asset-node--${state.health} ${selected ? 'is-selected' : ''} ${affected ? 'is-affected' : ''} ${dimmed ? 'is-dimmed' : ''}`}
        onClick={() => onSelect(asset.id)}
        aria-pressed={selected}
      >
        <span className="asset-node__topline"><span className="asset-node__kind">{asset.kind}</span><StatusMark health={state.health} compact /></span>
        <strong>{asset.label}</strong>
        <span className="asset-node__message">{state.message}</span>
      </button>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </>
  )
}

const nodeTypes = { asset: AssetNode }

export function TopologyMap({
  assets,
  states,
  selectedId,
  affectedIds = new Set<string>(),
  query = '',
  onSelect,
}: {
  assets: Asset[]
  states: AssetState[]
  selectedId: string
  affectedIds?: Set<string>
  query?: string
  onSelect: (assetId: string) => void
}) {
  const stateById = new Map(states.map((state) => [state.asset_id, state]))
  const { positions, depths } = layoutTopology(assets)
  const normalizedQuery = query.trim().toLowerCase()
  const matches = (asset: Asset) => !normalizedQuery
    || asset.label.toLowerCase().includes(normalizedQuery)
    || asset.id.includes(normalizedQuery)
  const nodes: TopologyNode[] = assets.map((asset) => ({
    id: asset.id,
    type: 'asset',
    position: positions.get(asset.id) ?? { x: 0, y: 0 },
    data: {
      asset,
      state: stateById.get(asset.id) ?? unknownState(asset.id),
      selected: selectedId === asset.id,
      affected: affectedIds.has(asset.id),
      dimmed: !matches(asset),
      onSelect,
    },
    selected: selectedId === asset.id,
    // Read-only console: node positions are derived from dependency depth and are
    // never persisted, so letting them be dragged would just look broken on reload.
    draggable: false,
    connectable: false,
    deletable: false,
    // Seed dimensions so nodes render before the ResizeObserver measures them
    // (the observer never fires in jsdom-based tests).
    initialWidth: NODE_WIDTH,
    initialHeight: NODE_HEIGHT,
    style: { width: NODE_WIDTH },
  }))
  const edges: Edge[] = assets.flatMap((asset) => asset.depends_on.flatMap((dependencyId) => {
    if (!positions.has(dependencyId) || !positions.has(asset.id)) return []
    const highlighted = affectedIds.has(asset.id) && affectedIds.has(dependencyId)
    return [{
      id: `${dependencyId}-${asset.id}`,
      source: dependencyId,
      target: asset.id,
      type: 'default',
      className: highlighted ? 'is-affected' : undefined,
      selectable: false,
      deletable: false,
    }]
  }))
  return (
    <section className="panel topology" aria-labelledby="topology-title">
      <div className="panel__heading">
        <div><p className="eyebrow">Dependency graph</p><h2 id="topology-title">Live service map</h2></div>
        <span className="panel__meta">{assets.length} · read-only</span>
      </div>
      <div className="topology__viewport">
        <div className="topology__flow" role="group" aria-label="Infrastructure dependency map">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            nodesDraggable={false}
            nodesConnectable={false}
            edgesReconnectable={false}
            deleteKeyCode={null}
          />
        </div>
      </div>
      <div className="topology__list" aria-label="Infrastructure assets">
        {assets.filter(matches).map((asset) => {
          const state = stateById.get(asset.id) ?? unknownState(asset.id)
          const style = { '--asset-depth': depths.get(asset.id) ?? 0 } as CSSProperties
          return (
            <button type="button" key={asset.id} style={style} className={selectedId === asset.id ? 'is-selected' : ''} onClick={() => onSelect(asset.id)}>
              <span><strong>{asset.label}</strong><small>{state.message}</small></span>
              <StatusMark health={state.health} />
            </button>
          )
        })}
      </div>
    </section>
  )
}
