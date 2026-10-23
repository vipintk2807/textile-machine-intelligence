"""Per-machine status snapshot used by the dashboard and machine pages."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.freshness import MachineStatusView
from src.core.settings_rules import RuntimeSettings
from src.models.orm import DataSource, Machine
from src.models.schemas import MachineStatus, SourceHealth
from src.services.anomaly_service import AnomalyService
from src.services.state_service import StateService


@dataclass(frozen=True)
class MachineOverview:
    machine: Machine
    view: MachineStatusView
    active_anomalies: int
    source_type: str


def machine_overviews(
    session: Session,
    settings: RuntimeSettings,
    now: datetime,
    *,
    enabled_only: bool = True,
    machine_ids: Sequence[str] | None = None,
) -> list[MachineOverview]:
    stmt = select(Machine, DataSource.source_type).join(DataSource, DataSource.id == Machine.source_id)
    if enabled_only:
        stmt = stmt.where(Machine.enabled.is_(True))
    if machine_ids:
        stmt = stmt.where(Machine.machine_id.in_(list(machine_ids)))
    stmt = stmt.order_by(Machine.machine_id)
    states = StateService(session)
    counts = AnomalyService(session).active_counts_by_machine()
    out = []
    for machine, source_type in session.execute(stmt).all():
        view = states.machine_status(machine.machine_id, machine.source_id, now, settings.freshness)
        out.append(MachineOverview(machine, view, counts.get(machine.machine_id, 0), source_type))
    return out


def status_counts(overviews: Sequence[MachineOverview]) -> dict[str, int]:
    """Counts per effective status, plus how many have stale evidence or lost connectivity."""
    counter = Counter(o.view.status.value for o in overviews)
    return {
        "active": len(overviews),
        MachineStatus.RUNNING.value: counter[MachineStatus.RUNNING.value],
        MachineStatus.STOPPED.value: counter[MachineStatus.STOPPED.value],
        MachineStatus.UNKNOWN.value: counter[MachineStatus.UNKNOWN.value],
        MachineStatus.DISCONNECTED.value: counter[MachineStatus.DISCONNECTED.value],
        "stale": sum(1 for o in overviews if o.view.is_stale),
        "source_unavailable": sum(1 for o in overviews if o.view.source_health is SourceHealth.UNAVAILABLE),
    }
