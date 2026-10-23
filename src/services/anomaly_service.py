"""Persistence and workflow for rule-triggered anomalies.

Episode policy: while an anomaly for the same (machine, sensor, rule) is OPEN or
ACKNOWLEDGED, repeated triggers update it (``occurrence_count``,
``last_observed_at``, latest evidence) instead of creating duplicates. A new
episode starts only after the previous one is RESOLVED. Acknowledging or opening an
anomaly never resolves it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.anomaly_rules import RuleHit
from src.models.orm import Anomaly
from src.models.schemas import (
    AnomalyStatus,
    NotFoundError,
    StateTransitionError,
    ValidationFailed,
    ValidationIssue,
)
from src.utils.time_utils import Window, utc_now

ACTIVE_STATUSES = (AnomalyStatus.OPEN.value, AnomalyStatus.ACKNOWLEDGED.value)


class AnomalyService:
    def __init__(self, session: Session, clock: Callable[[], datetime] = utc_now):
        self.session = session
        self.clock = clock

    def raise_hit(
        self,
        *,
        machine_id: str,
        hit: RuleHit,
        source_type: str,
        sensor_id: int | None = None,
        scenario_id: str | None = None,
        evidence: dict | None = None,
        now: datetime | None = None,
    ) -> tuple[Anomaly, bool]:
        """Create an anomaly or update the active one. Returns ``(anomaly, created)``."""
        now = now or self.clock()
        stmt = select(Anomaly).where(
            Anomaly.machine_id == machine_id,
            Anomaly.rule_code == hit.rule_code.value,
            Anomaly.status.in_(ACTIVE_STATUSES),
        )
        stmt = stmt.where(Anomaly.sensor_id.is_(None) if sensor_id is None else Anomaly.sensor_id == sensor_id)
        existing = self.session.scalar(stmt.order_by(Anomaly.id.desc()).limit(1))
        evidence_json = json.dumps(evidence or {}, default=str)
        if existing is not None:
            existing.occurrence_count += 1
            existing.last_observed_at = now
            existing.observed_event_time = hit.observed_time or existing.observed_event_time
            existing.observed_value = hit.observed_value
            existing.threshold_value = hit.threshold_value
            existing.description = hit.description
            existing.evidence_json = evidence_json
            existing.updated_at = now
            self.session.flush()
            return existing, False
        anomaly = Anomaly(
            machine_id=machine_id,
            sensor_id=sensor_id,
            rule_code=hit.rule_code.value,
            category=hit.category.value,
            severity=hit.severity.value,
            status=AnomalyStatus.OPEN.value,
            detected_at=now,
            last_observed_at=now,
            observed_event_time=hit.observed_time,
            occurrence_count=1,
            observed_value=hit.observed_value,
            threshold_value=hit.threshold_value,
            description=hit.description,
            evidence_json=evidence_json,
            source_type=source_type,
            scenario_id=scenario_id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(anomaly)
        self.session.flush()
        return anomaly, True

    # ------------------------------ workflow --------------------------------- #

    def get(self, anomaly_id: int) -> Anomaly:
        anomaly = self.session.get(Anomaly, anomaly_id)
        if anomaly is None:
            raise NotFoundError(f"Anomaly {anomaly_id} was not found.")
        return anomaly

    @staticmethod
    def _append_note(existing: str | None, stamp: datetime, text: str) -> str:
        line = f"[{stamp:%Y-%m-%d %H:%M} UTC] {text.strip()}"
        return f"{existing}\n{line}" if existing else line

    def acknowledge(self, anomaly_id: int, note: str | None = None) -> Anomaly:
        """Mark as reviewed. This does NOT mean the condition is fixed."""
        anomaly = self.get(anomaly_id)
        if anomaly.status != AnomalyStatus.OPEN.value:
            raise StateTransitionError("Only an open anomaly can be acknowledged.")
        now = self.clock()
        anomaly.status = AnomalyStatus.ACKNOWLEDGED.value
        anomaly.acknowledged_at = now
        if note and note.strip():
            anomaly.investigation_notes = self._append_note(anomaly.investigation_notes, now, note)
        anomaly.updated_at = now
        self.session.flush()
        return anomaly

    def add_note(self, anomaly_id: int, note: str) -> Anomaly:
        if not note or not note.strip():
            raise ValidationFailed([ValidationIssue("REQUIRED", "note", "Enter a note.")])
        anomaly = self.get(anomaly_id)
        now = self.clock()
        anomaly.investigation_notes = self._append_note(anomaly.investigation_notes, now, note)
        anomaly.updated_at = now
        self.session.flush()
        return anomaly

    def resolve(self, anomaly_id: int, note: str) -> Anomaly:
        if not note or not note.strip():
            raise ValidationFailed([ValidationIssue("REQUIRED", "note", "A resolution note is required.")])
        anomaly = self.get(anomaly_id)
        if anomaly.status == AnomalyStatus.RESOLVED.value:
            raise StateTransitionError("This anomaly is already resolved.")
        now = self.clock()
        anomaly.status = AnomalyStatus.RESOLVED.value
        anomaly.resolved_at = now
        anomaly.resolution_note = note.strip()
        anomaly.updated_at = now
        self.session.flush()
        return anomaly

    # -------------------------------- queries -------------------------------- #

    def list(
        self,
        *,
        machine_id: str | None = None,
        statuses: Sequence[str] | None = None,
        category: str | None = None,
        severity: str | None = None,
        window: Window | None = None,
        limit: int | None = 500,
    ) -> list[Anomaly]:
        stmt = select(Anomaly)
        if machine_id:
            stmt = stmt.where(Anomaly.machine_id == machine_id)
        if statuses:
            stmt = stmt.where(Anomaly.status.in_(list(statuses)))
        if category:
            stmt = stmt.where(Anomaly.category == category)
        if severity:
            stmt = stmt.where(Anomaly.severity == severity)
        if window is not None:
            stmt = stmt.where(Anomaly.detected_at >= window.start, Anomaly.detected_at < window.end)
        stmt = stmt.order_by(Anomaly.detected_at.desc(), Anomaly.id.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def active_count(self, machine_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(Anomaly).where(Anomaly.status.in_(ACTIVE_STATUSES))
        if machine_id:
            stmt = stmt.where(Anomaly.machine_id == machine_id)
        return int(self.session.scalar(stmt) or 0)

    def active_counts_by_machine(self) -> dict[str, int]:
        rows = self.session.execute(
            select(Anomaly.machine_id, func.count())
            .where(Anomaly.status.in_(ACTIVE_STATUSES))
            .group_by(Anomaly.machine_id)
        ).all()
        return {machine: int(n) for machine, n in rows}
