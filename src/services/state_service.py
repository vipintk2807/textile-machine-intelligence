"""Machine-state persistence and derived state (event-time based).

Stored rows are *observations*. ``is_transition`` / ``previous_state`` are derived
by event-time order and re-derived for the next event whenever a late record is
inserted, so a delayed record never overwrites the current state.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.freshness import MachineStatusView, derive_machine_status
from src.core.intervals import (
    Accounting,
    Observation,
    ResolvedPoint,
    account_window,
    resolve_observations,
)
from src.models.orm import MachineStateEvent, SourceHealthEvent
from src.models.schemas import ObservedState, SourceHealth, StateRecord
from src.utils.time_utils import Interval, Window, utc_now


@dataclass(frozen=True)
class InsertResult:
    event: MachineStateEvent
    out_of_order: bool
    latest_before: datetime | None


class StateService:
    def __init__(self, session: Session, clock: Callable[[], datetime] = utc_now):
        self.session = session
        self.clock = clock

    # ------------------------------- writes ---------------------------------- #

    def latest_event_time(self, machine_id: str) -> datetime | None:
        return self.session.scalar(
            select(func.max(MachineStateEvent.event_time)).where(MachineStateEvent.machine_id == machine_id)
        )

    def _previous_event(self, machine_id: str, before: datetime) -> MachineStateEvent | None:
        return self.session.scalar(
            select(MachineStateEvent)
            .where(MachineStateEvent.machine_id == machine_id, MachineStateEvent.event_time < before)
            .order_by(MachineStateEvent.event_time.desc(), MachineStateEvent.id.desc())
            .limit(1)
        )

    def insert_observation(
        self, record: StateRecord, source_id: int, ingested_at: datetime, quality: str
    ) -> InsertResult:
        latest = self.latest_event_time(record.machine_id)
        out_of_order = latest is not None and record.event_time < latest
        prev = self._previous_event(record.machine_id, record.event_time)
        event = MachineStateEvent(
            machine_id=record.machine_id,
            state=record.state.value,
            previous_state=prev.state if prev else None,
            is_transition=prev is None or prev.state != record.state.value,
            event_time=record.event_time,
            ingested_at=ingested_at,
            source_id=source_id,
            source_type=record.source_type.value,
            source_event_id=record.source_event_id,
            source_sequence=record.source_sequence,
            quality_status=quality,
            scenario_id=record.scenario_id,
            metadata_json=json.dumps(record.metadata, default=str),
        )
        self.session.add(event)
        self.session.flush()
        if out_of_order:
            self._refresh_following(record.machine_id, record.event_time)
        return InsertResult(event, out_of_order, latest)

    def _refresh_following(self, machine_id: str, after: datetime) -> None:
        """Re-derive previous_state/is_transition for the first event group after ``after``."""
        next_time = self.session.scalar(
            select(func.min(MachineStateEvent.event_time)).where(
                MachineStateEvent.machine_id == machine_id, MachineStateEvent.event_time > after
            )
        )
        if next_time is None:
            return
        prev = self._previous_event(machine_id, next_time)
        rows = self.session.scalars(
            select(MachineStateEvent).where(
                MachineStateEvent.machine_id == machine_id, MachineStateEvent.event_time == next_time
            )
        ).all()
        for row in rows:
            row.previous_state = prev.state if prev else None
            row.is_transition = prev is None or prev.state != row.state
        self.session.flush()

    # ------------------------------- reads ----------------------------------- #

    def resolved_latest(self, machine_id: str) -> ResolvedPoint | None:
        """Latest state by EVENT time (not ingestion order), conflicts resolved per policy."""
        last_time = self.latest_event_time(machine_id)
        if last_time is None:
            return None
        rows = self.session.scalars(
            select(MachineStateEvent).where(
                MachineStateEvent.machine_id == machine_id, MachineStateEvent.event_time == last_time
            )
        ).all()
        points = resolve_observations(
            Observation(r.event_time, ObservedState(r.state), r.source_sequence) for r in rows
        )
        return points[-1]

    def state_since(self, machine_id: str, latest_time: datetime) -> datetime | None:
        return self.session.scalar(
            select(MachineStateEvent.event_time)
            .where(
                MachineStateEvent.machine_id == machine_id,
                MachineStateEvent.is_transition.is_(True),
                MachineStateEvent.event_time <= latest_time,
            )
            .order_by(MachineStateEvent.event_time.desc())
            .limit(1)
        )

    def current_source_health(self, source_id: int) -> SourceHealth:
        health = self.session.scalar(
            select(SourceHealthEvent.health)
            .where(SourceHealthEvent.source_id == source_id)
            .order_by(SourceHealthEvent.event_time.desc(), SourceHealthEvent.id.desc())
            .limit(1)
        )
        return SourceHealth(health) if health else SourceHealth.UNKNOWN

    def machine_status(
        self, machine_id: str, source_id: int, now: datetime, threshold: timedelta
    ) -> MachineStatusView:
        latest = self.resolved_latest(machine_id)
        since = self.state_since(machine_id, latest.event_time) if latest else None
        return derive_machine_status(
            latest=latest,
            state_since=since,
            source_health=self.current_source_health(source_id),
            now=now,
            threshold=threshold,
        )

    def outage_intervals(self, source_id: int, window: Window) -> list[Interval]:
        """Known UNAVAILABLE intervals for a source, derived from health events."""
        prior = self.session.scalar(
            select(SourceHealthEvent)
            .where(SourceHealthEvent.source_id == source_id, SourceHealthEvent.event_time < window.start)
            .order_by(SourceHealthEvent.event_time.desc(), SourceHealthEvent.id.desc())
            .limit(1)
        )
        events = list(
            self.session.scalars(
                select(SourceHealthEvent)
                .where(
                    SourceHealthEvent.source_id == source_id,
                    SourceHealthEvent.event_time >= window.start,
                    SourceHealthEvent.event_time < window.end,
                )
                .order_by(SourceHealthEvent.event_time, SourceHealthEvent.id)
            )
        )
        if prior is not None:
            events.insert(0, prior)
        outages: list[Interval] = []
        open_since: datetime | None = None
        for ev in events:
            if ev.health == SourceHealth.UNAVAILABLE.value:
                if open_since is None:
                    open_since = ev.event_time
            elif open_since is not None:
                outages.append(Interval(open_since, max(ev.event_time, open_since)))
                open_since = None
        if open_since is not None:
            outages.append(Interval(open_since, max(window.end, open_since)))
        return outages

    def account(
        self, machine_id: str, source_id: int, window: Window, evidence_timeout: timedelta | None
    ) -> Accounting:
        """Runtime/downtime/unknown/gap accounting for one machine over ``window``."""
        lo = window.start - evidence_timeout if evidence_timeout is not None else None
        bound = lo if lo is not None else window.start
        rows = list(
            self.session.scalars(
                select(MachineStateEvent)
                .where(
                    MachineStateEvent.machine_id == machine_id,
                    MachineStateEvent.event_time >= bound,
                    MachineStateEvent.event_time < window.end,
                )
                .order_by(MachineStateEvent.event_time)
            )
        )
        if lo is None:  # transition-only semantics also need the latest state before the window
            prior = self._previous_event(machine_id, window.start)
            if prior is not None:
                rows = list(
                    self.session.scalars(
                        select(MachineStateEvent).where(
                            MachineStateEvent.machine_id == machine_id,
                            MachineStateEvent.event_time == prior.event_time,
                        )
                    )
                ) + rows
        first = self.session.scalar(
            select(func.min(MachineStateEvent.event_time)).where(MachineStateEvent.machine_id == machine_id)
        )
        observations = [Observation(r.event_time, ObservedState(r.state), r.source_sequence) for r in rows]
        return account_window(
            observations,
            window,
            evidence_timeout,
            self.outage_intervals(source_id, window),
            first_observation_time=first,
        )

    def recent_transitions(self, limit: int = 15, machine_id: str | None = None) -> list[MachineStateEvent]:
        stmt = select(MachineStateEvent).where(MachineStateEvent.is_transition.is_(True))
        if machine_id:
            stmt = stmt.where(MachineStateEvent.machine_id == machine_id)
        stmt = stmt.order_by(MachineStateEvent.event_time.desc(), MachineStateEvent.id.desc()).limit(limit)
        return list(self.session.scalars(stmt))
