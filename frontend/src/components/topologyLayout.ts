import type { Asset } from '../types'

export interface Position { x: number; y: number }

export interface TopologyLayout {
  positions: Map<string, Position>
  width: number
  height: number
  depths: Map<string, number>
}

export function getDepth(asset: Asset, byId: Map<string, Asset>, visiting = new Set<string>()): number {
  if (visiting.has(asset.id) || asset.depends_on.length === 0) return 0
  const next = new Set(visiting).add(asset.id)
  return 1 + Math.max(0, ...asset.depends_on.map((id) => {
    const dependency = byId.get(id)
    return dependency ? getDepth(dependency, byId, next) : 0
  }))
}

export function layoutTopology(assets: Asset[]): TopologyLayout {
  const byId = new Map(assets.map((asset) => [asset.id, asset]))
  const groups = new Map<number, Asset[]>()
  const depths = new Map<string, number>()
  for (const asset of assets) {
    const depth = getDepth(asset, byId)
    depths.set(asset.id, depth)
    groups.set(depth, [...(groups.get(depth) ?? []), asset])
  }
  const largestGroup = Math.max(1, ...[...groups.values()].map((group) => group.length))
  const height = Math.max(430, largestGroup * 98 + 44)
  const positions = new Map<string, Position>()
  for (const [depth, group] of groups) {
    const spacing = (height - 70) / Math.max(group.length, 1)
    group
      .sort((left, right) => left.sort_order - right.sort_order || left.id.localeCompare(right.id))
      .forEach((asset, index) => {
        positions.set(asset.id, { x: 36 + depth * 244, y: 28 + index * spacing })
      })
  }
  return {
    positions,
    width: Math.max(620, Math.max(0, ...groups.keys()) * 244 + 270),
    height,
    depths,
  }
}
