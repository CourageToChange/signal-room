import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { assetDetail, bootstrap, diagnostics, event, incident, incidentSummary, maintenance, metrics } from '../test/fixtures'
import { apiSource } from './apiSource'

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('apiSource', () => {
  it('adds idempotency and CSRF headers to same-origin mutations', async () => {
    let headers = new Headers()
    server.use(http.post('*/api/v1/maintenance', ({ request }) => {
      headers = request.headers
      return HttpResponse.json({ maintenance: {
        id: 'window-1', asset_ids: ['asset-1'], starts_at: '2026-07-15T18:00:00Z',
        ends_at: '2026-07-15T19:00:00Z', reason: 'Upgrade',
        created_at: '2026-07-15T17:55:00Z', created_by: 'owner@example.invalid',
        cancelled_at: null, cancelled_by: null, version: 1,
      } })
    }))
    await apiSource.createMaintenance({
      asset_ids: ['asset-1'], starts_at: '2026-07-15T18:00:00Z',
      ends_at: '2026-07-15T19:00:00Z', reason: 'Upgrade',
    }, 'fixed-operation-key')
    expect(headers.get('X-Signal-Room-CSRF')).toBe('1')
    expect(headers.get('Idempotency-Key')).toBe('fixed-operation-key')
  })

  it('validates every stable query and mutation contract', async () => {
    const seen: Request[] = []
    server.use(
      http.get('*/api/v1/bootstrap', () => HttpResponse.json(bootstrap)),
      http.get('*/api/v1/assets/guest', () => HttpResponse.json(assetDetail)),
      http.get('*/api/v1/assets/guest/metrics', () => HttpResponse.json(metrics)),
      http.get('*/api/v1/incidents', () => HttpResponse.json({ items: [incidentSummary], next_cursor: 'next' })),
      http.get('*/api/v1/incidents/incident-1', () => HttpResponse.json(incident)),
      http.get('*/api/v1/incidents/incident-1/timeline', () => HttpResponse.json({ items: [event], next_cursor: null })),
      http.post('*/api/v1/incidents/incident-1/acknowledge', ({ request }) => { seen.push(request); return HttpResponse.json({ incident }) }),
      http.post('*/api/v1/incidents/incident-1/notes', ({ request }) => { seen.push(request); return HttpResponse.json({ incident }) }),
      http.post('*/api/v1/incidents/incident-1/close', ({ request }) => { seen.push(request); return HttpResponse.json({ incident }) }),
      http.get('*/api/v1/maintenance', () => HttpResponse.json([maintenance])),
      http.post('*/api/v1/maintenance', () => HttpResponse.json({ maintenance })),
      http.post('*/api/v1/maintenance/maintenance-1/cancel', ({ request }) => { seen.push(request); return HttpResponse.json({ maintenance }) }),
      http.get('*/api/v1/diagnostics', () => HttpResponse.json(diagnostics)),
    )
    expect((await apiSource.bootstrap()).build_version).toBe('1.0.0')
    expect((await apiSource.asset('guest')).asset.id).toBe('guest')
    expect((await apiSource.metrics('guest', '24h', '1h')).buckets).toHaveLength(2)
    expect((await apiSource.incidents(['open'], 'cursor value')).next_cursor).toBe('next')
    expect((await apiSource.incidents()).items).toHaveLength(1)
    expect((await apiSource.incident('incident-1')).events).toHaveLength(1)
    expect((await apiSource.timeline('incident-1', 4)).items[0].kind).toBe('opened')
    await apiSource.acknowledge('incident-1', 1, 'ack-key')
    await apiSource.addNote('incident-1', 2, 'Checked evidence', 'note-key')
    await apiSource.close('incident-1', 3, 'close-key')
    expect(await apiSource.maintenance()).toHaveLength(1)
    await apiSource.createMaintenance({ asset_ids: ['guest'], starts_at: maintenance.starts_at, ends_at: maintenance.ends_at, reason: maintenance.reason }, 'create-key')
    await apiSource.cancelMaintenance('maintenance-1', 1, 'cancel-key')
    expect((await apiSource.diagnostics()).schema_version).toBe(4)
    expect(seen.map((request) => request.headers.get('If-Match'))).toEqual(['"1"', '"2"', '"3"', '"1"'])
    expect(seen.map((request) => request.headers.get('Idempotency-Key'))).toEqual([
      'ack-key', 'note-key', 'close-key', 'cancel-key',
    ])
  })

  it('parses bounded problem details', async () => {
    server.use(http.get('*/api/v1/bootstrap', () => HttpResponse.json({
      type: 'about:blank', title: 'Forbidden', status: 403,
      detail: 'Access identity was rejected', instance: '/api/v1/bootstrap',
      request_id: 'request-1234',
    }, { status: 403 })))
    await expect(apiSource.bootstrap()).rejects.toThrow('Access identity was rejected')
  })

  it('creates safe fallback problem details for malformed error bodies', async () => {
    server.use(http.get('*/api/v1/bootstrap', () => new HttpResponse('broken', {
      status: 502,
      headers: { 'Content-Type': 'text/plain', 'X-Request-ID': 'request-fallback' },
    })))
    await expect(apiSource.bootstrap()).rejects.toMatchObject({
      problem: { status: 502, request_id: 'request-fallback', detail: 'Request failed (502)' },
    })
    server.use(http.get('*/api/v1/bootstrap', () => new HttpResponse('still broken', { status: 503 })))
    await expect(apiSource.bootstrap()).rejects.toMatchObject({ problem: { request_id: 'unavailable' } })
  })

  it('rejects invalid success payloads at runtime', async () => {
    server.use(http.get('*/api/v1/bootstrap', () => HttpResponse.json({ stale: false })))
    await expect(apiSource.bootstrap()).rejects.toThrow()
  })

  it('tracks EventSource lifecycle, topics, offline state, and cleanup', () => {
    const listeners = new Map<string, () => void>()
    class FakeEventSource {
      static instance: FakeEventSource | undefined
      onopen: (() => void) | null = null
      onerror: (() => void) | null = null
      closed = false
      constructor(readonly url: string) { FakeEventSource.instance = this }
      addEventListener(topic: string, handler: () => void) { listeners.set(topic, handler) }
      close() { this.closed = true }
    }
    vi.stubGlobal('EventSource', FakeEventSource)
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: true })
    const events: string[] = []
    const states: string[] = []
    const close = apiSource.subscribe((topic) => events.push(topic), (state) => states.push(state))
    const source = FakeEventSource.instance
    expect(source?.url).toBe('/api/v1/stream')
    source?.onopen?.()
    listeners.get('incident')?.()
    source?.onerror?.()
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: false })
    source?.onerror?.()
    expect(states).toEqual(['connecting', 'live', 'retrying', 'offline'])
    expect(events).toEqual(['incident'])
    close()
    expect(source?.closed).toBe(true)
    const offlineStates: string[] = []
    const closeOffline = apiSource.subscribe(() => undefined, (state) => offlineStates.push(state))
    expect(offlineStates[0]).toBe('offline')
    closeOffline()
    vi.unstubAllGlobals()
  })
})
