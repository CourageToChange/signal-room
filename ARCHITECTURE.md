# Architecture

## Trusted process boundary

`signal-room-core` is the sole SQLite owner and incident-engine process. Four clients have
separate runtime identities and method-scoped sockets:

| Role | Can do | Secret ownership |
|---|---|---|
| core | migrations, persistence, incident reconciliation, verified backup | none |
| collector | submit complete provider batches and heartbeat | Proxmox token and PVE CA |
| web | query state and perform versioned responder mutations | Access issuer/AUD/email |
| notifier | read and mark durable notification outbox entries | webhook HMAC secret |
| maintenance | request a verified backup | none |

Production assigns `query.sock`, `ingest.sock`, and `notifier.sock` to distinct non-secret
transport groups; the maintenance socket remains core-only. The runtime directory is
traversable but not writable by clients, every socket is mode `0660`, and the SQLite state
and backup directories are mode `0700` for core. The RPC dispatcher still rejects
wrong-role methods as defence in depth. Production also rejects combined roles, `.env`,
unknown settings, placeholder build values, and secrets supplied to a non-owning process.

## Deterministic incident flow

1. A provider submits one complete, idempotent run with last-attempt and last-success state.
2. Check states and raw observations are persisted in one transaction.
3. Reconciliation selects the worst representative per asset and processes assets in a
   stable topological order, independent of provider response order.
4. Only an ancestor whose own threshold is confirmed can become a correlation root.
5. Late related signals join the active root incident; escalation is monotonic and atomic.
6. Every recurrence gets a new UUID and `previous_incident_id`; resolved incidents are
   immutable and never reuse acknowledgement or recovery timestamps.
7. Maintenance mutes notifications but not telemetry. One fresh post-window signal opens
   a fault that remained confirmed through maintenance.

Incident types are explicit: `monitoring_unavailable`, `asset_down`,
`resource_pressure`, `backup_failed`, `backup_stale`, `http_failed`, and
`certificate_expiring`.

## Storage lifecycle

Ordered SQL migrations run transactionally before core startup. A database with a future
schema or a changed migration checksum is rejected. An existing database receives a
verified pre-migration SQLite backup; a failed migration restores it before services can
start.

SQLite runs with WAL, foreign keys, bounded busy handling, and periodic checkpoints. Raw
samples are retained for 7 days, hourly rollups for 180 days, and incidents/events for
365 days. Removed assets are retired rather than deleted. Daily backups use SQLite's
online backup API, write `.partial`, run `quick_check`, `integrity_check`, and
`foreign_key_check`, fsync, atomically rename, checksum, and retain 14 copies.

## API and live console

The API provides a small bootstrap response, asset detail and bucketed metrics, cursor
incident pages, full timelines, maintenance, safe diagnostics, and idempotent versioned
mutations. Errors are `application/problem+json`; security headers cover success and error
responses. A single core poller fans replayable SSE events to bounded clients.

The React console uses React Router, TanStack Query, Zod validation, keyed cancellable
metrics, explicit live/retrying/offline/stale states, SSE invalidation, and bounded polling
fallback. The topology remains navigable at 50+ assets and on mobile. Metric charts have
labelled series, thresholds, and a table alternative.

## Outbound policy

Probes accept only configured HTTPS URLs on approved ports and public allowlisted hosts.
The collector validates every A and AAAA answer, pins the selected address, ignores proxy
environment variables, bounds concurrency and response bodies, and rejects redirects
unless an explicitly approved same-host HTTPS redirect is configured. Proxmox calls are
GET-only and validate with the copied PVE root CA.

The notifier sends only redacted event payloads. Each event has a stable UUID and
HMAC-SHA256 signature. Fixed retries survive restarts and end in diagnosable dead letter.
The optional webhook and five-minute dead-man URL ship disabled.

## Separate public artifact

`dist-demo` has a different entrypoint and cannot import private API modules. Its scenario
is generated from fictional data and held only in React memory. Build and release scans
reject off-origin networking, Access/PVE identifiers, private address patterns, and a CSP
without `connect-src 'none'`.
