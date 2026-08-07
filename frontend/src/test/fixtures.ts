import type {
  Asset,
  AssetDetail,
  AssetState,
  Bootstrap,
  Diagnostics,
  Incident,
  IncidentEvent,
  IncidentSummary,
  MaintenanceWindow,
  MetricsResponse,
  ProviderState,
} from '../types'

export const now = '2026-07-15T18:00:00Z'

export const assets: Asset[] = [
  { id: 'host', label: 'Atlas Host', kind: 'node', depends_on: [], parent_id: null, check_ids: ['node-health'], runbook_id: 'host', sort_order: 1, retired_at: null },
  { id: 'guest', label: 'Orchid Guest', kind: 'guest', depends_on: ['host'], parent_id: 'host', check_ids: ['guest-health'], runbook_id: 'guest', sort_order: 2, retired_at: null },
]

export const states: AssetState[] = [
  { asset_id: 'host', health: 'healthy', last_observed_at: now, unhealthy_since_at: null, consecutive_failures: 0, consecutive_successes: 3, message: 'Host healthy', latency_ms: null, cpu_ratio: 0.2, memory_ratio: 0.4, disk_ratio: 0.3 },
  { asset_id: 'guest', health: 'down', last_observed_at: now, unhealthy_since_at: '2026-07-15T17:58:00Z', consecutive_failures: 3, consecutive_successes: 0, message: 'Guest unavailable', latency_ms: 5000, cpu_ratio: 0.9, memory_ratio: 0.98, disk_ratio: 0.4 },
]

export const provider: ProviderState = {
  provider: 'proxmox', last_attempt_at: now, last_success_at: now,
  consecutive_failures: 0, status: 'healthy', message: 'Provider batch completed',
}

export const incidentSummary: IncidentSummary = {
  id: 'incident-1', previous_incident_id: 'incident-previous',
  fingerprint: 'guest:resource_pressure', root_asset_id: 'guest',
  incident_type: 'resource_pressure', severity: 'critical', state: 'open', version: 1,
  title: 'Orchid capacity exhausted', summary: 'The shared guest stopped serving dependants.',
  opened_at: '2026-07-15T17:59:00Z', acknowledged_at: null, acknowledged_by: null,
  recovered_at: null, closed_at: null, closed_by: null, affected_asset_ids: ['guest'],
}

export const event: IncidentEvent = {
  id: 1, event_uuid: 'event-1', incident_id: 'incident-1', created_at: now,
  kind: 'opened', message: 'Failure threshold confirmed', actor_subject: null,
  actor_email: null, metadata: {},
}

export const incident: Incident = {
  ...incidentSummary,
  events: [event],
  notes: [],
  runbook: { title: 'Guest pressure', summary: 'Preserve evidence.', checks: ['Inspect memory', 'Check recent changes'] },
}

export const bootstrap: Bootstrap = {
  build_version: '1.0.0', build_sha: 'abcdef12', generated_at: now,
  collector_last_seen_at: now, stale: false, assets, states, providers: [provider],
  incidents: [incidentSummary],
  capabilities: { can_mutate: true, drill_available: true, data_source: 'live' },
  last_event_id: 1,
}

export const metrics: MetricsResponse = {
  asset_id: 'guest', range: '24h', resolution: '1h', generated_at: now, completeness: 0.75,
  thresholds: { cpu_warning_ratio: 0.9, memory_warning_ratio: 0.9, memory_critical_ratio: 0.97, disk_warning_ratio: 0.85, disk_critical_ratio: 0.95 },
  buckets: [
    { started_at: '2026-07-15T16:00:00Z', ended_at: '2026-07-15T17:00:00Z', sample_count: 3, expected_samples: 4, completeness: 0.75, health: 'degraded', cpu_ratio: 0.5, memory_ratio: 0.8, disk_ratio: null, latency_ms: 30 },
    { started_at: '2026-07-15T17:00:00Z', ended_at: now, sample_count: 4, expected_samples: 4, completeness: 1, health: 'down', cpu_ratio: 0.9, memory_ratio: 0.98, disk_ratio: 0.4, latency_ms: 5000 },
  ],
}

export const assetDetail: AssetDetail = {
  asset: assets[1], state: states[1], active_incidents: [incidentSummary],
}

export const maintenance: MaintenanceWindow = {
  id: 'maintenance-1', asset_ids: ['guest'], starts_at: '2026-07-15T19:00:00Z',
  ends_at: '2026-07-15T20:00:00Z', reason: 'Planned upgrade', created_at: now,
  created_by: 'owner@example.invalid', cancelled_at: null, cancelled_by: null, version: 1,
}

export const diagnostics: Diagnostics = {
  request_id: 'request-1234', build_version: '1.0.0', build_sha: 'abcdef12',
  schema_version: 4, configuration_revision: 'test-v2', database_ok: true,
  collector_fresh: true, providers: [provider],
  notifications: {
    enabled: true,
    pending: 2,
    delivered: 4,
    dead_letter: 0,
    suppressed: 0,
    last_success_at: now,
  },
}
