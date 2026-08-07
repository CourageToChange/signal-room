# Security Policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability that could expose credentials,
private topology, or deployment details. Contact the repository owner privately with the
affected build SHA, a secret-free reproduction, expected and observed behaviour, and
likely impact.

## Supported version

Only an exact tagged build that has passed every objective gate in the private
release runbook is supported. A release candidate is internal, and after promotion
only the latest exact production tag is supported.

## Non-negotiable invariants

- Only `signal-room-core` opens SQLite; no web, collector, or notifier fallback may do so.
- Use only `signal-room@pve!<token-id>`, privilege-separated with effective `PVEAuditor`.
- Never supply a root password, root token, write-capable token, or disabled TLS validation.
- Never add remote control, shell execution, SSH, Docker access, or auto-remediation.
- The private origin stays loopback-only and independently validates Cloudflare Access JWTs.
- The collector secret is unreadable by web; the webhook secret is unreadable by core,
  collector, and web.
- Probe targets remain operator-configured, public, allowlisted HTTPS destinations.
- The public demo is built and verified as a separate, network-isolated artifact.
- a separate backup container and all existing backup archives/jobs are outside Signal Room's change boundary.

The intended verification baseline is OWASP ASVS 5.0 Level 2 where applicable. Passing
tests reduces known risk; it is not a claim that the software is bug-free or bulletproof.
