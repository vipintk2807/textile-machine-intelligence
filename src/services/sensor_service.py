"""Sensor definitions, readings and freshness overview."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.anomaly_rules import validate_threshold_config
from src.core.freshness import Freshness, classify_freshness
from src.core.validation import check_identifier
from src.models.orm import Anomaly, SensorDefinition, SensorReading
from src.models.schemas import (
    NotFoundError,
    RuleConfigError,
    SensorRecord,
    SensorSpec,
    Severity,
    ValidationFailed,
    ValidationIssue,
)
from src.utils.time_utils import Window, utc_now


@dataclass(frozen=True)
class SensorOverviewRow:
    definition: SensorDefinition
    latest: SensorReading | None
    freshness: Freshness
    stale_after: timedelta
    active_anomalies: int


class SensorService:
    def __init__(self, session: Session, clock: Callable[[], datetime] = utc_now):
        self.session = session
        self.clock = clock

    # ------------------------------ definitions ------------------------------ #

    def create_definition(
        self,
        machine_id: str,
        sensor_code: str,
        measurement_type: str,
        unit: str | None = None,
        lower_threshold: float | None = None,
        upper_threshold: float | None = None,
        severity: Severity = Severity.WARNING,
        stale_after_seconds: int | None = None,
    ) -> SensorDefinition:
        code, issues = check_identifier(sensor_code, "sensor_code", "REQUIRED")
        if not measurement_type or not measurement_type.strip():
            issues.append(ValidationIssue("REQUIRED", "measurement_type", "Measurement type is required."))
        self._check_thresholds(lower_threshold, upper_threshold, issues)
        if issues:
            raise ValidationFailed(issues)
        existing = self.session.scalar(
            select(SensorDefinition).where(
                SensorDefinition.machine_id == machine_id, SensorDefinition.sensor_code == code
            )
        )
        if existing is not None:
            raise ValidationFailed(
                [ValidationIssue("DUPLICATE", "sensor_code", "This sensor already exists for the machine.")]
            )
        now = self.clock()
        definition = SensorDefinition(
            machine_id=machine_id,
            sensor_code=code,
            measurement_type=measurement_type.strip(),
            unit=(unit.strip() or None) if unit else None,
            lower_threshold=lower_threshold,
            upper_threshold=upper_threshold,
            threshold_severity=Severity(severity).value,
            stale_after_seconds=stale_after_seconds,
            created_at=now,
            updated_at=now,
        )
        self.session.add(definition)
        self.session.flush()
        return definition

    @staticmethod
    def _check_thresholds(lower: float | None, upper: float | None, issues: list) -> None:
        try:
            validate_threshold_config(lower, upper)
        except RuleConfigError as exc:
            issues.append(ValidationIssue("INVALID_THRESHOLD", "thresholds", str(exc)))

    def ensure_definitions(self, machine_id: str, specs: Sequence[dict]) -> list[SensorDefinition]:
        """Create any missing definitions from ``specs`` (idempotent; never overwrites edits)."""
        out = []
        for spec in specs:
            existing = self.session.scalar(
                select(SensorDefinition).where(
                    SensorDefinition.machine_id == machine_id,
                    SensorDefinition.sensor_code == spec["sensor_code"],
                )
            )
            out.append(existing or self.create_definition(machine_id, **spec))
        return out

    def get_definition(self, sensor_id: int) -> SensorDefinition:
        definition = self.session.get(SensorDefinition, sensor_id)
        if definition is None:
            raise NotFoundError(f"Sensor {sensor_id} was not found.")
        return definition

    def definitions(self, machine_id: str | None = None) -> list[SensorDefinition]:
        stmt = select(SensorDefinition).order_by(SensorDefinition.machine_id, SensorDefinition.sensor_code)
        if machine_id:
            stmt = stmt.where(SensorDefinition.machine_id == machine_id)
        return list(self.session.scalars(stmt))

    def update_thresholds(
        self,
        sensor_id: int,
        *,
        lower_threshold: float | None,
        upper_threshold: float | None,
        severity: Severity,
        stale_after_seconds: int | None,
    ) -> SensorDefinition:
        issues: list[ValidationIssue] = []
        self._check_thresholds(lower_threshold, upper_threshold, issues)
        if stale_after_seconds is not None and not (10 <= stale_after_seconds <= 604_800):
            issues.append(
                ValidationIssue("INVALID_STALE_AFTER", "stale_after_seconds", "Must be empty or 10-604800 seconds.")
            )
        if issues:
            raise ValidationFailed(issues)
        definition = self.get_definition(sensor_id)
        definition.lower_threshold = lower_threshold
        definition.upper_threshold = upper_threshold
        definition.threshold_severity = Severity(severity).value
        definition.stale_after_seconds = stale_after_seconds
        definition.updated_at = self.clock()
        self.session.flush()
        return definition

    def sensor_specs(self) -> dict[tuple[str, str], SensorSpec]:
        return {
            (d.machine_id, d.sensor_code): SensorSpec(d.id, d.machine_id, d.sensor_code, d.unit)
            for d in self.session.scalars(select(SensorDefinition))
        }

    # -------------------------------- readings -------------------------------- #

    def latest_event_time(self, sensor_id: int) -> datetime | None:
        return self.session.scalar(
            select(SensorReading.event_time)
            .where(SensorReading.sensor_id == sensor_id)
            .order_by(SensorReading.event_time.desc())
            .limit(1)
        )

    def insert_reading(
        self,
        record: SensorRecord,
        sensor_id: int,
        source_id: int,
        ingested_at: datetime,
        quality: str,
        metadata_json: str,
    ) -> SensorReading:
        reading = SensorReading(
            machine_id=record.machine_id,
            sensor_id=sensor_id,
            value=record.value,
            unit=record.unit,
            event_time=record.event_time,
            ingested_at=ingested_at,
            source_id=source_id,
            source_type=record.source_type.value,
            source_event_id=record.source_event_id,
            quality_status=quality,
            scenario_id=record.scenario_id,
            metadata_json=metadata_json,
        )
        self.session.add(reading)
        self.session.flush()
        return reading

    def latest_reading(self, sensor_id: int) -> SensorReading | None:
        return self.session.scalar(
            select(SensorReading)
            .where(SensorReading.sensor_id == sensor_id)
            .order_by(SensorReading.event_time.desc(), SensorReading.id.desc())
            .limit(1)
        )

    def overview(
        self, now: datetime, default_stale_after: timedelta, machine_id: str | None = None
    ) -> list[SensorOverviewRow]:
        rows = []
        for definition in self.definitions(machine_id):
            latest = self.latest_reading(definition.id)
            stale_after = (
                timedelta(seconds=definition.stale_after_seconds)
                if definition.stale_after_seconds
                else default_stale_after
            )
            freshness = classify_freshness(latest.event_time if latest else None, now, stale_after)
            active = self.session.scalars(
                select(Anomaly.id).where(
                    Anomaly.sensor_id == definition.id, Anomaly.status != "RESOLVED"
                )
            ).all()
            rows.append(SensorOverviewRow(definition, latest, freshness, stale_after, len(active)))
        return rows

    def readings(
        self,
        *,
        sensor_id: int | None = None,
        machine_id: str | None = None,
        window: Window | None = None,
        limit: int | None = 1000,
        newest_first: bool = True,
    ) -> list[SensorReading]:
        stmt = select(SensorReading)
        if sensor_id is not None:
            stmt = stmt.where(SensorReading.sensor_id == sensor_id)
        if machine_id:
            stmt = stmt.where(SensorReading.machine_id == machine_id)
        if window is not None:
            stmt = stmt.where(SensorReading.event_time >= window.start, SensorReading.event_time < window.end)
        order = (SensorReading.event_time.desc(), SensorReading.id.desc()) if newest_first else (
            SensorReading.event_time, SensorReading.id)
        stmt = stmt.order_by(*order)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))
