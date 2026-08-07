export type AssetKind = 'node' | 'guest' | 'storage' | 'service' | 'external'
export type HealthState = 'healthy' | 'degraded' | 'down' | 'unknown'
export type IncidentState = 'open' | 'recovering' | 'resolved' | 'closed'
export type IncidentType =
  | 'monitoring_unavailable'
  | 'asset_down'
  | 'resource_pressure'
  | 'backup_failed'
  | 'backup_stale'
  | 'http_failed'
  | 'certificate_expiring'
export type Severity = 'info' | 'warning' | 'critical'
export type ProviderKind = 'fixture' | 'proxmox' | 'backup' | 'https' | 'tls'

export interface Asset {
  id: string
  label: string
  kind: AssetKind
  depends_on: string[]
  parent_id: string | null
  check_ids: string[]
  runbook_id: string | null
  sort_order: number
  retired_at: string | null
}

export interface AssetState {
  asset_id: string
  health: HealthState
  last_observed_at: string | null
  unhealthy_since_at: string | null
  consecutive_failures: number
  consecutive_successes: number
  message: string
  latency_ms: number | null
  cpu_ratio: number | null
  memory_ratio: number | null
  disk_ratio: number | null
}

export interface ProviderState {
  provider: ProviderKind
  last_attempt_at: string | null
  last_success_at: string | null
  consecutive_failures: number
  status: 'never' | 'healthy' | 'failed' | 'stale'
  message: string
}

export interface Observation {
  asset_id: string
  check_id?: string
  provider?: ProviderKind
  provider_run_id?: string | null
  observed_at: string
  health: HealthState
  condition?: IncidentType | null
  message: string
  latency_ms: number | null
  cpu_ratio: number | null
  memory_ratio: number | null
  disk_ratio: number | null
  details: Record<string, unknown>
}

export interface Runbook {
  title: string
  summary: string
  checks: string[]
}

export interface IncidentEvent {
  id: number
  event_uuid: string
  incident_id: string
  created_at: string
  kind: string
  message: string
  actor_subject: string | null
  actor_email: string | null
  metadata: Record<string, unknown>
}

export interface IncidentNote {
  id: number
  incident_id: string
  created_at: string
  author: string
  body: string
}

export interface IncidentSummary {
  id: string
  previous_incident_id: string | null
  fingerprint: string
  root_asset_id: string
  incident_type: IncidentType
  severity: Severity
  state: IncidentState
  version: number
  title: string
  summary: string
  opened_at: string
  acknowledged_at: string | null
  acknowledged_by: string | null
  recovered_at: string | null
  closed_at: string | null
  closed_by: string | null
  affected_asset_ids: string[]
}

export interface Incident extends IncidentSummary {
  events: IncidentEvent[]
  notes: IncidentNote[]
  runbook: Runbook | null
}

export interface Bootstrap {
  build_version: string
  build_sha: string
  generated_at: string
  collector_last_seen_at: string | null
  stale: boolean
  assets: Asset[]
  states: AssetState[]
  providers: ProviderState[]
  incidents: IncidentSummary[]
  capabilities: {
    can_mutate: boolean
    drill_available: boolean
    data_source: 'fixture' | 'live'
  }
  last_event_id: number
}

export interface AssetDetail {
  asset: Asset
  state: AssetState
  active_incidents: IncidentSummary[]
}

export interface MetricBucket {
  started_at: string
  ended_at: string
  sample_count: number
  expected_samples: number
  completeness: number
  health: HealthState
  cpu_ratio: number | null
  memory_ratio: number | null
  disk_ratio: number | null
  latency_ms: number | null
}

export interface MetricsResponse {
  asset_id: string
  range: '1h' | '24h' | '7d' | '30d' | '180d'
  resolution: 'raw' | '5m' | '1h' | '1d'
  generated_at: string
  completeness: number
  thresholds: {
    cpu_warning_ratio: number
    memory_warning_ratio: number
    memory_critical_ratio: number
    disk_warning_ratio: number
    disk_critical_ratio: number
  }
  buckets: MetricBucket[]
}

export interface IncidentPage {
  items: IncidentSummary[]
  next_cursor: string | null
}

export interface TimelinePage {
  items: IncidentEvent[]
  next_cursor: string | null
}

export interface MaintenanceWindow {
  id: string
  asset_ids: string[]
  starts_at: string
  ends_at: string
  reason: string
  created_at: string
  created_by: string
  cancelled_at: string | null
  cancelled_by: string | null
  version: number
}

export interface Diagnostics {
  request_id: string
  build_version: string
  build_sha: string
  schema_version: number
  configuration_revision: string
  database_ok: boolean
  collector_fresh: boolean
  providers: ProviderState[]
  notifications: {
    enabled: boolean
    pending: number
    delivered: number
    dead_letter: number
    suppressed: number
    last_success_at: string | null
  }
}

export interface ProblemDetails {
  type: string
  title: string
  status: number
  detail: string
  instance: string
  request_id: string
}

export interface DrillQuestion {
  id: string
  prompt: string
  options: string[]
  answer: string
  explanation: string
}

export type DrillSnapshot = Omit<Bootstrap, 'incidents'> & { incidents: Incident[] }

export interface DrillScenario {
  version: 1
  slug: string
  title: string
  summary: string
  duration_seconds: number
  frames: Array<{ at_seconds: number; snapshot: DrillSnapshot }>
  questions: DrillQuestion[]
}
