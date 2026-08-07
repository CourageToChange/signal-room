import { z } from 'zod'

const nullableNumber = z.number().nullable()
const nullableDate = z.string().datetime({ offset: true }).nullable()
const health = z.enum(['healthy', 'degraded', 'down', 'unknown'])
const incidentState = z.enum(['open', 'recovering', 'resolved', 'closed'])
const incidentType = z.enum([
  'monitoring_unavailable',
  'asset_down',
  'resource_pressure',
  'backup_failed',
  'backup_stale',
  'http_failed',
  'certificate_expiring',
])

export const assetSchema = z.object({
  id: z.string(),
  label: z.string(),
  kind: z.enum(['node', 'guest', 'storage', 'service', 'external']),
  depends_on: z.array(z.string()),
  parent_id: z.string().nullable(),
  check_ids: z.array(z.string()),
  runbook_id: z.string().nullable(),
  sort_order: z.number().int(),
  retired_at: nullableDate,
})

export const assetStateSchema = z.object({
  asset_id: z.string(),
  health,
  last_observed_at: nullableDate,
  unhealthy_since_at: nullableDate,
  consecutive_failures: z.number().int().nonnegative(),
  consecutive_successes: z.number().int().nonnegative(),
  message: z.string(),
  latency_ms: nullableNumber,
  cpu_ratio: nullableNumber,
  memory_ratio: nullableNumber,
  disk_ratio: nullableNumber,
})

export const providerStateSchema = z.object({
  provider: z.enum(['fixture', 'proxmox', 'backup', 'https', 'tls']),
  last_attempt_at: nullableDate,
  last_success_at: nullableDate,
  consecutive_failures: z.number().int().nonnegative(),
  status: z.enum(['never', 'healthy', 'failed', 'stale']),
  message: z.string(),
})

export const incidentSummarySchema = z.object({
  id: z.string(),
  previous_incident_id: z.string().nullable(),
  fingerprint: z.string(),
  root_asset_id: z.string(),
  incident_type: incidentType,
  severity: z.enum(['info', 'warning', 'critical']),
  state: incidentState,
  version: z.number().int().positive(),
  title: z.string(),
  summary: z.string(),
  opened_at: z.string().datetime({ offset: true }),
  acknowledged_at: nullableDate,
  acknowledged_by: z.string().nullable(),
  recovered_at: nullableDate,
  closed_at: nullableDate,
  closed_by: z.string().nullable(),
  affected_asset_ids: z.array(z.string()),
})

export const incidentEventSchema = z.object({
  id: z.number().int().nonnegative(),
  event_uuid: z.string(),
  incident_id: z.string(),
  created_at: z.string().datetime({ offset: true }),
  kind: z.string(),
  message: z.string(),
  actor_subject: z.string().nullable(),
  actor_email: z.string().nullable(),
  metadata: z.record(z.string(), z.unknown()),
})

export const incidentSchema = incidentSummarySchema.extend({
  events: z.array(incidentEventSchema),
  notes: z.array(z.object({
    id: z.number().int(),
    incident_id: z.string(),
    created_at: z.string().datetime({ offset: true }),
    author: z.string(),
    body: z.string(),
  })),
  runbook: z.object({
    title: z.string(),
    summary: z.string(),
    checks: z.array(z.string()),
  }).nullable(),
})

export const bootstrapSchema = z.object({
  build_version: z.string(),
  build_sha: z.string(),
  generated_at: z.string().datetime({ offset: true }),
  collector_last_seen_at: nullableDate,
  stale: z.boolean(),
  assets: z.array(assetSchema),
  states: z.array(assetStateSchema),
  providers: z.array(providerStateSchema),
  incidents: z.array(incidentSummarySchema),
  capabilities: z.object({
    can_mutate: z.boolean(),
    drill_available: z.boolean(),
    data_source: z.enum(['fixture', 'live']),
  }),
  last_event_id: z.number().int().nonnegative(),
})

export const assetDetailSchema = z.object({
  asset: assetSchema,
  state: assetStateSchema,
  active_incidents: z.array(incidentSummarySchema),
})

export const metricsSchema = z.object({
  asset_id: z.string(),
  range: z.enum(['1h', '24h', '7d', '30d', '180d']),
  resolution: z.enum(['raw', '5m', '1h', '1d']),
  generated_at: z.string().datetime({ offset: true }),
  completeness: z.number().min(0).max(1),
  thresholds: z.object({
    cpu_warning_ratio: z.number(),
    memory_warning_ratio: z.number(),
    memory_critical_ratio: z.number(),
    disk_warning_ratio: z.number(),
    disk_critical_ratio: z.number(),
  }),
  buckets: z.array(z.object({
    started_at: z.string().datetime({ offset: true }),
    ended_at: z.string().datetime({ offset: true }),
    sample_count: z.number().int().nonnegative(),
    expected_samples: z.number().int().positive(),
    completeness: z.number().min(0).max(1),
    health,
    cpu_ratio: nullableNumber,
    memory_ratio: nullableNumber,
    disk_ratio: nullableNumber,
    latency_ms: nullableNumber,
  })),
})

export const incidentPageSchema = z.object({
  items: z.array(incidentSummarySchema),
  next_cursor: z.string().nullable(),
})

export const timelinePageSchema = z.object({
  items: z.array(incidentEventSchema),
  next_cursor: z.string().nullable(),
})

export const maintenanceSchema = z.object({
  id: z.string(),
  asset_ids: z.array(z.string()),
  starts_at: z.string().datetime({ offset: true }),
  ends_at: z.string().datetime({ offset: true }),
  reason: z.string(),
  created_at: z.string().datetime({ offset: true }),
  created_by: z.string(),
  cancelled_at: nullableDate,
  cancelled_by: z.string().nullable(),
  version: z.number().int().positive(),
})

export const diagnosticsSchema = z.object({
  request_id: z.string(),
  build_version: z.string(),
  build_sha: z.string(),
  schema_version: z.number().int(),
  configuration_revision: z.string(),
  database_ok: z.boolean(),
  collector_fresh: z.boolean(),
  providers: z.array(providerStateSchema),
  notifications: z.object({
    enabled: z.boolean(),
    pending: z.number().int().nonnegative(),
    delivered: z.number().int().nonnegative(),
    dead_letter: z.number().int().nonnegative(),
    suppressed: z.number().int().nonnegative(),
    last_success_at: nullableDate,
  }),
})

export const problemSchema = z.object({
  type: z.string(),
  title: z.string(),
  status: z.number().int(),
  detail: z.string(),
  instance: z.string(),
  request_id: z.string(),
})
