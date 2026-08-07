# Signal Room — User Guide

A friendly guide to reading your homelab’s health and coordinating an incident safely.

Last updated: 2026-07-20.

> Signal Room is private. Use only the approved link supplied by the operator and do not share it.

## What Signal Room does

Signal Room brings service health, dependencies, backup results, web checks, certificate checks, and incident evidence into one read-only console.

It can help you understand a problem, but it cannot restart a service, open a shell, run a repair, or change the homelab. Use the normal administration tools only after you have checked the evidence and chosen a safe response.

## Opening the console

1. Open your private Signal Room link.
2. Complete the approved sign-in step if one is shown.
3. Wait for **Operations overview** to appear.
4. Check the connection badge in the top-right corner before relying on the data.

There is no unauthenticated public console. The **Pressure Drop** drill uses fictional data and does not expose your homelab.

## Start with the overview

The overview is the quickest place to answer “what needs attention?”

1. Read the cards for mapped, healthy, degraded, and down assets.
2. Check the number of active incidents.
3. Look at the **Triage queue** for confirmed faults.
4. Check **Provider freshness** at the bottom. A provider with no recent success may mean monitoring is unavailable rather than the service itself being down.
5. Use **Find an asset** to search by its visible name or asset ID.

Select an asset in the service map to open its detail page.

## Follow dependencies in Topology

Open **Topology** to see how assets depend on one another.

- Search to narrow a large map.
- Select any card to open that asset.
- Follow the connecting lines toward shared dependencies.
- During an incident, affected assets and the related path are highlighted.
- On a small screen, use the readable hierarchy below the map.

An **Awaiting telemetry** or unknown asset is not automatically healthy or down. It means Signal Room does not yet have enough current evidence.

## Inspect an asset and its metrics

An asset page shows its current message, last observation, dependencies, checks, and active incidents.

1. Open an asset from the overview, topology, or an incident.
2. Follow any incident callout before looking at individual metric spikes in isolation.
3. Choose **1h**, **24h**, **7d**, **30d**, or **180d** in the metric explorer.
4. Read CPU, memory, disk, and latency separately. Warning thresholds appear where configured.
5. Check the completeness percentage. Gaps reduce how confidently you can interpret a trend.
6. Expand **View metric data table** for exact accessible values.

If a range says **No samples in this range**, choose another range or wait for fresh collection. The asset can be known even when no retained samples match the selected period.

## Work an active incident

Open **Incidents** for open and recovering incidents. Open **History** for resolved and closed incidents.

1. Select an incident to read its summary, severity, state, affected assets, and immutable timeline.
2. Follow the affected-asset links and any included runbook checks.
3. Select **Acknowledge incident** when you take ownership. This coordinates responders; it does not change the monitored service.
4. Add a **Private responder note** when another responder needs useful context. Do not paste passwords, tokens, or other secrets.
5. Wait for fresh evidence to show recovery.
6. When an incident is resolved, select **Close resolved incident** and confirm. Closing keeps its evidence in history.

If the same fault returns later, Signal Room creates a new recurrence and may link to the previous incident. It does not reuse the earlier acknowledgement or recovery times.

If the console says an incident changed while you were viewing it, it has loaded the latest version. Review that version before repeating your action.

If an action reports a network or server error after you selected it, leave the incident version and note text unchanged and try the same action again. Signal Room safely recognises that retry instead of creating a duplicate. Change the note only when you intend to submit a different update.

## Schedule maintenance

A maintenance window mutes Signal Room notifications for selected assets. It does not pause monitoring or change those assets.

1. Open **Maintenance**.
2. Select the affected assets.
3. Set the start and end times. A window cannot exceed 24 hours.
4. Enter a short reason that another responder will understand.
5. Select **Create maintenance window**.

Scheduled windows appear alongside the form. Select **Cancel** if a window is no longer needed. If a fault remains after maintenance, one fresh post-maintenance observation can open an incident.

## Check Diagnostics

Open **Diagnostics** when the console looks stale or incomplete.

- **Core database** shows whether the trusted data service is ready.
- **Collector** shows whether monitoring data is fresh.
- **Live stream** shows the current update connection.
- **Notifications** shows whether delivery is enabled, plus pending, delivered, dead-lettered, and deliberately suppressed work. Suppressed notifications were muted while delivery was disabled and will not be sent later as stale alerts.
- **Provider state** shows each provider’s latest success and consecutive failures.

The build, schema, and configuration details at the bottom help identify the exact version during support or a restore test.

## Practice Pressure Drop

From the overview, select **Practice Pressure Drop**.

1. Read the exercise brief and select **Start incident drill**.
2. Watch the evidence develop, or use **Skip to incident**.
3. Pause, play, or change the speed to inspect the timeline.
4. Select assets and incidents as you would in the real console.
5. Complete **Make the call** and submit your decisions.
6. Read the explanations, then select **Run drill again** if you want another pass.

The drill is fictional. It has no analytics or saved practice results, and it does not use private homelab data.

## Connection and freshness messages

- **live** — streaming updates are connected.
- **connecting** — the first live connection is being made.
- **retrying** — the stream is reconnecting; limited polling keeps trying in the background.
- **offline** — the network is unavailable and the last verified snapshot is shown.
- **stale** — provider data is too old for a confident operational decision.

Do not treat stale or unavailable monitoring as proof that a service is down. Check Diagnostics and wait for fresh provider data.

## Trouble?

- If the console cannot open, select **Try again** once. If it still fails, report the visible request ID or error message without sharing secrets.
- If a page says **Data unavailable**, use its **Try again** button after the connection badge recovers.
- If live updates keep retrying, check your network and return to the tab; Signal Room refreshes active views when the tab becomes visible again.
- If you need to restart or repair something, leave Signal Room open for evidence and use the separate, approved administration process. Signal Room intentionally has no repair controls.
