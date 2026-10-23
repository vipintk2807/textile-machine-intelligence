"""Ingestion pipeline: Adapter -> validation -> normalisation -> duplicate and
ordering checks -> services -> SQLite.

Batch policy (documented)
-------------------------
* Valid records are accepted; invalid records are *quarantined* in
  ``rejected_records`` with their diagnosis (partial acceptance). Rejections are
  never hidden and never used in calculations.
* A database failure aborts the whole batch: the surrounding transaction is rolled
  back by ``Database.session_scope`` and no success is reported.
* A source error is reported as such (``CycleReport.status == "SOURCE_ERROR"``),
  recorded as source health, and is never turned into an empty result or into
  fabricated records. Gaps are preserved.
* Duplicates are detected only through ``(source, source_event_id)``. Equal values
  without an event id are *not* treated as duplicates.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.anomaly_rules import (
    evaluate_sensor_staleness,
    evaluate_state_duration,
    evaluate_threshold,
    out_of_order_hit,
    rejection_hit,
    source_unavailable_hit,
)
from src.core.settings_rules import RuntimeSettings
from src.core.validation import ValidationContext, validate_record
from src.data_sources.base import SourceAdapter
from src.models.orm import (
    DataSource,
    Machine,
    MachineStateEvent,
    RejectedRecord,
    SensorReading,
    SourceHealthEvent,
)
from src.models.schemas import (
    HealthRecord,
    MachineStatus,
    QualityStatus,
    RawRecord,
    RuleCode,
    SensorRecord,
    Severity,
    SourceError,
    SourceHealth,
    SourceType,
    StateRecord,
    ValidationOutcome,
)
from src.services.anomaly_service import AnomalyService
from src.services.sensor_service import SensorService
from src.services.state_service import StateService
from src.utils.time_utils import utc_now


@dataclass
class BatchReport:
    accepted_state: int = 0
    accepted_sensor: int = 0
    accepted_health: int = 0
    duplicates: int = 0
    rejected: int = 0
    out_of_order: int = 0
    anomalies_created: int = 0
    anomalies_updated: int = 0
    rejection_codes: Counter = field(default_factory=Counter)

    @property
    def accepted(self) -> int:
        return self.accepted_state + self.accepted_sensor + self.accepted_health


@dataclass
class CycleReport:
    status: str  # "OK" | "EMPTY" | "SOURCE_ERROR"
    source_health: SourceHealth
    message: str
    batch: BatchReport | None = None
    error: SourceError | None = None


class IngestionService:
    def __init__(
        self,
        session: Session,
        source: DataSource,
        settings: RuntimeSettings,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.session = session
        self.source = source
        self.settings = settings
        self.clock = clock
        self.states = StateService(session, clock)
        self.sensors = SensorService(session, clock)
        self.anomalies = AnomalyService(session, clock)
        self._source_type = SourceType(source.source_type)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run_cycle(self, adapter: SourceAdapter, now: datetime) -> CycleReport:
        """Read one batch from ``adapter`` and ingest it."""
        result = adapter.read_records(now)
        if result.error is not None:
            report = BatchReport()
            self._record_health(SourceHealth.UNAVAILABLE, now, result.error.message)
            self._raise_connectivity(now, result.error.message, report)
            return CycleReport(
                "SOURCE_ERROR",
                SourceHealth.UNAVAILABLE,
                f"Source error: {result.error.message} Machine states are not inferred from the outage.",
                batch=report,
                error=result.error,
            )
        batch = self.ingest_batch(result.records, now)
        health = SourceHealth.DEGRADED if batch.rejected else SourceHealth.HEALTHY
        detail = f"{batch.rejected} record(s) failed validation." if batch.rejected else None
        self._record_health(health, now, detail)
        if health is not SourceHealth.UNAVAILABLE:
            self.evaluate_periodic(now, batch)
        if not result.records:
            return CycleReport("EMPTY", health, "The source responded with no new records (valid empty result).", batch)
        return CycleReport(
            "OK",
            health,
            f"Accepted {batch.accepted} record(s); {batch.duplicates} duplicate(s) ignored; "
            f"{batch.rejected} rejected; {batch.out_of_order} out of order.",
            batch,
        )

    def ingest_batch(self, records: Sequence[RawRecord], now: datetime) -> BatchReport:
        report = BatchReport()
        ctx = self._context(now)
        for raw in records:
            outcome = validate_record(raw, ctx)
            if not outcome.accepted:
                self._quarantine(raw, outcome, now, report)
                continue
            record = outcome.record
            if isinstance(record, StateRecord):
                self._store_state(record, now, report)
            elif isinstance(record, SensorRecord):
                self._store_sensor(record, ctx, now, report)
            elif isinstance(record, HealthRecord):
                self._store_health(record, now, report)
        return report

    # ------------------------------------------------------------------ #
    # Context
    # ------------------------------------------------------------------ #

    def _context(self, now: datetime) -> ValidationContext:
        machines = {
            m.machine_id: m.enabled
            for m in self.session.scalars(select(Machine).where(Machine.source_id == self.source.id))
        }
        return ValidationContext(
            expected_source_type=self._source_type,
            machines=machines,
            sensors=self.sensors.sensor_specs(),
            now=now,
        )

    def _event_exists(self, model, source_event_id: str | None) -> bool:
        if source_event_id is None:
            return False
        return (
            self.session.scalar(
                select(model.id).where(
                    model.source_id == self.source.id, model.source_event_id == source_event_id
                ).limit(1)
            )
            is not None
        )

    def _count_anomaly(self, created: bool, report: BatchReport) -> None:
        if created:
            report.anomalies_created += 1
        else:
            report.anomalies_updated += 1

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #

    def _quarantine(
        self, raw: RawRecord, outcome: ValidationOutcome, now: datetime, report: BatchReport
    ) -> None:
        payload = raw.payload if hasattr(raw.payload, "get") else {}
        event_id = payload.get("source_event_id") if hasattr(payload, "get") else None
        event_id = event_id if isinstance(event_id, str) and 0 < len(event_id) <= 128 else None
        codes = [i.code for i in outcome.issues]
        if self._event_exists(RejectedRecord, event_id):
            report.duplicates += 1
            return
        self.session.add(
            RejectedRecord(
                source_id=self.source.id,
                source_type=self._source_type.value,
                record_type=str(raw.record_type)[:32],
                machine_ref=(outcome.machine_ref or None) and outcome.machine_ref[:128],
                raw_payload=json.dumps(dict(payload) if payload else {}, default=str)[:4000],
                issues_json=json.dumps(
                    [{"code": i.code, "field": i.field, "message": i.message} for i in outcome.issues]
                ),
                source_event_id=event_id,
                scenario_id=payload.get("scenario_id") if isinstance(payload.get("scenario_id"), str) else None,
                received_at=now,
            )
        )
        self.session.flush()
        report.rejected += 1
        report.rejection_codes.update(codes)
        machine = self.session.get(Machine, outcome.machine_ref) if outcome.machine_ref else None
        if machine is not None and machine.enabled and machine.source_id == self.source.id:
            hit = rejection_hit(codes, now, self.settings.severity_for(RuleCode.RECORD_REJECTED.value))
            _, created = self.anomalies.raise_hit(
                machine_id=machine.machine_id, hit=hit, source_type=self._source_type.value,
                evidence={"rejected_codes": sorted(set(codes))}, now=now,
            )
            self._count_anomaly(created, report)

    def _store_state(self, record: StateRecord, now: datetime, report: BatchReport) -> None:
        if self._event_exists(MachineStateEvent, record.source_event_id):
            report.duplicates += 1
            return
        latest = self.states.latest_event_time(record.machine_id)
        quality = (
            QualityStatus.DELAYED if latest is not None and record.event_time < latest else QualityStatus.VALID
        )
        result = self.states.insert_observation(record, self.source.id, now, quality.value)
        report.accepted_state += 1
        if result.out_of_order and latest is not None:
            report.out_of_order += 1
            hit = out_of_order_hit(
                "machine-state", record.event_time, latest,
                self.settings.severity_for(RuleCode.RECORD_OUT_OF_ORDER.value),
            )
            _, created = self.anomalies.raise_hit(
                machine_id=record.machine_id, hit=hit, source_type=record.source_type.value,
                scenario_id=record.scenario_id,
                evidence={"state_event_id": result.event.id}, now=now,
            )
            self._count_anomaly(created, report)

    def _store_sensor(
        self, record: SensorRecord, ctx: ValidationContext, now: datetime, report: BatchReport
    ) -> None:
        if self._event_exists(SensorReading, record.source_event_id):
            report.duplicates += 1
            return
        spec = ctx.sensors[(record.machine_id, record.sensor_code)]
        latest = self.sensors.latest_event_time(spec.id)
        out_of_order = latest is not None and record.event_time < latest
        quality = QualityStatus.DELAYED if out_of_order else QualityStatus.VALID
        reading = self.sensors.insert_reading(
            record, spec.id, self.source.id, now, quality.value, json.dumps(record.metadata, default=str)
        )
        report.accepted_sensor += 1
        definition = self.sensors.get_definition(spec.id)
        if out_of_order and latest is not None:
            report.out_of_order += 1
            hit = out_of_order_hit(
                f"{record.sensor_code} sensor", record.event_time, latest,
                self.settings.severity_for(RuleCode.RECORD_OUT_OF_ORDER.value),
            )
            _, created = self.anomalies.raise_hit(
                machine_id=record.machine_id, sensor_id=spec.id, hit=hit,
                source_type=record.source_type.value, scenario_id=record.scenario_id,
                evidence={"reading_id": reading.id}, now=now,
            )
            self._count_anomaly(created, report)
        hits = evaluate_threshold(
            sensor_label=f"{record.machine_id} / {definition.sensor_code} ({definition.measurement_type})",
            value=record.value,
            unit=record.unit,
            lower=definition.lower_threshold,
            upper=definition.upper_threshold,
            severity=Severity(definition.threshold_severity),
            observed_time=record.event_time,
        )
        for hit in hits:
            _, created = self.anomalies.raise_hit(
                machine_id=record.machine_id, sensor_id=spec.id, hit=hit,
                source_type=record.source_type.value, scenario_id=record.scenario_id,
                evidence={"reading_id": reading.id, "value": record.value, "unit": record.unit,
                          "event_time": record.event_time.isoformat()},
                now=now,
            )
            self._count_anomaly(created, report)

    def _store_health(self, record: HealthRecord, now: datetime, report: BatchReport) -> None:
        if self._event_exists(SourceHealthEvent, record.source_event_id):
            report.duplicates += 1
            return
        self.session.add(
            SourceHealthEvent(
                source_id=self.source.id, health=record.health.value, event_time=record.event_time,
                ingested_at=now, detail=record.detail, source_event_id=record.source_event_id,
                scenario_id=record.scenario_id,
            )
        )
        self.session.flush()
        report.accepted_health += 1
        if record.health is SourceHealth.UNAVAILABLE:
            self._raise_connectivity(record.event_time, record.detail or "reported by source", report, now)

    # ------------------------------------------------------------------ #
    # Source health and periodic rules
    # ------------------------------------------------------------------ #

    def _record_health(self, health: SourceHealth, now: datetime, detail: str | None) -> None:
        """Record a source-health *transition* observed by the application."""
        if self.states.current_source_health(self.source.id) == health:
            return
        self.session.add(
            SourceHealthEvent(
                source_id=self.source.id, health=health.value, event_time=now, ingested_at=now,
                detail=(detail or "")[:500] or None,
            )
        )
        self.session.flush()

    def _source_machines(self) -> list[Machine]:
        return list(
            self.session.scalars(
                select(Machine).where(Machine.source_id == self.source.id, Machine.enabled.is_(True))
            )
        )

    def _raise_connectivity(
        self, observed: datetime, detail: str, report: BatchReport, now: datetime | None = None
    ) -> None:
        hit = source_unavailable_hit(
            detail, observed, self.settings.severity_for(RuleCode.SOURCE_UNAVAILABLE.value)
        )
        for machine in self._source_machines():
            _, created = self.anomalies.raise_hit(
                machine_id=machine.machine_id, hit=hit, source_type=self._source_type.value,
                evidence={"source_id": self.source.id}, now=now or observed,
            )
            self._count_anomaly(created, report)

    def evaluate_periodic(self, now: datetime, report: BatchReport | None = None) -> BatchReport:
        """Time-based rules (stale sensors, prolonged states). Idempotent per episode."""
        report = report or BatchReport()
        if self.states.current_source_health(self.source.id) is SourceHealth.UNAVAILABLE:
            return report  # root cause is already known; avoid redundant stale alerts
        limits = self.settings.state_limits()
        for machine in self._source_machines():
            for row in self.sensors.overview(now, self.settings.sensor_stale, machine.machine_id):
                hit = evaluate_sensor_staleness(
                    sensor_label=f"{machine.machine_id} / {row.definition.sensor_code}",
                    last_event_time=row.latest.event_time if row.latest else None,
                    now=now,
                    stale_after=row.stale_after,
                    severity=self.settings.severity_for(RuleCode.SENSOR_STALE.value),
                )
                if hit is not None:
                    _, created = self.anomalies.raise_hit(
                        machine_id=machine.machine_id, sensor_id=row.definition.id, hit=hit,
                        source_type=self._source_type.value, now=now,
                        evidence={"last_event_time": row.latest.event_time.isoformat()} if row.latest else {},
                    )
                    self._count_anomaly(created, report)
            view = self.states.machine_status(machine.machine_id, self.source.id, now, self.settings.freshness)
            if view.status in (MachineStatus.RUNNING, MachineStatus.STOPPED) and view.last_known_state:
                hit = evaluate_state_duration(
                    state=view.last_known_state, state_since=view.state_since, now=now, limits=limits,
                    severity=self.settings.severity_for(RuleCode.STATE_DURATION_EXCEEDED.value),
                )
                if hit is not None:
                    _, created = self.anomalies.raise_hit(
                        machine_id=machine.machine_id, hit=hit, source_type=self._source_type.value,
                        now=now, evidence={"state": view.last_known_state.value},
                    )
                    self._count_anomaly(created, report)
        return report
