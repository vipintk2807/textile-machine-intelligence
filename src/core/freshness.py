"""Freshness and effective-status rules (pure functions).

Key principles enforced here:

* Freshness is judged from the **event time**, never from when a record was
  ingested, so a recently ingested old record is not "current".
* A source outage (DISCONNECTED) and a stale/unknown machine state are different
  conditions from a confirmed STOPPED machine and are never merged with it.
* The last known state is always reported *separately* from the effective status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.core.intervals import ResolvedPoint
from src.models.schemas import (
    FreshnessLabel,
    MachineStatus,
    ObservedState,
    SourceHealth,
)


@dataclass(frozen=True)
class Freshness:
    label: FreshnessLabel
    age: timedelta | None
    last_event_time: datetime | None

    @property
    def is_current(self) -> bool:
        return self.label is FreshnessLabel.CURRENT


def classify_freshness(
    last_event_time: datetime | None, now: datetime, threshold: timedelta
) -> Freshness:
    """CURRENT when ``now - last_event_time <= threshold``; STALE when older; NO_DATA if none."""
    if last_event_time is None:
        return Freshness(FreshnessLabel.NO_DATA, None, None)
    age = max(now - last_event_time, timedelta(0))  # clock skew inside tolerance -> age 0
    label = FreshnessLabel.CURRENT if age <= threshold else FreshnessLabel.STALE
    return Freshness(label, age, last_event_time)


@dataclass(frozen=True)
class MachineStatusView:
    status: MachineStatus
    last_known_state: ObservedState | None
    state_since: datetime | None
    last_evidence_time: datetime | None
    is_stale: bool
    source_health: SourceHealth
    reason: str

    @property
    def is_confirmed(self) -> bool:
        """True only for a current RUNNING/STOPPED status."""
        return self.status in (MachineStatus.RUNNING, MachineStatus.STOPPED)


def derive_machine_status(
    *,
    latest: ResolvedPoint | None,
    state_since: datetime | None,
    source_health: SourceHealth,
    now: datetime,
    threshold: timedelta,
) -> MachineStatusView:
    """Combine the latest resolved state, its freshness and source health.

    Order of precedence:

    1. Source reported UNAVAILABLE -> DISCONNECTED (the last known state is kept
       separately and is *not* presented as a confirmed physical condition).
    2. No observation ever -> UNKNOWN.
    3. Latest observation older than the freshness threshold -> UNKNOWN (stale).
    4. Latest state is UNKNOWN or ambiguous -> UNKNOWN.
    5. Otherwise RUNNING or STOPPED.
    """
    last_state = latest.state if latest else None
    last_time = latest.event_time if latest else None
    freshness = classify_freshness(last_time, now, threshold)
    stale = freshness.label is FreshnessLabel.STALE

    if source_health is SourceHealth.UNAVAILABLE:
        return MachineStatusView(
            MachineStatus.DISCONNECTED,
            last_state,
            state_since,
            last_time,
            stale,
            source_health,
            "The monitoring source is reported unavailable. The last known state may be out "
            "of date and is not a confirmed physical condition.",
        )
    if latest is None:
        return MachineStatusView(
            MachineStatus.UNKNOWN,
            None,
            None,
            None,
            False,
            source_health,
            "No state observation has been received for this machine.",
        )
    if stale:
        return MachineStatusView(
            MachineStatus.UNKNOWN,
            last_state,
            state_since,
            last_time,
            True,
            source_health,
            "The latest state observation is older than the freshness threshold, so the "
            "current state cannot be established. This does not mean the machine stopped.",
        )
    if latest.ambiguous or latest.state is ObservedState.UNKNOWN:
        return MachineStatusView(
            MachineStatus.UNKNOWN,
            last_state,
            state_since,
            last_time,
            False,
            source_health,
            "The source reports the state as unknown"
            + (" (conflicting records at the same timestamp)." if latest.ambiguous else "."),
        )
    status = MachineStatus.RUNNING if latest.state is ObservedState.RUNNING else MachineStatus.STOPPED
    return MachineStatusView(
        status,
        last_state,
        state_since,
        last_time,
        False,
        source_health,
        "Reported by the latest valid, current observation.",
    )
