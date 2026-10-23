"""Runtime / downtime / unknown / monitoring-gap accounting.

Accounting model (see docs/DESIGN_DECISIONS.md for the full policy)::

    T = R + D + U + G

``T`` is the reporting window duration, ``R`` known running time, ``D`` known
stopped time, ``U`` time whose state is *reported* UNKNOWN (or ambiguous) and
``G`` unobserved monitoring-gap time. The four categories tile the window
exactly, so no instant is counted twice.

Event-time policy
-----------------
* Observations are ordered by ``event_time``, never by ingestion order.
* Each observation supports its state from its own event time until the *next*
  observation, but for no longer than ``evidence_timeout`` (when given) and
  never into a known source outage. Time with no supporting observation is a
  gap (G): it is neither running nor stopped.
* ``evidence_timeout=None`` means a state persists until the next observation or
  the end of the window ("transition-only" semantics).
* Several observations at the same ``event_time``: identical states collapse to
  one; conflicting states are resolved by ``sequence`` when every observation
  carries one, otherwise the instant is *ambiguous* and counted as UNKNOWN.
* Windows are half-open ``[start, end)``; an observation exactly at ``end`` has
  no effect on the window and one exactly at ``start`` is included.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.models.schemas import GapReason, IntervalCategory, ObservedState
from src.utils.time_utils import Interval, Window, ensure_utc, merge_intervals


@dataclass(frozen=True)
class Observation:
    event_time: datetime
    state: ObservedState
    sequence: int | None = None


@dataclass(frozen=True)
class ResolvedPoint:
    event_time: datetime
    state: ObservedState
    ambiguous: bool = False


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    category: IntervalCategory
    state: ObservedState | None = None
    gap_reason: GapReason | None = None
    ambiguous: bool = False

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass(frozen=True)
class Accounting:
    window: Window
    running: timedelta
    stopped: timedelta
    unknown: timedelta
    gap: timedelta
    segments: tuple[Segment, ...]
    evidence_timeout: timedelta | None
    ambiguous_instants: int = 0

    @property
    def total(self) -> timedelta:
        return self.window.duration

    @property
    def classified_total(self) -> timedelta:
        return self.running + self.stopped + self.unknown + self.gap

    @property
    def is_balanced(self) -> bool:
        """True when R + D + U + G equals T exactly."""
        return self.classified_total == self.total

    @property
    def known_state_time(self) -> timedelta:
        return self.running + self.stopped

    @property
    def known_state_runtime_share(self) -> float | None:
        """R / (R + D) x 100, or ``None`` when R + D is zero (never a fake 0 %)."""
        return known_state_runtime_share(self.running, self.stopped)

    @property
    def coverage_fraction(self) -> float:
        """Share of the window that is not an unobserved gap, (T - G) / T."""
        return 1.0 - (self.gap / self.total)


def known_state_runtime_share(running: timedelta, stopped: timedelta) -> float | None:
    known = running + stopped
    if known <= timedelta(0):
        return None
    return 100.0 * (running / known)


def resolve_observations(observations: Iterable[Observation]) -> list[ResolvedPoint]:
    """Collapse raw observations into one resolved point per distinct event time."""
    groups: dict[datetime, list[Observation]] = {}
    for obs in observations:
        ensure_utc(obs.event_time)
        groups.setdefault(obs.event_time, []).append(obs)

    points: list[ResolvedPoint] = []
    for moment in sorted(groups):
        group = groups[moment]
        states = {o.state for o in group}
        if len(states) == 1:
            points.append(ResolvedPoint(moment, next(iter(states))))
            continue
        sequences = [o.sequence for o in group]
        if all(s is not None for s in sequences):
            top = max(s for s in sequences if s is not None)
            top_states = {o.state for o in group if o.sequence == top}
            if len(top_states) == 1:
                points.append(ResolvedPoint(moment, next(iter(top_states))))
                continue
        points.append(ResolvedPoint(moment, ObservedState.UNKNOWN, ambiguous=True))
    return points


def _category(state: ObservedState) -> IntervalCategory:
    if state is ObservedState.RUNNING:
        return IntervalCategory.RUNNING
    if state is ObservedState.STOPPED:
        return IntervalCategory.STOPPED
    return IntervalCategory.UNKNOWN


def _gap_segments(
    start: datetime,
    end: datetime,
    outages: Sequence[Interval],
    first_observation: datetime | None,
) -> list[Segment]:
    """Split a gap into sub-segments labelled with the reason for the gap."""
    cuts = {start, end}
    for o in outages:
        for t in (o.start, o.end):
            if start < t < end:
                cuts.add(t)
    if first_observation is not None and start < first_observation < end:
        cuts.add(first_observation)
    ordered = sorted(cuts)
    out: list[Segment] = []
    for a, b in zip(ordered, ordered[1:]):
        if any(o.start <= a and b <= o.end for o in outages):
            reason = GapReason.SOURCE_UNAVAILABLE
        elif first_observation is None:
            reason = GapReason.NO_OBSERVATIONS
        elif b <= first_observation:
            reason = GapReason.BEFORE_FIRST_OBSERVATION
        else:
            reason = GapReason.EVIDENCE_EXPIRED
        out.append(Segment(a, b, IntervalCategory.GAP, gap_reason=reason))
    return out


def _merge_segments(segments: list[Segment]) -> list[Segment]:
    merged: list[Segment] = []
    for seg in segments:
        if (
            merged
            and merged[-1].end == seg.start
            and merged[-1].category == seg.category
            and merged[-1].state == seg.state
            and merged[-1].gap_reason == seg.gap_reason
            and merged[-1].ambiguous == seg.ambiguous
        ):
            prev = merged[-1]
            merged[-1] = Segment(
                prev.start, seg.end, prev.category, prev.state, prev.gap_reason, prev.ambiguous
            )
        else:
            merged.append(seg)
    return merged


def account_window(
    observations: Iterable[Observation],
    window: Window,
    evidence_timeout: timedelta | None = None,
    outages: Sequence[Interval] = (),
    first_observation_time: datetime | None = None,
) -> Accounting:
    """Classify every instant of ``window`` as running, stopped, unknown or gap.

    ``observations`` should include the most recent observation(s) *before* the
    window start (within ``evidence_timeout``) so a state that began earlier is
    carried into the window. ``outages`` are known source-unavailable intervals;
    ``first_observation_time`` is the machine's earliest observation anywhere
    (used only to label gap reasons).
    """
    if evidence_timeout is not None and evidence_timeout <= timedelta(0):
        raise ValueError("evidence_timeout must be positive or None.")

    points = resolve_observations(observations)
    merged_outages = merge_intervals(list(outages))
    outage_starts = [o.start for o in merged_outages]
    first_t = first_observation_time
    if first_t is None and points:
        first_t = points[0].event_time

    segments: list[Segment] = []
    cursor = window.start
    for i, point in enumerate(points):
        span_end = points[i + 1].event_time if i + 1 < len(points) else window.end
        if evidence_timeout is not None:
            span_end = min(span_end, point.event_time + evidence_timeout)
        # Known outage cuts the extension of evidence: find first outage start >= t.
        idx = bisect_right(outage_starts, point.event_time - timedelta(microseconds=1))
        if idx < len(outage_starts):
            span_end = min(span_end, outage_starts[idx])

        start = max(point.event_time, window.start)
        end = min(span_end, window.end)
        if end <= start or end <= cursor:
            continue
        start = max(start, cursor)
        if start > cursor:
            segments.extend(_gap_segments(cursor, start, merged_outages, first_t))
        segments.append(
            Segment(
                start,
                end,
                _category(point.state),
                state=point.state,
                ambiguous=point.ambiguous,
            )
        )
        cursor = end

    if cursor < window.end:
        segments.extend(_gap_segments(cursor, window.end, merged_outages, first_t))

    segments = _merge_segments(segments)
    totals = {c: timedelta(0) for c in IntervalCategory}
    for seg in segments:
        totals[seg.category] += seg.duration

    return Accounting(
        window=window,
        running=totals[IntervalCategory.RUNNING],
        stopped=totals[IntervalCategory.STOPPED],
        unknown=totals[IntervalCategory.UNKNOWN],
        gap=totals[IntervalCategory.GAP],
        segments=tuple(segments),
        evidence_timeout=evidence_timeout,
        ambiguous_instants=sum(1 for p in points if p.ambiguous and window.contains(p.event_time)),
    )


def sum_accountings(items: Sequence[Accounting]) -> tuple[timedelta, timedelta, timedelta, timedelta]:
    """Sum (R, D, U, G) across machines for the same window."""
    zero = timedelta(0)
    return (
        sum((a.running for a in items), zero),
        sum((a.stopped for a in items), zero),
        sum((a.unknown for a in items), zero),
        sum((a.gap for a in items), zero),
    )
