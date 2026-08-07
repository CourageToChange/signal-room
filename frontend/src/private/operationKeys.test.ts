import { describe, expect, it, vi } from 'vitest'
import { OperationKeyStore } from './operationKeys'

describe('OperationKeyStore', () => {
  it('reuses a key for an ambiguous retry and rotates it for changed input', () => {
    const generate = vi.fn()
      .mockReturnValueOnce('operation-key-1')
      .mockReturnValueOnce('operation-key-2')
      .mockReturnValueOnce('operation-key-3')
    const store = new OperationKeyStore(generate)

    expect(store.keyFor('incident:one:note', 'v1:body-a')).toBe('operation-key-1')
    expect(store.keyFor('incident:one:note', 'v1:body-a')).toBe('operation-key-1')
    expect(store.keyFor('incident:one:note', 'v1:body-b')).toBe('operation-key-2')
    store.clear('incident:one:note')
    expect(store.keyFor('incident:one:note', 'v1:body-b')).toBe('operation-key-3')
    expect(generate).toHaveBeenCalledTimes(3)
  })
})
