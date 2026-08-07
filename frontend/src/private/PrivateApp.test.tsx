import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, apiSource, type StreamState } from '../data/apiSource'
import {
  assetDetail,
  bootstrap,
  diagnostics,
  event,
  incident,
  incidentSummary,
  maintenance,
  metrics,
} from '../test/fixtures'
import type { Incident } from '../types'
import { PrivateApp } from './PrivateApp'

function mockApi(options: {
  bootstrapError?: Error
  bootstrapData?: typeof bootstrap
  metricsError?: Error
  assetError?: Error
  incidentError?: Error
  timelineError?: Error
  incidentsError?: Error
  maintenanceError?: Error
  diagnosticsError?: Error
  streamState?: StreamState
} = {}) {
  let streamState: ((state: StreamState) => void) | undefined
  let streamEvent: ((topic: string) => void) | undefined
  vi.spyOn(apiSource, 'bootstrap').mockImplementation(() => {
    if (options.bootstrapError) return Promise.reject(options.bootstrapError)
    return Promise.resolve(options.bootstrapData ?? bootstrap)
  })
  vi.spyOn(apiSource, 'subscribe').mockImplementation((event, state) => {
    streamEvent = event
    streamState = state
    state(options.streamState ?? 'live')
    return vi.fn()
  })
  vi.spyOn(apiSource, 'asset').mockImplementation(() => options.assetError ? Promise.reject(options.assetError) : Promise.resolve(assetDetail))
  vi.spyOn(apiSource, 'metrics').mockImplementation(() => {
    if (options.metricsError) return Promise.reject(options.metricsError)
    return Promise.resolve(metrics)
  })
  vi.spyOn(apiSource, 'incidents').mockImplementation((states = [], cursor) => {
    if (options.incidentsError) return Promise.reject(options.incidentsError)
    if (states.includes('resolved')) return Promise.resolve({ items: [], next_cursor: null })
    return Promise.resolve({ items: [incidentSummary], next_cursor: cursor ? null : 'page-2' })
  })
  vi.spyOn(apiSource, 'incident').mockImplementation(() => options.incidentError ? Promise.reject(options.incidentError) : Promise.resolve(incident))
  vi.spyOn(apiSource, 'timeline').mockImplementation(() => options.timelineError ? Promise.reject(options.timelineError) : Promise.resolve({ items: [event], next_cursor: null }))
  vi.spyOn(apiSource, 'acknowledge').mockResolvedValue({ incident: { ...incident, acknowledged_at: '2026-07-15T18:01:00Z', acknowledged_by: 'owner@example.invalid', version: 2 } })
  vi.spyOn(apiSource, 'addNote').mockResolvedValue({ incident: { ...incident, version: 2 } })
  vi.spyOn(apiSource, 'close').mockResolvedValue({ incident: { ...incident, state: 'closed', closed_at: '2026-07-15T18:03:00Z', closed_by: 'owner@example.invalid', version: 3 } })
  vi.spyOn(apiSource, 'maintenance').mockImplementation(() => options.maintenanceError ? Promise.reject(options.maintenanceError) : Promise.resolve([maintenance]))
  vi.spyOn(apiSource, 'createMaintenance').mockResolvedValue({ maintenance })
  vi.spyOn(apiSource, 'cancelMaintenance').mockResolvedValue({ maintenance: { ...maintenance, cancelled_at: '2026-07-15T18:10:00Z', cancelled_by: 'owner@example.invalid', version: 2 } })
  vi.spyOn(apiSource, 'diagnostics').mockImplementation(() => options.diagnosticsError ? Promise.reject(options.diagnosticsError) : Promise.resolve(diagnostics))
  return {
    emitStreamState: (state: StreamState) => streamState?.(state),
    emitTopic: (topic: string) => streamEvent?.(topic),
  }
}

function renderRoute(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}><PrivateApp /></MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true })
})

describe('PrivateApp routes', () => {
  it('opens the overview, filters topology, navigates to an asset, and reports stream state', async () => {
    const stream = mockApi()
    renderRoute('/')
    expect(screen.getByRole('heading', { name: 'Opening Signal Room' })).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Operations overview' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Find an asset'), { target: { value: 'Orchid' } })
    fireEvent.click(screen.getAllByRole('button', { name: /Orchid Guest/ })[0])
    expect(await screen.findByRole('heading', { name: 'Orchid Guest' })).toBeInTheDocument()
    act(() => stream.emitStreamState('retrying'))
    expect(screen.getByText(/bounded polling is active/i)).toBeInTheDocument()
    act(() => { window.dispatchEvent(new Event('offline')) })
    expect(screen.getByText(/Network offline/i)).toBeInTheDocument()
    act(() => { window.dispatchEvent(new Event('online')) })
    act(() => {
      stream.emitTopic('incident')
      stream.emitTopic('provider')
      stream.emitTopic('maintenance')
      stream.emitTopic('notification')
    })
  })

  it('makes stale, empty, never-seen, failed, and unknown provider states explicit', async () => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: false })
    mockApi({
      streamState: 'offline',
      bootstrapData: {
        ...bootstrap,
        stale: true,
        collector_last_seen_at: null,
        assets: [],
        states: [],
        incidents: [],
        providers: [
          { ...bootstrap.providers[0], status: 'failed', last_success_at: null, message: 'Collection failed' },
          { ...bootstrap.providers[0], provider: 'tls', status: 'stale', last_success_at: null, message: 'Awaiting first run' },
        ],
      },
    })
    renderRoute('/')
    expect(await screen.findByText('Telemetry stale')).toBeInTheDocument()
    expect(screen.getByText('No active incidents')).toBeInTheDocument()
    expect(screen.getAllByText(/never/)).toHaveLength(2)
    expect(screen.getByText(/Network offline/i)).toBeInTheDocument()
  })

  it('supports the dedicated topology and metric explorer routes', async () => {
    mockApi()
    renderRoute('/topology')
    expect(await screen.findByRole('heading', { name: 'Topology' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Search 2 assets'), { target: { value: 'Atlas' } })
    expect(screen.getAllByRole('button', { name: /Atlas Host/ })).toHaveLength(2)
  })

  it('shows detail, active evidence, keyed ranges, and complete metric alternatives', async () => {
    mockApi()
    renderRoute('/assets/guest')
    expect(await screen.findByRole('heading', { name: 'Orchid Guest' })).toBeInTheDocument()
    expect(screen.getByText('Orchid capacity exhausted · open')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'CPU' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    await waitFor(() => expect(apiSource.metrics).toHaveBeenCalledWith('guest', '7d', 'auto', expect.any(AbortSignal)))
  })

  it('renders asset and metric failures and the empty metric state safely', async () => {
    mockApi({ metricsError: new Error('Metrics unavailable') })
    const view = renderRoute('/assets/guest')
    expect(await screen.findByText('Metrics unavailable')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    view.unmount()
    vi.restoreAllMocks()
    mockApi()
    vi.mocked(apiSource.metrics).mockResolvedValue({ ...metrics, buckets: [] })
    renderRoute('/assets/guest')
    expect(await screen.findByText('No samples in this range')).toBeInTheDocument()
  })

  it('renders query failures for asset detail, incident lists, and incident evidence', async () => {
    mockApi({ assetError: new Error('Asset unavailable') })
    let view = renderRoute('/assets/guest')
    expect(await screen.findByText('Asset unavailable')).toBeInTheDocument()
    view.unmount()
    vi.restoreAllMocks()
    mockApi({ incidentsError: new Error('Queue unavailable') })
    view = renderRoute('/incidents')
    expect(await screen.findByText('Queue unavailable')).toBeInTheDocument()
    view.unmount()
    vi.restoreAllMocks()
    mockApi({ incidentError: new Error('Incident unavailable') })
    view = renderRoute('/incidents/incident-1')
    expect(await screen.findByText('Incident unavailable')).toBeInTheDocument()
    view.unmount()
    vi.restoreAllMocks()
    mockApi({ timelineError: new Error('Timeline unavailable') })
    renderRoute('/incidents/incident-1')
    expect(await screen.findByText('Timeline unavailable')).toBeInTheDocument()
  })

  it('paginates the incident inbox and gives history an explicit empty state', async () => {
    mockApi()
    const view = renderRoute('/incidents')
    expect(await screen.findByRole('heading', { name: 'Incident inbox' })).toBeInTheDocument()
    expect(await screen.findByText('Orchid capacity exhausted')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(apiSource.incidents).toHaveBeenCalledWith(['open', 'recovering'], 'page-2', expect.any(AbortSignal)))
    view.unmount()
    renderRoute('/history')
    expect(await screen.findByText('Nothing in this queue')).toBeInTheDocument()
  })

  it('acknowledges and annotates an incident while retaining its full timeline', async () => {
    mockApi()
    renderRoute('/incidents/incident-1')
    expect(await screen.findByRole('heading', { name: 'Orchid capacity exhausted' })).toBeInTheDocument()
    expect(screen.getByText('View previous recurrence')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge incident' }))
    expect(await screen.findByText('Incident acknowledged.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Private responder note'), { target: { value: '  Evidence preserved  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add note' }))
    expect(await screen.findByText('Responder note added.')).toBeInTheDocument()
    expect(apiSource.addNote).toHaveBeenCalledWith(
      'incident-1',
      expect.any(Number),
      'Evidence preserved',
      expect.any(String),
    )
  })

  it('reuses the same operation key after an ambiguous lost response', async () => {
    mockApi()
    vi.mocked(apiSource.addNote)
      .mockRejectedValueOnce(new TypeError('Network response was lost'))
      .mockResolvedValueOnce({ incident: { ...incident, version: 2 } })
    renderRoute('/incidents/incident-1')
    await screen.findByRole('heading', { name: 'Orchid capacity exhausted' })
    fireEvent.change(screen.getByLabelText('Private responder note'), {
      target: { value: 'Retry-safe evidence' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add note' }))
    expect(await screen.findByText('Network response was lost')).toBeInTheDocument()
    const firstKey = vi.mocked(apiSource.addNote).mock.calls[0][3]

    fireEvent.click(screen.getByRole('button', { name: 'Add note' }))
    expect(await screen.findByText('Responder note added.')).toBeInTheDocument()
    const secondKey = vi.mocked(apiSource.addNote).mock.calls[1][3]
    expect(firstKey).toMatch(/^[0-9a-f-]{36}$/)
    expect(secondKey).toBe(firstKey)
  })

  it('recovers from optimistic conflicts and confirms closure of resolved incidents', async () => {
    mockApi()
    const problem = new ApiProblemError({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'Version changed', instance: '/api/v1/incidents/incident-1', request_id: 'request-1' })
    vi.mocked(apiSource.acknowledge).mockRejectedValue(problem)
    const view = renderRoute('/incidents/incident-1')
    fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge incident' }))
    expect(await screen.findByText(/latest version has been loaded/i)).toBeInTheDocument()
    expect(apiSource.incident).toHaveBeenCalledTimes(2)
    view.unmount()
    vi.restoreAllMocks()
    mockApi()
    const resolved: Incident = { ...incident, state: 'resolved', recovered_at: '2026-07-15T18:02:00Z' }
    vi.mocked(apiSource.incident).mockResolvedValue(resolved)
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderRoute('/incidents/incident-1')
    fireEvent.click(await screen.findByRole('button', { name: 'Close resolved incident' }))
    expect(await screen.findByText('Incident closed.')).toBeInTheDocument()
  })

  it('keeps acknowledged evidence immutable and reports non-problem mutation failures', async () => {
    mockApi()
    vi.mocked(apiSource.incident).mockResolvedValue({
      ...incident,
      previous_incident_id: null,
      acknowledged_at: '2026-07-15T18:01:00Z',
      acknowledged_by: 'owner@example.invalid',
      affected_asset_ids: [],
      runbook: null,
    })
    vi.mocked(apiSource.addNote).mockRejectedValue('unexpected failure')
    renderRoute('/incidents/incident-1')
    expect(await screen.findByText('0 assets')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Acknowledge incident' })).not.toBeInTheDocument()
    expect(screen.queryByText('View previous recurrence')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Private responder note'), { target: { value: 'Preserved' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add note' }))
    expect(await screen.findByText('The action could not be completed')).toBeInTheDocument()
  })

  it('does not close a resolved incident when confirmation is declined', async () => {
    mockApi()
    vi.mocked(apiSource.incident).mockResolvedValue({ ...incident, state: 'resolved', recovered_at: '2026-07-15T18:02:00Z' })
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderRoute('/incidents/incident-1')
    fireEvent.click(await screen.findByRole('button', { name: 'Close resolved incident' }))
    expect(apiSource.close).not.toHaveBeenCalled()
  })

  it('creates and cancels scoped maintenance windows', async () => {
    mockApi()
    renderRoute('/maintenance')
    expect(await screen.findByRole('heading', { name: 'Maintenance' })).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Orchid Guest'))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Guest upgrade' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create maintenance window' }))
    expect(await screen.findByText(/Maintenance window created/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(await screen.findByText('Maintenance window cancelled.')).toBeInTheDocument()
  })

  it('supports removing asset selections and distinguishes empty, cancelled, and failed maintenance', async () => {
    mockApi()
    vi.mocked(apiSource.maintenance).mockResolvedValue([])
    let view = renderRoute('/maintenance')
    expect(await screen.findByText('No scheduled maintenance')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Atlas Host'))
    fireEvent.click(screen.getByLabelText('Atlas Host'))
    view.unmount()
    vi.restoreAllMocks()
    mockApi()
    vi.mocked(apiSource.maintenance).mockResolvedValue([{ ...maintenance, cancelled_at: '2026-07-15T18:20:00Z', cancelled_by: 'owner@example.invalid' }])
    view = renderRoute('/maintenance')
    expect(await screen.findByText('Cancelled')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
    view.unmount()
    vi.restoreAllMocks()
    mockApi({ maintenanceError: new Error('Maintenance unavailable') })
    renderRoute('/maintenance')
    expect(await screen.findByText('Maintenance unavailable')).toBeInTheDocument()
  })

  it('shows authenticated diagnostics and a useful unknown-route response', async () => {
    mockApi()
    const view = renderRoute('/diagnostics')
    expect(await screen.findByRole('heading', { name: 'Diagnostics' })).toBeInTheDocument()
    expect(screen.getByText('2 pending')).toBeInTheDocument()
    expect(screen.getByText(/schema 4/)).toBeInTheDocument()
    view.unmount()
    renderRoute('/does-not-exist')
    expect(await screen.findByText('Page not found')).toBeInTheDocument()
  })

  it('renders degraded and failed diagnostic alternatives', async () => {
    mockApi()
    vi.mocked(apiSource.diagnostics).mockResolvedValue({
      ...diagnostics,
      database_ok: false,
      collector_fresh: false,
      providers: [{ ...diagnostics.providers[0], last_success_at: null, status: 'failed', consecutive_failures: 4 }],
      notifications: {
        enabled: false,
        pending: 0,
        delivered: 4,
        dead_letter: 1,
        suppressed: 3,
        last_success_at: null,
      },
    })
    const view = renderRoute('/diagnostics')
    expect(await screen.findByText('Unavailable')).toBeInTheDocument()
    expect(screen.getByText('Stale')).toBeInTheDocument()
    expect(screen.getByText('Disabled · 3 suppressed')).toBeInTheDocument()
    expect(screen.getByText('4 delivered · 1 dead letter')).toBeInTheDocument()
    expect(screen.getByText('never')).toBeInTheDocument()
    view.unmount()
    vi.restoreAllMocks()
    mockApi({ diagnosticsError: new Error('Diagnostics unavailable') })
    renderRoute('/diagnostics')
    expect(await screen.findByText('Diagnostics unavailable')).toBeInTheDocument()
  })

  it('offers a retry when the trusted core cannot bootstrap', async () => {
    mockApi({ bootstrapError: new Error('Core unavailable') })
    renderRoute('/')
    expect(await screen.findByRole('heading', { name: 'The console could not open' })).toBeInTheDocument()
    expect(screen.getByText('Core unavailable')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(apiSource.bootstrap).toHaveBeenCalledTimes(2))
  })
})
