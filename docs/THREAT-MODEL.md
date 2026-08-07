# Threat model

## Protected assets

- The privilege-separated Proxmox token and inventory it can read.
- Access issuer/audience/email, responder identity, notes, and incident history.
- The webhook HMAC secret and durable notification state.
- SQLite integrity, migration history, backups, configuration, and release identity.
- Availability of monitored homelab services; observation must never become control.

## Trust boundaries

1. The browser reaches loopback web only through the named Cloudflare Tunnel and Access.
2. Web independently verifies current JWT signature, issuer, audience, expiry, and email.
3. Web, collector, notifier, and maintenance cross method-scoped Unix sockets into core.
4. Only core opens SQLite; only the one-shot migration role may open it before core starts.
5. Collector crosses a default-deny egress boundary to exact PVE and approved public probes.
6. Notifier crosses that boundary only for configured signed receivers.
7. The public demo is a separate static artifact and deployment.

## Principal threats and controls

| Threat | Control |
|---|---|
| Forged/stale Access assertion | Origin JWT signature, safe JWKS refresh, exact iss/AUD/email, bounded leeway |
| Origin bypass/host confusion | Loopback binding, default-drop ingress, trusted Host allowlist |
| CSRF or mutation replay | Exact Origin, JSON-only, confirmation header, rate limit, mandatory idempotency and version |
| SSRF/DNS rebinding | Operator allowlist, HTTPS/safe ports, A+AAAA public validation, address pinning, no proxy, bounded body, redirect denial |
| Web compromise reaching PVE/DB | Core-only `0700` state, per-role `0660` transport sockets, no PVE secret, query RPC allowlist |
| Malicious/malformed PVE response | GET-only client, required PVE CA, strict normalization, bounded responses, generic failures |
| Incident/audit tampering | Transactional typed events, immutable resolved incidents, optimistic versions, recurrence UUID |
| Duplicate/out-of-order telemetry | Idempotent provider run IDs, complete batches, stable topological reconciliation |
| Alert storm | Confirmation threshold, persistence window, recovery hysteresis, dependency correlation |
| Backup false-green | Match exact job/guest and retain latest failed/partial attempt separately from latest success |
| Database loss/corruption/full | Sole owner, WAL maintenance, busy/full handling, verified online backups, restore tested into a dedicated throwaway container |
| Migration supply-chain failure | Ordered checksummed SQL, future-schema refusal, pre-backup, rollback before service start |
| Dependency/release compromise | Exact locks, binary offline wheelhouse, universal app wheel, SBOM, manifest, allowlisted bundles |
| Webhook data leak/spoof | Redacted schema, event UUID, HMAC-SHA256, no notes/metrics/credentials/private URLs |
| SSE exhaustion | One broadcaster, replay limit, per-client bounded queue, connection cap, polling fallback |
| Demo privacy leak | Separate entrypoint, no private imports, network-disabled CSP, source/artifact scans |
| Container breakout impact | Unprivileged clean the target container, no devices/nesting/SSH, hardened units, no capabilities, egress firewall |

## Accepted limits

- the target container cannot report a total host, switch, power, or WAN outage; the optional external
  dead-man receiver is the compensating signal.
- The Access JWKS, public probes, notifications, and Tunnel require controlled external
  connectivity.
- Kernel/LXC escape and compromise of Proxmox itself are outside the application boundary.
- No test suite can prove absence of defects. Promotion means the documented objective
  gates passed for the exact build, not that the software is “bulletproof.”
