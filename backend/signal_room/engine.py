from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from .db import Database
from .models import (
    AssetDefinition,
    HealthState,
    IncidentState,
    IncidentType,
    Observation,
    ProviderKind,
    Severity,
    ThresholdConfig,
)


class IncidentEngine:
    """Deterministic, dependency-aware incident reconciliation."""

    def __init__(
        self,
        database: Database,
        assets: list[AssetDefinition],
        thresholds: ThresholdConfig,
        incident_id_factory: Callable[[str, datetime], str] | None = None,
    ) -> None:
        self.database = database
        self.assets = {asset.id: asset for asset in assets}
        self.thresholds = thresholds
        self.incident_id_factory = incident_id_factory or (
            lambda _asset_id, _opened_at: str(uuid4())
        )

    def _ancestors(self, asset_id: str) -> list[AssetDefinition]:
        found: dict[str, tuple[int, AssetDefinition]] = {}

        def walk(current_id: str, distance: int) -> None:
            for dependency in self.assets[current_id].depends_on:
                existing = found.get(dependency)
                if existing is None or distance > existing[0]:
                    found[dependency] = (distance, self.assets[dependency])
                    walk(dependency, distance + 1)

        walk(asset_id, 1)
        return [
            item[1]
            for item in sorted(
                found.values(), key=lambda value: (-value[0], value[1].sort_order, value[1].id)
            )
        ]

    async def _correlation_root(self, asset_id: str, observed_at: datetime) -> AssetDefinition:
        for ancestor in self._ancestors(asset_id):
            state = await self.database.get_state(ancestor.id)
            if state.health == HealthState.HEALTHY:
                continue
            if state.consecutive_failures < self.thresholds.failure_observations:
                continue
            if state.last_observed_at is None:
                continue
            age = abs((observed_at - state.last_observed_at).total_seconds())
            if age <= self.thresholds.correlation_window_seconds:
                return ancestor
        return self.assets[asset_id]

    @staticmethod
    def _severity(observation: Observation) -> Severity:
        if (
            observation.health == HealthState.DOWN
            or observation.details.get("severity") == "critical"
        ):
            return Severity.CRITICAL
        return Severity.WARNING

    @staticmethod
    def _condition(observation: Observation) -> IncidentType:
        if observation.condition is not None:
            return observation.condition
        source = str(observation.details.get("source", ""))
        if observation.health == HealthState.UNKNOWN:
            return IncidentType.MONITORING_UNAVAILABLE
        if source in {"https", "http"}:
            return IncidentType.HTTP_FAILED
        if source == "tls":
            return IncidentType.CERTIFICATE_EXPIRING
        if source in {"proxmox-backup", "backup"}:
            return IncidentType.BACKUP_STALE
        if observation.details.get("condition") == "resource_pressure":
            return IncidentType.RESOURCE_PRESSURE
        return IncidentType.ASSET_DOWN

    @staticmethod
    def _observation_rank(observation: Observation) -> tuple[int, int, str]:
        health = {
            HealthState.HEALTHY: 0,
            HealthState.DEGRADED: 1,
            HealthState.UNKNOWN: 2,
            HealthState.DOWN: 3,
        }[observation.health]
        severity = 1 if observation.details.get("severity") == "critical" else 0
        return health, severity, observation.check_id

    async def process_batch(
        self,
        *,
        provider: ProviderKind,
        run_id: str,
        attempted_at: datetime,
        observations: list[Observation],
        success: bool = True,
        completed_at: datetime | None = None,
        error_code: str | None = None,
        message: str = "",
    ) -> None:
        unknown = {item.asset_id for item in observations} - self.assets.keys()
        if unknown:
            raise KeyError(f"observations reference unknown assets: {sorted(unknown)!r}")
        await self.database.record_provider_batch(
            provider=provider,
            run_id=run_id,
            attempted_at=attempted_at,
            completed_at=completed_at,
            observations=observations,
            success=success,
            error_code=error_code,
            message=message,
        )
        grouped: dict[str, list[Observation]] = {}
        for observation in observations:
            grouped.setdefault(observation.asset_id, []).append(observation)

        # Reconciliation order is topological and stable, never provider response order.
        ordered_assets = sorted(
            grouped,
            key=lambda asset_id: (
                len(self._ancestors(asset_id)),
                self.assets[asset_id].sort_order,
                asset_id,
            ),
        )
        representatives = {
            asset_id: max(values, key=self._observation_rank)
            for asset_id, values in grouped.items()
        }
        for asset_id in ordered_assets:
            await self._reconcile(representatives[asset_id], representatives)

    async def process(self, observation: Observation) -> None:
        await self.process_batch(
            provider=observation.provider,
            run_id=observation.provider_run_id or str(uuid4()),
            attempted_at=observation.observed_at,
            completed_at=observation.observed_at,
            observations=[observation],
        )

    async def _reconcile(
        self,
        observation: Observation,
        batch_representatives: dict[str, Observation],
    ) -> None:
        state = await self.database.get_state(observation.asset_id)
        if state.health == HealthState.HEALTHY:
            await self._handle_recovery(observation, state.consecutive_successes)
            return
        if state.consecutive_failures < self.thresholds.failure_observations:
            return
        if (
            self._condition(observation) == IncidentType.RESOURCE_PRESSURE
            and self._severity(observation) != Severity.CRITICAL
        ):
            if state.unhealthy_since_at is None:
                return
            warning_age = (observation.observed_at - state.unhealthy_since_at).total_seconds()
            if warning_age < self.thresholds.resource_warning_seconds:
                return
        if await self.database.is_maintenance_active(observation.asset_id, observation.observed_at):
            return

        root = await self._correlation_root(observation.asset_id, observation.observed_at)
        root_observation = batch_representatives.get(root.id, observation)
        incident_type = self._condition(root_observation)
        incident = await self.database.find_active_root_incident(root.id)
        if incident is None:
            incident = await self.database.create_incident(
                incident_id=self.incident_id_factory(root.id, observation.observed_at),
                root_asset_id=root.id,
                incident_type=incident_type,
                severity=self._severity(observation),
                title=f"{root.label} requires attention",
                summary=(
                    f"Repeated {incident_type.value.replace('_', ' ')} signals were "
                    "confirmed around "
                    f"{root.label}. Review the shared dependency before treating child signals "
                    "separately."
                ),
                opened_at=observation.observed_at,
                affected_asset_id=observation.asset_id,
            )
        elif incident.state == IncidentState.RECOVERING:
            await self.database.set_incident_state(
                incident.id,
                IncidentState.OPEN,
                at=observation.observed_at,
                message="An unhealthy observation interrupted recovery",
            )
        await self.database.attach_asset(
            incident.id,
            observation.asset_id,
            f"{self.assets[observation.asset_id].label} joined the incident: {observation.message}",
            observation.observed_at,
        )
        await self.database.escalate_incident(
            incident.id,
            self._severity(observation),
            observation.observed_at,
            "Severity escalated after "
            f"{self.assets[observation.asset_id].label} reported a critical signal",
        )

    async def _handle_recovery(self, observation: Observation, successes: int) -> None:
        incidents = await self.database.active_incidents_containing(observation.asset_id)
        for incident in incidents:
            if successes == 1 and incident.state == IncidentState.OPEN:
                await self.database.set_incident_state(
                    incident.id,
                    IncidentState.RECOVERING,
                    at=observation.observed_at,
                    message="Healthy observations have resumed; waiting for confirmation",
                )
            if await self.database.incident_assets_healthy(
                incident.id, self.thresholds.recovery_observations
            ):
                await self.database.set_incident_state(
                    incident.id,
                    IncidentState.RESOLVED,
                    at=observation.observed_at,
                    message="Every correlated asset met the recovery threshold",
                )
