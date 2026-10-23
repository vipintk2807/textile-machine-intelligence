"""Historical queries, summaries and CSV exports.

Every query uses the half-open window ``[start, end)``. Exports are built from the
same filtered queries as the on-screen tables (but without the display row cap), so
they always match the selected filters and never silently drop rows.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.intervals import Accounting
from src.core.settings_rules import RuntimeSettings
from src.models.orm import (
    Anomaly,
    DataSource,
    Machine,
    MachineStateEvent,
    MaintenanceEvent,
    RejectedRecord,
    SensorDefinition,
    SensorReading,
)
from src.models.schemas import ExportError, SourceType
from src.services.state_service import StateService
from src.utils.csv_utils import rows_to_csv
from src.utils.time_utils import Window, to_iso_z, utc_now

SIM_LABEL = "SIMULATED (synthetic data)"
REAL_LABEL = "REAL_SOURCE (provenance as configured)"


def provenance_label(source_type: str) -> str:
    return SIM_LABEL if source_type == SourceType.SIMULATED.value else REAL_LABEL


@dataclass(frozen=True)
class Page:
    rows: list[dict]
    total: int

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)


class ReportService:
    def __init__(
        self,
        session: Session,
        settings: RuntimeSettings,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.session = session
        self.settings = settings
        self.clock = clock

    def _window_cols(self, window: Window) -> dict:
        return {"report_start_utc": window.start, "report_end_utc": window.end}

    # ------------------------------ state history ---------------------------- #

    def state_events(
        self,
        window: Window,
        machine_ids: Sequence[str] | None = None,
        states: Sequence[str] | None = None,
        transitions_only: bool = True,
        limit: int | None = 500,
    ) -> Page:
        cond = [MachineStateEvent.event_time >= window.start, MachineStateEvent.event_time < window.end]
        if machine_ids:
            cond.append(MachineStateEvent.machine_id.in_(list(machine_ids)))
        if states:
            cond.append(MachineStateEvent.state.in_(list(states)))
        if transitions_only:
            cond.append(MachineStateEvent.is_transition.is_(True))
        total = int(self.session.scalar(select(func.count()).select_from(MachineStateEvent).where(*cond)) or 0)
        stmt = select(MachineStateEvent).where(*cond).order_by(
            MachineStateEvent.event_time.desc(), MachineStateEvent.id.desc()
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = [
            {
                "machine_id": e.machine_id,
                "event_time_utc": e.event_time,
                "ingested_at_utc": e.ingested_at,
                "state": e.state,
                "previous_state": e.previous_state,
                "is_transition": e.is_transition,
                "quality_status": e.quality_status,
                "source_type": e.source_type,
                "scenario_id": e.scenario_id,
                "data_provenance": provenance_label(e.source_type),
                **self._window_cols(window),
            }
            for e in self.session.scalars(stmt)
        ]
        return Page(rows, total)

    # ------------------------------ runtime summary -------------------------- #

    def accountings(self, window: Window, machine_ids: Sequence[str] | None = None) -> list[tuple[Machine, Accounting, str]]:
        stmt = select(Machine, DataSource.source_type).join(DataSource, DataSource.id == Machine.source_id)
        if machine_ids:
            stmt = stmt.where(Machine.machine_id.in_(list(machine_ids)))
        stmt = stmt.order_by(Machine.machine_id)
        states = StateService(self.session, self.clock)
        out = []
        for machine, source_type in self.session.execute(stmt).all():
            acc = states.account(machine.machine_id, machine.source_id, window, self.settings.freshness)
            out.append((machine, acc, source_type))
        return out

    def runtime_rows(self, window: Window, machine_ids: Sequence[str] | None = None) -> list[dict]:
        rows = []
        for machine, acc, source_type in self.accountings(window, machine_ids):
            share = acc.known_state_runtime_share
            rows.append(
                {
                    "machine_id": machine.machine_id,
                    "machine_name": machine.name,
                    "monitoring_enabled": machine.enabled,
                    "running_seconds": acc.running.total_seconds(),
                    "stopped_seconds": acc.stopped.total_seconds(),
                    "unknown_seconds": acc.unknown.total_seconds(),
                    "monitoring_gap_seconds": acc.gap.total_seconds(),
                    "total_seconds": acc.total.total_seconds(),
                    "known_state_runtime_share_percent": None if share is None else round(share, 2),
                    "monitoring_coverage_percent": round(acc.coverage_fraction * 100, 2),
                    "evidence_timeout_seconds": self.settings.freshness_seconds,
                    "notes": (
                        "Share is unavailable: no known running/stopped time in this window."
                        if share is None
                        else "Known-state runtime share = R/(R+D); excludes unknown and gap time; "
                        "not OEE or factory availability."
                    ),
                    "data_provenance": provenance_label(source_type),
                    **self._window_cols(window),
                }
            )
        return rows

    # ------------------------------ sensors ----------------------------------- #

    def sensor_readings(
        self,
        window: Window,
        machine_ids: Sequence[str] | None = None,
        sensor_ids: Sequence[int] | None = None,
        limit: int | None = 500,
    ) -> Page:
        cond = [SensorReading.event_time >= window.start, SensorReading.event_time < window.end]
        if machine_ids:
            cond.append(SensorReading.machine_id.in_(list(machine_ids)))
        if sensor_ids:
            cond.append(SensorReading.sensor_id.in_(list(sensor_ids)))
        total = int(self.session.scalar(select(func.count()).select_from(SensorReading).where(*cond)) or 0)
        stmt = (
            select(SensorReading, SensorDefinition)
            .join(SensorDefinition, SensorDefinition.id == SensorReading.sensor_id)
            .where(*cond)
            .order_by(SensorReading.event_time.desc(), SensorReading.id.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = [
            {
                "machine_id": r.machine_id,
                "sensor_code": d.sensor_code,
                "measurement_type": d.measurement_type,
                "value": r.value,
                "unit": r.unit,
                "unit_status": "known" if r.unit else "unknown",
                "event_time_utc": r.event_time,
                "ingested_at_utc": r.ingested_at,
                "quality_status": r.quality_status,
                "source_type": r.source_type,
                "scenario_id": r.scenario_id,
                "data_provenance": provenance_label(r.source_type),
                **self._window_cols(window),
            }
            for r, d in self.session.execute(stmt).all()
        ]
        return Page(rows, total)

    def rejected_records(
        self, window: Window, machine_ref: str | None = None, limit: int | None = 200
    ) -> Page:
        cond = [RejectedRecord.received_at >= window.start, RejectedRecord.received_at < window.end]
        if machine_ref:
            cond.append(RejectedRecord.machine_ref == machine_ref)
        total = int(self.session.scalar(select(func.count()).select_from(RejectedRecord).where(*cond)) or 0)
        stmt = select(RejectedRecord).where(*cond).order_by(RejectedRecord.received_at.desc(), RejectedRecord.id.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = []
        for r in self.session.scalars(stmt):
            try:
                issues = "; ".join(f"{i['code']}: {i['message']}" for i in json.loads(r.issues_json))
            except (ValueError, KeyError, TypeError):
                issues = "(unreadable diagnosis)"
            rows.append(
                {
                    "received_at_utc": r.received_at,
                    "record_type": r.record_type,
                    "machine_ref": r.machine_ref,
                    "diagnosis": issues,
                    "raw_payload": r.raw_payload,
                    "source_type": r.source_type,
                    "data_provenance": provenance_label(r.source_type),
                    **self._window_cols(window),
                }
            )
        return Page(rows, total)

    # ------------------------------ anomalies / maintenance ------------------- #

    def anomaly_rows(
        self,
        window: Window,
        machine_ids: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        limit: int | None = 500,
    ) -> Page:
        cond = [Anomaly.detected_at >= window.start, Anomaly.detected_at < window.end]
        if machine_ids:
            cond.append(Anomaly.machine_id.in_(list(machine_ids)))
        if statuses:
            cond.append(Anomaly.status.in_(list(statuses)))
        total = int(self.session.scalar(select(func.count()).select_from(Anomaly).where(*cond)) or 0)
        stmt = select(Anomaly).where(*cond).order_by(Anomaly.detected_at.desc(), Anomaly.id.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = [
            {
                "anomaly_id": a.id,
                "machine_id": a.machine_id,
                "sensor_id": a.sensor_id,
                "rule_code": a.rule_code,
                "category": a.category,
                "severity": a.severity,
                "status": a.status,
                "detected_at_utc": a.detected_at,
                "last_observed_at_utc": a.last_observed_at,
                "observed_event_time_utc": a.observed_event_time,
                "occurrence_count": a.occurrence_count,
                "observed_value": a.observed_value,
                "threshold_value": a.threshold_value,
                "explanation": a.description,
                "source_type": a.source_type,
                "data_provenance": provenance_label(a.source_type),
                **self._window_cols(window),
            }
            for a in self.session.scalars(stmt)
        ]
        return Page(rows, total)

    def maintenance_rows(
        self, window: Window, machine_ids: Sequence[str] | None = None, limit: int | None = 500
    ) -> Page:
        cond = [MaintenanceEvent.start_time >= window.start, MaintenanceEvent.start_time < window.end]
        if machine_ids:
            cond.append(MaintenanceEvent.machine_id.in_(list(machine_ids)))
        total = int(self.session.scalar(select(func.count()).select_from(MaintenanceEvent).where(*cond)) or 0)
        stmt = select(MaintenanceEvent).where(*cond).order_by(
            MaintenanceEvent.start_time.desc(), MaintenanceEvent.id.desc()
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = [
            {
                "maintenance_id": m.id,
                "machine_id": m.machine_id,
                "event_type": m.event_type,
                "start_time_utc": m.start_time,
                "end_time_utc": m.end_time,
                "description": m.description,
                "user_entered_finding": m.finding,
                "responsible": m.responsible,
                "notes": m.notes,
                "related_anomaly_id": m.related_anomaly_id,
                "data_provenance": "USER-ENTERED (not verified by the system)",
                **self._window_cols(window),
            }
            for m in self.session.scalars(stmt)
        ]
        return Page(rows, total)

    # ------------------------------ CSV exports ------------------------------- #

    EXPORTS = {
        "state_history": [
            "machine_id", "event_time_utc", "ingested_at_utc", "state", "previous_state",
            "is_transition", "quality_status", "source_type", "scenario_id", "data_provenance",
            "report_start_utc", "report_end_utc",
        ],
        "runtime_summary": [
            "machine_id", "machine_name", "monitoring_enabled", "running_seconds", "stopped_seconds",
            "unknown_seconds", "monitoring_gap_seconds", "total_seconds",
            "known_state_runtime_share_percent", "monitoring_coverage_percent",
            "evidence_timeout_seconds", "notes", "data_provenance", "report_start_utc", "report_end_utc",
        ],
        "sensor_readings": [
            "machine_id", "sensor_code", "measurement_type", "value", "unit", "unit_status",
            "event_time_utc", "ingested_at_utc", "quality_status", "source_type", "scenario_id",
            "data_provenance", "report_start_utc", "report_end_utc",
        ],
        "anomalies": [
            "anomaly_id", "machine_id", "sensor_id", "rule_code", "category", "severity", "status",
            "detected_at_utc", "last_observed_at_utc", "observed_event_time_utc", "occurrence_count",
            "observed_value", "threshold_value", "explanation", "source_type", "data_provenance",
            "report_start_utc", "report_end_utc",
        ],
        "maintenance": [
            "maintenance_id", "machine_id", "event_type", "start_time_utc", "end_time_utc",
            "description", "user_entered_finding", "responsible", "notes", "related_anomaly_id",
            "data_provenance", "report_start_utc", "report_end_utc",
        ],
    }

    def export_csv(
        self,
        kind: str,
        window: Window,
        machine_ids: Sequence[str] | None = None,
        *,
        transitions_only: bool = True,
        max_rows: int = 200_000,
    ) -> str:
        """Build a CSV for the *same filters* used on screen; never silently truncates."""
        if kind not in self.EXPORTS:
            raise ExportError(f"Unsupported export type: {kind!r}.")
        cap = max_rows + 1  # one extra row lets rows_to_csv detect an over-limit result
        if kind == "state_history":
            rows = self.state_events(window, machine_ids, None, transitions_only, cap).rows
        elif kind == "runtime_summary":
            rows = self.runtime_rows(window, machine_ids)
        elif kind == "sensor_readings":
            rows = self.sensor_readings(window, machine_ids, None, cap).rows
        elif kind == "anomalies":
            rows = self.anomaly_rows(window, machine_ids, None, cap).rows
        else:
            rows = self.maintenance_rows(window, machine_ids, cap).rows
        return rows_to_csv(self.EXPORTS[kind], rows, max_rows=max_rows)

    @staticmethod
    def iso(dt: datetime | None) -> str:
        return "" if dt is None else to_iso_z(dt)
