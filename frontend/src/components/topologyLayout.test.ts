import { describe, expect, it } from 'vitest'
import type { Asset } from '../types'
import { getDepth, layoutTopology } from './topologyLayout'

const makeAsset = (overrides: Partial<Asset> & { id: string }): Asset => ({
  label: overrides.id,
  kind: 'service',
  depends_on: [],
  parent_id: null,
  check_ids: [],
  runbook_id: null,
  sort_order: 0,
  retired_at: null,
  ...overrides,
})

describe('getDepth', () => {
  it('gives a leaf asset depth 0', () => {
    const leaf = makeAsset({ id: 'leaf' })
    expect(getDepth(leaf, new Map([[leaf.id, leaf]]))).toBe(0)
  })

  it('counts the length of a dependency chain', () => {
    const assets = [
      makeAsset({ id: 'root' }),
      makeAsset({ id: 'middle', depends_on: ['root'] }),
      makeAsset({ id: 'top', depends_on: ['middle'] }),
    ]
    const byId = new Map(assets.map((asset) => [asset.id, asset]))
    expect(getDepth(byId.get('top')!, byId)).toBe(2)
    expect(getDepth(byId.get('middle')!, byId)).toBe(1)
  })

  it('does not hang on dependency cycles', () => {
    const assets = [
      makeAsset({ id: 'alpha', depends_on: ['beta'] }),
      makeAsset({ id: 'beta', depends_on: ['alpha'] }),
    ]
    const byId = new Map(assets.map((asset) => [asset.id, asset]))
    for (const asset of assets) {
      const depth = getDepth(asset, byId)
      expect(Number.isFinite(depth)).toBe(true)
      expect(depth).toBeGreaterThanOrEqual(0)
    }
  })

  it('treats missing dependencies as depth 0', () => {
    const asset = makeAsset({ id: 'lonely', depends_on: ['gone'] })
    expect(getDepth(asset, new Map([[asset.id, asset]]))).toBe(1)
  })
})

describe('layoutTopology', () => {
  it('orders a column by sort_order, then by id', () => {
    const assets = [
      makeAsset({ id: 'zulu', sort_order: 2 }),
      makeAsset({ id: 'bravo', sort_order: 1 }),
      makeAsset({ id: 'alpha', sort_order: 1 }),
    ]
    const { positions, depths } = layoutTopology(assets)
    expect(depths.get('alpha')).toBe(0)
    const yOf = (id: string) => positions.get(id)!.y
    expect(yOf('alpha')).toBeLessThan(yOf('bravo'))
    expect(yOf('bravo')).toBeLessThan(yOf('zulu'))
  })

  it('places deeper assets further right', () => {
    const assets = [
      makeAsset({ id: 'root' }),
      makeAsset({ id: 'child', depends_on: ['root'] }),
    ]
    const { positions } = layoutTopology(assets)
    expect(positions.get('child')!.x).toBeGreaterThan(positions.get('root')!.x)
  })
})
