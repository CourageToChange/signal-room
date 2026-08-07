import { z } from 'zod'
import type {
  AssetDetail,
  Bootstrap,
  Diagnostics,
  Incident,
  IncidentPage,
  MaintenanceWindow,
  MetricsResponse,
  ProblemDetails,
  TimelinePage,
} from '../types'
import {
  assetDetailSchema,
  bootstrapSchema,
  diagnosticsSchema,
  incidentPageSchema,
  incidentSchema,
  maintenanceSchema,
  metricsSchema,
  problemSchema,
  timelinePageSchema,
} from './schemas'

export type StreamState = 'connecting' | 'live' | 'retrying' | 'offline'

export class ApiProblemError extends Error {
  readonly problem: ProblemDetails

  constructor(problem: ProblemDetails) {
    super(problem.detail)
    this.name = 'ApiProblemError'
    this.problem = problem
  }
}

async function request<T>(
  path: string,
  schema: z.ZodType<T>,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...init.headers,
    },
  })
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const parsed = problemSchema.safeParse(payload)
    throw new ApiProblemError(parsed.success ? parsed.data : {
      type: 'about:blank',
      title: 'Request failed',
      status: response.status,
      detail: `Request failed (${response.status})`,
      instance: path,
      request_id: response.headers.get('X-Request-ID') ?? 'unavailable',
    })
  }
  return schema.parse(payload)
}

function mutationHeaders(idempotencyKey: string, version?: number): HeadersInit {
  return {
    'X-Signal-Room-CSRF': '1',
    'Idempotency-Key': idempotencyKey,
    ...(version == null ? {} : { 'If-Match': `"${version}"` }),
  }
}

export const apiSource = {
  bootstrap: (signal?: AbortSignal): Promise<Bootstrap> =>
    request('/api/v1/bootstrap', bootstrapSchema, { signal }),

  asset: (assetId: string, signal?: AbortSignal): Promise<AssetDetail> =>
    request(`/api/v1/assets/${encodeURIComponent(assetId)}`, assetDetailSchema, { signal }),

  metrics: (
    assetId: string,
    range: MetricsResponse['range'] = '1h',
    resolution = 'auto',
    signal?: AbortSignal,
  ): Promise<MetricsResponse> =>
    request(
      `/api/v1/assets/${encodeURIComponent(assetId)}/metrics?range=${range}&resolution=${resolution}`,
      metricsSchema,
      { signal },
    ),

  incidents: (
    states: string[] = [],
    cursor?: string,
    signal?: AbortSignal,
  ): Promise<IncidentPage> => {
    const query = new URLSearchParams()
    states.forEach((state) => query.append('state', state))
    if (cursor) query.set('cursor', cursor)
    return request(`/api/v1/incidents?${query}`, incidentPageSchema, { signal })
  },

  incident: (incidentId: string, signal?: AbortSignal): Promise<Incident> =>
    request(`/api/v1/incidents/${encodeURIComponent(incidentId)}`, incidentSchema, { signal }),

  timeline: (
    incidentId: string,
    cursor = 0,
    signal?: AbortSignal,
  ): Promise<TimelinePage> => request(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/timeline?cursor=${cursor}`,
    timelinePageSchema,
    { signal },
  ),

  acknowledge: (
    incidentId: string,
    version: number,
    idempotencyKey: string,
  ): Promise<{ incident: Incident }> =>
    request(
      `/api/v1/incidents/${encodeURIComponent(incidentId)}/acknowledge`,
      z.object({ incident: incidentSchema }),
      { method: 'POST', body: '{}', headers: mutationHeaders(idempotencyKey, version) },
    ),

  addNote: (
    incidentId: string,
    version: number,
    body: string,
    idempotencyKey: string,
  ): Promise<{ incident: Incident }> =>
    request(
      `/api/v1/incidents/${encodeURIComponent(incidentId)}/notes`,
      z.object({ incident: incidentSchema }),
      {
        method: 'POST',
        body: JSON.stringify({ body }),
        headers: mutationHeaders(idempotencyKey, version),
      },
    ),

  close: (
    incidentId: string,
    version: number,
    idempotencyKey: string,
  ): Promise<{ incident: Incident }> =>
    request(
      `/api/v1/incidents/${encodeURIComponent(incidentId)}/close`,
      z.object({ incident: incidentSchema }),
      { method: 'POST', body: '{}', headers: mutationHeaders(idempotencyKey, version) },
    ),

  maintenance: (signal?: AbortSignal): Promise<MaintenanceWindow[]> =>
    request('/api/v1/maintenance', z.array(maintenanceSchema), { signal }),

  createMaintenance: (input: {
    asset_ids: string[]
    starts_at: string
    ends_at: string
    reason: string
  }, idempotencyKey: string): Promise<{ maintenance: MaintenanceWindow }> => request(
    '/api/v1/maintenance',
    z.object({ maintenance: maintenanceSchema }),
    { method: 'POST', body: JSON.stringify(input), headers: mutationHeaders(idempotencyKey) },
  ),

  cancelMaintenance: (
    maintenanceId: string,
    version: number,
    idempotencyKey: string,
  ): Promise<{ maintenance: MaintenanceWindow }> => request(
    `/api/v1/maintenance/${encodeURIComponent(maintenanceId)}/cancel`,
    z.object({ maintenance: maintenanceSchema }),
    { method: 'POST', body: '{}', headers: mutationHeaders(idempotencyKey, version) },
  ),

  diagnostics: (signal?: AbortSignal): Promise<Diagnostics> =>
    request('/api/v1/diagnostics', diagnosticsSchema, { signal }),

  subscribe(
    onEvent: (topic: string) => void,
    onState: (state: StreamState) => void,
  ): () => void {
    onState(navigator.onLine ? 'connecting' : 'offline')
    const stream = new EventSource('/api/v1/stream')
    const topics = ['snapshot', 'incident', 'provider', 'notification', 'maintenance']
    topics.forEach((topic) => stream.addEventListener(topic, () => onEvent(topic)))
    stream.onopen = () => onState('live')
    stream.onerror = () => onState(navigator.onLine ? 'retrying' : 'offline')
    return () => stream.close()
  },
}
