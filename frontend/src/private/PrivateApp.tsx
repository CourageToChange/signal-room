import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createContext, FormEvent, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { Link, NavLink, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import pressureDropRaw from '../demo/generated/pressure-drop.json'
import { DrillExperience } from '../DrillExperience'
import { MetricChart } from '../components/MetricChart'
import { StatusMark } from '../components/StatusMark'
import { TopologyMap } from '../components/TopologyMap'
import { ApiProblemError, apiSource, type StreamState } from '../data/apiSource'
import type {
  Bootstrap,
  DrillScenario,
  Incident,
  IncidentSummary,
  MaintenanceWindow,
  MetricsResponse,
} from '../types'
import { OperationKeyStore } from './operationKeys'

const pressureDrop = pressureDropRaw as unknown as DrillScenario
const BootstrapContext = createContext<Bootstrap | null>(null)
const StreamContext = createContext<StreamState>('connecting')

function useBootstrap() {
  const value = useContext(BootstrapContext)
  if (!value) throw new Error('Bootstrap is unavailable')
  return value
}

function Loading({ label = 'Loading operational state…' }: { label?: string }) {
  return <div className="route-state" role="status"><span className="loader" aria-hidden="true" /><p>{label}</p></div>
}

function ErrorState({ error, retry }: { error: unknown; retry: () => void }) {
  const message = error instanceof Error ? error.message : 'The request could not be completed'
  return <div className="route-state route-state--error" role="alert"><strong>Data unavailable</strong><p>{message}</p><button className="button" type="button" onClick={retry}>Try again</button></div>
}

function formatRelative(value: string | null): string {
  if (!value) return 'never'
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000)
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86_400)}d ago`
}

function AppShell() {
  const queryClient = useQueryClient()
  const [stream, setStream] = useState<StreamState>(navigator.onLine ? 'connecting' : 'offline')
  const bootstrap = useQuery({
    queryKey: ['bootstrap'],
    queryFn: ({ signal }) => apiSource.bootstrap(signal),
    refetchInterval: stream === 'live' ? false : 15_000,
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  })

  useEffect(() => {
    const invalidate = (topic: string) => {
      void queryClient.invalidateQueries({ queryKey: ['bootstrap'] })
      if (topic === 'incident') void queryClient.invalidateQueries({ queryKey: ['incidents'] })
      if (topic === 'provider') void queryClient.invalidateQueries({ queryKey: ['diagnostics'] })
      if (topic === 'maintenance') void queryClient.invalidateQueries({ queryKey: ['maintenance'] })
    }
    const unsubscribe = apiSource.subscribe(invalidate, setStream)
    const online = () => { setStream('retrying'); void queryClient.refetchQueries({ type: 'active' }) }
    const offline = () => setStream('offline')
    window.addEventListener('online', online)
    window.addEventListener('offline', offline)
    const visible = () => { if (!document.hidden) void queryClient.refetchQueries({ type: 'active' }) }
    document.addEventListener('visibilitychange', visible)
    return () => {
      unsubscribe()
      window.removeEventListener('online', online)
      window.removeEventListener('offline', offline)
      document.removeEventListener('visibilitychange', visible)
    }
  }, [queryClient])

  if (bootstrap.isPending) return <main id="main" className="loading-screen"><span className="loader" aria-hidden="true" /><h1>Opening Signal Room</h1><p>Connecting to the trusted core…</p></main>
  if (bootstrap.isError || !bootstrap.data) return <main id="main" className="error-screen"><p className="eyebrow">Connection unavailable</p><h1>The console could not open</h1><p>{bootstrap.error?.message}</p><button className="button" type="button" onClick={() => void bootstrap.refetch()}>Try again</button></main>

  const stateLabel = stream === 'offline' ? 'offline' : bootstrap.data.stale ? 'stale' : stream
  return (
    <BootstrapContext.Provider value={bootstrap.data}>
      <StreamContext.Provider value={stream}>
        <div className="private-console">
          <header className="app-header">
            <Link className="brand" to="/" aria-label="Signal Room overview"><span className="brand__mark" aria-hidden="true"><i /><i /><i /></span><span><strong>Signal Room</strong><small>Homelab operations</small></span></Link>
            <nav aria-label="Primary navigation">
              <NavLink to="/" end>Overview</NavLink><NavLink to="/topology">Topology</NavLink><NavLink to="/incidents">Incidents</NavLink><NavLink to="/history">History</NavLink><NavLink to="/maintenance">Maintenance</NavLink><NavLink to="/diagnostics">Diagnostics</NavLink>
            </nav>
            <div className={`live-state live-state--${stateLabel}`} role="status"><i aria-hidden="true" /><span><strong>{stateLabel}</strong><small>{bootstrap.data.stale ? 'Telemetry needs attention' : `SSE ${stream}`}</small></span></div>
          </header>
          {(stream !== 'live' || bootstrap.data.stale) && <div className="connection-warning" role="status">{stream === 'offline' ? 'Network offline. Showing the last verified snapshot.' : bootstrap.data.stale ? 'Telemetry is stale. Decisions should wait for fresh provider data.' : 'Live stream is reconnecting; bounded polling is active.'}</div>}
          <main id="main" tabIndex={-1} className="app-main">
            <Routes>
              <Route path="/" element={<OverviewPage />} />
              <Route path="/topology" element={<TopologyPage />} />
              <Route path="/assets/:assetId" element={<AssetPage />} />
              <Route path="/incidents" element={<IncidentListPage states={['open', 'recovering']} title="Incident inbox" />} />
              <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
              <Route path="/history" element={<IncidentListPage states={['resolved', 'closed']} title="Incident history" />} />
              <Route path="/maintenance" element={<MaintenancePage />} />
              <Route path="/diagnostics" element={<DiagnosticsPage />} />
              <Route path="/drill" element={<DrillExperience scenario={pressureDrop} embedded onExit={() => history.back()} />} />
              <Route path="*" element={<div className="route-state"><strong>Page not found</strong><Link className="button" to="/">Return to overview</Link></div>} />
            </Routes>
          </main>
          <footer><span>Read-only telemetry</span><span>No shell · no service controls · no automated remediation</span></footer>
        </div>
      </StreamContext.Provider>
    </BootstrapContext.Provider>
  )
}

function SummaryCards({ snapshot }: { snapshot: Bootstrap }) {
  const counts = snapshot.states.reduce<Record<string, number>>((result, state) => {
    result[state.health] = (result[state.health] ?? 0) + 1
    return result
  }, {})
  return <section className="overview" aria-label="Current posture">
    <div><span className="overview__value">{snapshot.assets.length}</span><span>Mapped assets</span></div>
    <div><span className="overview__value overview__value--healthy">{counts.healthy ?? 0}</span><span>Healthy</span></div>
    <div><span className="overview__value overview__value--warning">{counts.degraded ?? 0}</span><span>Degraded</span></div>
    <div><span className="overview__value overview__value--danger">{counts.down ?? 0}</span><span>Down</span></div>
    <div><span className="overview__value">{snapshot.incidents.length}</span><span>Active incidents</span></div>
    <div className={snapshot.stale ? 'overview__freshness is-stale' : 'overview__freshness'}><span>{snapshot.stale ? 'Telemetry stale' : 'Collector current'}</span><small>{snapshot.capabilities.data_source} source</small></div>
  </section>
}

function OverviewPage() {
  const snapshot = useBootstrap()
  const navigate = useNavigate()
  const [selected, setSelected] = useState(snapshot.assets[0]?.id ?? '')
  const [query, setQuery] = useState('')
  const affected = new Set(snapshot.incidents.flatMap((incident) => incident.affected_asset_ids))
  const select = (assetId: string) => { setSelected(assetId); void navigate(`/assets/${assetId}`) }
  return <>
    <div className="route-heading"><div><p className="eyebrow">Current posture</p><h1>Operations overview</h1></div><Link className="button" to="/drill">Practice Pressure Drop</Link></div>
    <SummaryCards snapshot={snapshot} />
    <div className="toolbar"><label htmlFor="overview-search">Find an asset</label><input id="overview-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name or asset ID" /></div>
    <div className="overview-layout">
      <TopologyMap assets={snapshot.assets} states={snapshot.states} selectedId={selected} affectedIds={affected} query={query} onSelect={select} />
      <section className="panel queue-card"><div className="panel__heading"><div><p className="eyebrow">Triage queue</p><h2>Active incidents</h2></div><Link to="/incidents">Open inbox</Link></div>{snapshot.incidents.length === 0 ? <div className="empty-state"><span aria-hidden="true">✓</span><strong>No active incidents</strong><p>Repeated failures will appear after confirmation.</p></div> : <IncidentRows incidents={snapshot.incidents} />}</section>
    </div>
    <section className="provider-strip" aria-label="Provider freshness">{snapshot.providers.map((provider) => <div key={provider.provider}><StatusMark health={provider.status === 'healthy' ? 'healthy' : provider.status === 'failed' ? 'down' : 'unknown'} /><span><strong>{provider.provider}</strong><small>{provider.message} · {formatRelative(provider.last_success_at)}</small></span></div>)}</section>
  </>
}

function TopologyPage() {
  const snapshot = useBootstrap()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(snapshot.assets[0]?.id ?? '')
  const affected = new Set(snapshot.incidents.flatMap((incident) => incident.affected_asset_ids))
  return <><div className="route-heading"><div><p className="eyebrow">All dependencies</p><h1>Topology</h1></div></div><div className="toolbar"><label htmlFor="topology-search">Search {snapshot.assets.length} assets</label><input id="topology-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} /></div><TopologyMap assets={snapshot.assets} states={snapshot.states} selectedId={selected} affectedIds={affected} query={query} onSelect={(id) => { setSelected(id); void navigate(`/assets/${id}`) }} /></>
}

function AssetPage() {
  const { assetId = '' } = useParams()
  const [range, setRange] = useState<MetricsResponse['range']>('24h')
  const detail = useQuery({ queryKey: ['asset', assetId], queryFn: ({ signal }) => apiSource.asset(assetId, signal), enabled: Boolean(assetId) })
  const metrics = useQuery({ queryKey: ['metrics', assetId, range], queryFn: ({ signal }) => apiSource.metrics(assetId, range, 'auto', signal), enabled: Boolean(assetId), staleTime: 15_000 })
  if (detail.isPending) return <Loading label="Loading asset detail…" />
  if (detail.isError || !detail.data) return <ErrorState error={detail.error} retry={() => void detail.refetch()} />
  const state = detail.data.state
  return <><div className="route-heading"><div><p className="eyebrow">{detail.data.asset.kind}</p><h1>{detail.data.asset.label}</h1><p>{state.message}</p></div><StatusMark health={state.health} /></div><section className="asset-facts"><div><span>Last observed</span><strong>{formatRelative(state.last_observed_at)}</strong></div><div><span>Dependencies</span><strong>{detail.data.asset.depends_on.length || 'None'}</strong></div><div><span>Checks</span><strong>{detail.data.asset.check_ids.length}</strong></div><div><span>Active incidents</span><strong>{detail.data.active_incidents.length}</strong></div></section>{detail.data.active_incidents.map((incident) => <Link className="incident-callout" key={incident.id} to={`/incidents/${incident.id}`}>{incident.title} · {incident.state}</Link>)}<section className="panel metric-panel"><div className="panel__heading"><div><p className="eyebrow">Telemetry</p><h2>Metric explorer</h2></div><div className="range-tabs" aria-label="Metric range">{(['1h', '24h', '7d', '30d', '180d'] as const).map((value) => <button type="button" aria-pressed={range === value} key={value} onClick={() => setRange(value)}>{value}</button>)}</div></div>{metrics.isPending ? <Loading label="Loading metric buckets…" /> : metrics.isError ? <ErrorState error={metrics.error} retry={() => void metrics.refetch()} /> : metrics.data.buckets.length === 0 ? <div className="empty-state"><strong>No samples in this range</strong><p>The asset is known, but no matching telemetry buckets were retained.</p></div> : <MetricChart data={metrics.data} />}</section></>
}

function IncidentRows({ incidents }: { incidents: IncidentSummary[] }) {
  return <div className="incident-rows">{incidents.map((incident) => <Link key={incident.id} to={`/incidents/${incident.id}`} className={`incident-row severity-${incident.severity}`}><span><strong>{incident.title}</strong><small>{incident.incident_type.replaceAll('_', ' ')} · {incident.affected_asset_ids.length} affected</small></span><span><b>{incident.state}</b><small>{formatRelative(incident.opened_at)}</small></span></Link>)}</div>
}

function IncidentListPage({ states, title }: { states: string[]; title: string }) {
  const [cursor, setCursor] = useState<string | undefined>()
  const query = useQuery({ queryKey: ['incidents', states.join(','), cursor], queryFn: ({ signal }) => apiSource.incidents(states, cursor, signal) })
  return <><div className="route-heading"><div><p className="eyebrow">Evidence-first response</p><h1>{title}</h1></div></div><section className="panel">{query.isPending ? <Loading /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data.items.length === 0 ? <div className="empty-state"><span aria-hidden="true">✓</span><strong>Nothing in this queue</strong><p>Incident transitions are retained for 365 days.</p></div> : <><IncidentRows incidents={query.data.items} />{query.data.next_cursor && <div className="pagination"><button className="button" type="button" onClick={() => setCursor(query.data.next_cursor ?? undefined)}>Next page</button></div>}</>}</section></>
}

function mutationMessage(error: unknown): string {
  if (error instanceof ApiProblemError && error.problem.status === 409) return 'The incident changed while you were viewing it. The latest version has been loaded.'
  return error instanceof Error ? error.message : 'The action could not be completed'
}

function IncidentDetailPage() {
  const { incidentId = '' } = useParams()
  const client = useQueryClient()
  const operationKeys = useRef(new OperationKeyStore()).current
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [feedback, setFeedback] = useState('')
  const incidentQuery = useQuery({ queryKey: ['incident', incidentId], queryFn: ({ signal }) => apiSource.incident(incidentId, signal), enabled: Boolean(incidentId) })
  const timeline = useQuery({ queryKey: ['timeline', incidentId], queryFn: ({ signal }) => apiSource.timeline(incidentId, 0, signal), enabled: Boolean(incidentId) })
  const settle = async (next: Incident, message: string) => {
    client.setQueryData(['incident', incidentId], next)
    setFeedback(message)
    await Promise.all([client.invalidateQueries({ queryKey: ['bootstrap'] }), client.invalidateQueries({ queryKey: ['incidents'] }), client.invalidateQueries({ queryKey: ['timeline', incidentId] })])
  }
  const failed = async (error: unknown, operation: string) => {
    setFeedback(mutationMessage(error))
    if (error instanceof ApiProblemError && error.problem.status === 409) {
      operationKeys.clear(operation)
      await incidentQuery.refetch()
    }
  }
  const acknowledge = useMutation({
    mutationFn: (submitted: Incident) => {
      const operation = `incident:${submitted.id}:acknowledge`
      const key = operationKeys.keyFor(operation, String(submitted.version))
      return apiSource.acknowledge(submitted.id, submitted.version, key)
    },
    onSuccess: ({ incident }, submitted) => {
      operationKeys.clear(`incident:${submitted.id}:acknowledge`)
      void settle(incident, 'Incident acknowledged.')
    },
    onError: (error, submitted) => {
      void failed(error, `incident:${submitted.id}:acknowledge`)
    },
  })
  const note = useMutation({
    mutationFn: ({ incident: submitted, body }: { incident: Incident; body: string }) => {
      const operation = `incident:${submitted.id}:note`
      const signature = JSON.stringify({ version: submitted.version, body })
      const key = operationKeys.keyFor(operation, signature)
      return apiSource.addNote(submitted.id, submitted.version, body, key)
    },
    onSuccess: ({ incident }, submitted) => {
      operationKeys.clear(`incident:${submitted.incident.id}:note`)
      setDrafts((current) => ({ ...current, [incident.id]: '' }))
      void settle(incident, 'Responder note added.')
    },
    onError: (error, submitted) => {
      void failed(error, `incident:${submitted.incident.id}:note`)
    },
  })
  const close = useMutation({
    mutationFn: (submitted: Incident) => {
      const operation = `incident:${submitted.id}:close`
      const key = operationKeys.keyFor(operation, String(submitted.version))
      return apiSource.close(submitted.id, submitted.version, key)
    },
    onSuccess: ({ incident }, submitted) => {
      operationKeys.clear(`incident:${submitted.id}:close`)
      void settle(incident, 'Incident closed.')
    },
    onError: (error, submitted) => {
      void failed(error, `incident:${submitted.id}:close`)
    },
  })
  if (incidentQuery.isPending) return <Loading label="Loading incident evidence…" />
  if (incidentQuery.isError || !incidentQuery.data) return <ErrorState error={incidentQuery.error} retry={() => void incidentQuery.refetch()} />
  const incident = incidentQuery.data
  const busy = acknowledge.isPending || note.isPending || close.isPending
  const draft = drafts[incident.id] ?? ''
  return <><div className="route-heading incident-heading"><div><p className="eyebrow">{incident.incident_type.replaceAll('_', ' ')}</p><h1>{incident.title}</h1><p>{incident.summary}</p></div><span className={`severity-badge severity-${incident.severity}`}>{incident.severity} · {incident.state}</span></div><div className="incident-layout"><section className="panel incident-evidence"><div className="panel__heading"><div><p className="eyebrow">Immutable evidence</p><h2>Timeline</h2></div><span className="panel__meta">v{incident.version}</span></div>{timeline.isPending ? <Loading /> : timeline.isError ? <ErrorState error={timeline.error} retry={() => void timeline.refetch()} /> : <ol className="timeline">{timeline.data.items.map((event) => <li key={event.event_uuid}><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time><span><strong>{event.kind}</strong>{event.message}</span></li>)}</ol>}</section><aside className="panel responder-panel"><div className="panel__heading"><div><p className="eyebrow">Responder controls</p><h2>Coordinate safely</h2></div></div><dl className="incident-meta"><div><dt>Affected</dt><dd>{incident.affected_asset_ids.length} assets</dd></div><div><dt>Opened</dt><dd>{formatRelative(incident.opened_at)}</dd></div><div><dt>Acknowledged</dt><dd>{incident.acknowledged_at ? formatRelative(incident.acknowledged_at) : 'No'}</dd></div></dl>{incident.previous_incident_id && <Link to={`/incidents/${incident.previous_incident_id}`}>View previous recurrence</Link>}<div className="affected-list">{incident.affected_asset_ids.map((id) => <Link key={id} to={`/assets/${id}`}>{id}</Link>)}</div>{incident.runbook && <details className="runbook"><summary>{incident.runbook.title}</summary><p>{incident.runbook.summary}</p><ol>{incident.runbook.checks.map((item) => <li key={item}>{item}</li>)}</ol></details>}{incident.state !== 'resolved' && incident.state !== 'closed' && !incident.acknowledged_at && <button className="button button--primary" type="button" disabled={busy} onClick={() => acknowledge.mutate(incident)}>{acknowledge.isPending ? 'Acknowledging…' : 'Acknowledge incident'}</button>}{incident.state !== 'resolved' && incident.state !== 'closed' && <form className="note-form" onSubmit={(event) => { event.preventDefault(); if (draft.trim()) note.mutate({ incident, body: draft.trim() }) }}><label htmlFor="incident-note">Private responder note</label><textarea id="incident-note" maxLength={2000} value={draft} onChange={(event) => setDrafts((current) => ({ ...current, [incident.id]: event.target.value }))} /><button className="button" type="submit" disabled={busy || !draft.trim()}>{note.isPending ? 'Adding note…' : 'Add note'}</button></form>}{incident.state === 'resolved' && <button className="button" type="button" disabled={busy} onClick={() => { if (window.confirm('Close this resolved incident? Its evidence remains retained.')) close.mutate(incident) }}>{close.isPending ? 'Closing…' : 'Close resolved incident'}</button>}<p className="mutation-feedback" aria-live="polite">{feedback}</p></aside></div></>
}

function localInput(date: Date): string {
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}

function MaintenancePage() {
  const snapshot = useBootstrap()
  const client = useQueryClient()
  const operationKeys = useRef(new OperationKeyStore()).current
  const windows = useQuery({ queryKey: ['maintenance'], queryFn: ({ signal }) => apiSource.maintenance(signal) })
  const now = useMemo(() => new Date(), [])
  const [selected, setSelected] = useState<string[]>([])
  const [startsAt, setStartsAt] = useState(localInput(new Date(now.getTime() + 5 * 60_000)))
  const [endsAt, setEndsAt] = useState(localInput(new Date(now.getTime() + 65 * 60_000)))
  const [reason, setReason] = useState('')
  const [feedback, setFeedback] = useState('')
  const create = useMutation({
    mutationFn: (input: {
      asset_ids: string[]
      starts_at: string
      ends_at: string
      reason: string
    }) => apiSource.createMaintenance(
      input,
      operationKeys.keyFor('maintenance:create', JSON.stringify(input)),
    ),
    onSuccess: () => {
      operationKeys.clear('maintenance:create')
      setFeedback('Maintenance window created. Telemetry will continue and notifications will be muted.')
      setReason('')
      setSelected([])
      void client.invalidateQueries({ queryKey: ['maintenance'] })
    },
    onError: (error) => setFeedback(mutationMessage(error)),
  })
  const cancel = useMutation({
    mutationFn: (item: MaintenanceWindow) => {
      const operation = `maintenance:${item.id}:cancel`
      const key = operationKeys.keyFor(operation, String(item.version))
      return apiSource.cancelMaintenance(item.id, item.version, key)
    },
    onSuccess: (_, item) => {
      operationKeys.clear(`maintenance:${item.id}:cancel`)
      setFeedback('Maintenance window cancelled.')
      void client.invalidateQueries({ queryKey: ['maintenance'] })
    },
    onError: (error, item) => {
      if (error instanceof ApiProblemError && error.problem.status === 409) {
        operationKeys.clear(`maintenance:${item.id}:cancel`)
        void client.invalidateQueries({ queryKey: ['maintenance'] })
        setFeedback('The maintenance window changed. The latest list has been loaded.')
      } else {
        setFeedback(mutationMessage(error))
      }
    },
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (selected.length && reason.trim()) {
      create.mutate({
        asset_ids: selected,
        starts_at: new Date(startsAt).toISOString(),
        ends_at: new Date(endsAt).toISOString(),
        reason: reason.trim(),
      })
    }
  }
  return <><div className="route-heading"><div><p className="eyebrow">Audited suppression</p><h1>Maintenance</h1><p>Windows are asset-scoped, capped at 24 hours, and never pause telemetry.</p></div></div><div className="maintenance-layout"><form className="panel maintenance-form" onSubmit={submit}><div className="panel__heading"><h2>Create window</h2></div><div className="form-body"><fieldset><legend>Assets</legend><div className="asset-checklist">{snapshot.assets.map((asset) => <label key={asset.id}><input type="checkbox" checked={selected.includes(asset.id)} onChange={() => setSelected((current) => current.includes(asset.id) ? current.filter((id) => id !== asset.id) : [...current, asset.id])} />{asset.label}</label>)}</div></fieldset><label>Starts<input type="datetime-local" value={startsAt} onChange={(event) => setStartsAt(event.target.value)} required /></label><label>Ends<input type="datetime-local" value={endsAt} onChange={(event) => setEndsAt(event.target.value)} required /></label><label>Reason<textarea maxLength={240} value={reason} onChange={(event) => setReason(event.target.value)} required /></label><button className="button button--primary" type="submit" disabled={create.isPending || !selected.length || !reason.trim()}>{create.isPending ? 'Creating…' : 'Create maintenance window'}</button><p aria-live="polite">{feedback}</p></div></form><section className="panel"><div className="panel__heading"><h2>Scheduled windows</h2></div>{windows.isPending ? <Loading /> : windows.isError ? <ErrorState error={windows.error} retry={() => void windows.refetch()} /> : windows.data.length === 0 ? <div className="empty-state"><strong>No scheduled maintenance</strong></div> : <div className="maintenance-list">{windows.data.map((item) => <article key={item.id}><strong>{item.reason}</strong><span>{item.asset_ids.length} assets · {new Date(item.starts_at).toLocaleString()}</span><span>{item.cancelled_at ? 'Cancelled' : `Ends ${new Date(item.ends_at).toLocaleString()}`}</span>{!item.cancelled_at && <button className="button" type="button" disabled={cancel.isPending} onClick={() => cancel.mutate(item)}>Cancel</button>}</article>)}</div>}</section></div></>
}

function DiagnosticsPage() {
  const stream = useContext(StreamContext)
  const query = useQuery({ queryKey: ['diagnostics'], queryFn: ({ signal }) => apiSource.diagnostics(signal), refetchInterval: 30_000 })
  if (query.isPending) return <Loading label="Running safe diagnostics…" />
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />
  const data = query.data
  const notificationLabel = data.notifications.enabled
    ? `${data.notifications.pending} pending`
    : `Disabled · ${data.notifications.suppressed} suppressed`
  return <><div className="route-heading"><div><p className="eyebrow">Authenticated diagnostics</p><h1>Diagnostics</h1><p>Request ID {data.request_id}</p></div></div><section className="diagnostic-grid"><div className="panel diagnostic-card"><span>Core database</span><strong>{data.database_ok ? 'Ready' : 'Unavailable'}</strong></div><div className="panel diagnostic-card"><span>Collector</span><strong>{data.collector_fresh ? 'Fresh' : 'Stale'}</strong></div><div className="panel diagnostic-card"><span>Live stream</span><strong>{stream}</strong></div><div className="panel diagnostic-card"><span>Notifications</span><strong>{notificationLabel}</strong><small>{data.notifications.delivered} delivered · {data.notifications.dead_letter} dead letter</small></div></section><section className="panel diagnostics-table"><div className="panel__heading"><h2>Provider state</h2></div><table><thead><tr><th>Provider</th><th>Status</th><th>Last success</th><th>Failures</th></tr></thead><tbody>{data.providers.map((provider) => <tr key={provider.provider}><th>{provider.provider}</th><td>{provider.status}</td><td>{formatRelative(provider.last_success_at)}</td><td>{provider.consecutive_failures}</td></tr>)}</tbody></table></section><p className="build-line">Signal Room {data.build_version} · {data.build_sha} · schema {data.schema_version} · config {data.configuration_revision}</p></>
}

export function PrivateApp() {
  return <AppShell />
}
