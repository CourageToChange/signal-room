export class OperationKeyStore {
  private readonly entries = new Map<string, { signature: string; key: string }>()

  constructor(private readonly generate: () => string = () => crypto.randomUUID()) {}

  keyFor(operation: string, signature: string): string {
    const existing = this.entries.get(operation)
    if (existing?.signature === signature) return existing.key
    const key = this.generate()
    this.entries.set(operation, { signature, key })
    return key
  }

  clear(operation: string): void {
    this.entries.delete(operation)
  }
}
