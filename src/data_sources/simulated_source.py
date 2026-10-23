"""Deterministic simulated machine-data source.

*** Everything produced here is SYNTHETIC. It is not data from real textile
machinery, and the sensor "units" are illustrative labels only. ***

Determinism
-----------
Output is a pure function of ``(seed, machine id, time)`` plus the scripted
scenario, so the same configuration always yields the same records regardless of
how reads are chunked, and re-reading the same period produces records with the
same ``source_event_id`` (the ingestion layer then de-duplicates them). Nothing is
generated from the wall clock except where the caller passes ``now``.

The simulator can inject the situations the specification asks for: threshold
crossings, UNKNOWN windows, silent machines (stale data while the source is
healthy), sensor gaps, source outages (reported as errors, never as empty
results), late/out-of-order records, and several kinds of invalid record.
"""

from __future__ import annotations

import math
import random
import zlib
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.data_sources.base import SourceAdapter
from src.models.schemas import (
    ObservedState,
    RawRecord,
    ReadResult,
    RecordType,
    SourceError,
    SourceHealth,
    SourceHealthReport,
    SourceType,
)
from src.utils.time_utils import Interval, ensure_utc, to_iso_z

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
SIM_SOURCE_NAME = "Simulated source (SYNTHETIC DATA)"


@dataclass(frozen=True)
class SensorProfile:
    """Shape of one simulated signal. Types/units are illustrative labels only."""

    code: str
    measurement_type: str
    unit: str | None
    baseline: float
    amplitude: float
    noise: float
    period_minutes: float
    upper_threshold: float | None = None
    lower_threshold: float | None = None
    every_steps: int = 5


DEFAULT_SENSOR_PROFILES: tuple[SensorProfile, ...] = (
    SensorProfile(
        code="SIM-S1",
        measurement_type="Simulated signal 1",
        unit="sim-units",
        baseline=50.0,
        amplitude=10.0,
        noise=1.0,
        period_minutes=90.0,
        upper_threshold=75.0,  # illustrative demonstration threshold
    ),
    SensorProfile(
        code="SIM-S2",
        measurement_type="Simulated signal 2 (unit unknown)",
        unit=None,
        baseline=10.0,
        amplitude=2.0,
        noise=0.3,
        period_minutes=45.0,
    ),
)


@dataclass(frozen=True)
class Spike:
    machine_index: int
    sensor_code: str
    offset_minutes: int
    duration_minutes: int
    magnitude: float


@dataclass(frozen=True)
class MachineWindow:
    machine_index: int
    start_offset_minutes: int
    end_offset_minutes: int | None = None  # None = open-ended


@dataclass(frozen=True)
class SensorGap:
    machine_index: int
    sensor_code: str
    start_offset_minutes: int
    end_offset_minutes: int


@dataclass(frozen=True)
class OutageWindow:
    start_offset_minutes: int
    end_offset_minutes: int


@dataclass(frozen=True)
class LateArrival:
    machine_index: int
    start_offset_minutes: int
    end_offset_minutes: int
    delay_minutes: int


@dataclass(frozen=True)
class InvalidInjection:
    offset_minutes: int
    kind: str
    machine_index: int = 0


INVALID_KINDS = (
    "nan_value",
    "inf_value",
    "non_numeric",
    "unknown_sensor",
    "unit_mismatch",
    "bad_timestamp",
    "missing_timestamp",
    "naive_timestamp",
    "invalid_state",
    "unknown_machine",
    "missing_machine_id",
)


@dataclass(frozen=True)
class ScenarioScript:
    spikes: tuple[Spike, ...] = ()
    unknown_windows: tuple[MachineWindow, ...] = ()
    silent_windows: tuple[MachineWindow, ...] = ()
    sensor_gaps: tuple[SensorGap, ...] = ()
    outages: tuple[OutageWindow, ...] = ()
    late_arrivals: tuple[LateArrival, ...] = ()
    invalid_injections: tuple[InvalidInjection, ...] = ()


EMPTY_SCRIPT = ScenarioScript()

# Offsets are minutes relative to the anchor (negative = in the past).
DEMO_SCRIPT = ScenarioScript(
    spikes=(Spike(0, "SIM-S1", -120, 10, 35.0),),
    unknown_windows=(MachineWindow(1, -200, -185),),
    silent_windows=(MachineWindow(2, -45, 1),),
    sensor_gaps=(SensorGap(0, "SIM-S2", -90, -60),),
    outages=(OutageWindow(-300, -285),),
    late_arrivals=(LateArrival(3, -150, -140, 20),),
    invalid_injections=(
        InvalidInjection(-100, "nan_value"),
        InvalidInjection(-99, "unknown_sensor"),
        InvalidInjection(-98, "bad_timestamp"),
        InvalidInjection(-97, "unit_mismatch"),
        InvalidInjection(-96, "invalid_state"),
        InvalidInjection(-95, "unknown_machine"),
        InvalidInjection(-94, "non_numeric"),
        InvalidInjection(-93, "inf_value"),
        InvalidInjection(-92, "naive_timestamp"),
        InvalidInjection(-91, "missing_machine_id"),
    ),
)


@dataclass(frozen=True)
class SimulationConfig:
    anchor: datetime
    seed: int = 42
    step_seconds: int = 60
    scenario_id: str = "demo-v1"
    profiles: tuple[SensorProfile, ...] = field(default=DEFAULT_SENSOR_PROFILES)

    def __post_init__(self) -> None:
        ensure_utc(self.anchor)
        if not (1 <= self.step_seconds <= 3600):
            raise ValueError("step_seconds must be between 1 and 3600.")


@dataclass(frozen=True)
class _Emission:
    available_at: datetime
    event_time: datetime
    order: tuple
    record: RawRecord


class _RunSchedule:
    """Lazily extended, deterministic sequence of RUNNING/STOPPED runs for one machine."""

    def __init__(self, rng: random.Random):
        self._rng = rng
        self._starts: list[int] = [0]
        self._states: list[ObservedState] = [ObservedState.RUNNING]
        self._end = self._duration(ObservedState.RUNNING)

    def _duration(self, state: ObservedState) -> int:
        minutes = self._rng.randint(45, 150) if state is ObservedState.RUNNING else self._rng.randint(6, 30)
        return minutes * 60

    def state_at(self, t_seconds: int) -> ObservedState:
        while self._end <= t_seconds:
            new_state = (
                ObservedState.STOPPED
                if self._states[-1] is ObservedState.RUNNING
                else ObservedState.RUNNING
            )
            self._starts.append(self._end)
            self._states.append(new_state)
            self._end += self._duration(new_state)
        idx = max(bisect_right(self._starts, t_seconds) - 1, 0)
        return self._states[idx]


class SimulatedSource(SourceAdapter):
    """Read-only synthetic adapter implementing :class:`SourceAdapter`."""

    source_type = SourceType.SIMULATED

    def __init__(
        self,
        config: SimulationConfig,
        script: ScenarioScript = EMPTY_SCRIPT,
        machine_ids: Sequence[str] = (),
        start_after: datetime | None = None,
        extra_outages: Sequence[Interval] = (),
        name: str = SIM_SOURCE_NAME,
    ):
        self.config = config
        self.script = script
        self.name = name
        self._machine_ids: tuple[str, ...] = tuple(machine_ids)
        self._cursor = ensure_utc(start_after) if start_after else config.anchor
        self._extra_outages = tuple(extra_outages)
        self._schedules: dict[str, _RunSchedule] = {}
        self._connected = False
        self._health = SourceHealth.UNKNOWN
        self._detail: str | None = "Not connected."
        self._last_success: datetime | None = None
        a = config.anchor
        self._outages: tuple[Interval, ...] = tuple(
            Interval(a + timedelta(minutes=o.start_offset_minutes), a + timedelta(minutes=o.end_offset_minutes))
            for o in script.outages
        ) + self._extra_outages

    # ------------------------------------------------------------------ #
    # Adapter interface
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        self._connected = True
        self._health = SourceHealth.UNKNOWN
        self._detail = "Connected; no read yet."

    def close(self) -> None:
        self._connected = False
        self._health = SourceHealth.UNKNOWN
        self._detail = "Closed."

    def get_health(self) -> SourceHealthReport:
        return SourceHealthReport(self._health, self._last_success, self._detail)

    def set_machine_ids(self, machine_ids: Sequence[str]) -> None:
        self._machine_ids = tuple(machine_ids)

    @property
    def cursor(self) -> datetime:
        return self._cursor

    @property
    def outages(self) -> tuple[Interval, ...]:
        return self._outages

    def read_records(self, now: datetime, limit: int | None = None) -> ReadResult:
        if not self._connected:
            return ReadResult(error=SourceError("NOT_CONNECTED", "The simulated source is not connected."))
        now = ensure_utc(now)
        for outage in self._outages:
            if outage.start <= now < outage.end:
                self._health = SourceHealth.UNAVAILABLE
                self._detail = "Simulated source interruption (synthetic scenario)."
                return ReadResult(
                    error=SourceError(
                        "SOURCE_UNAVAILABLE",
                        "Simulated source interruption (synthetic scenario).",
                        retryable=True,
                    )
                )
        self._health = SourceHealth.HEALTHY
        self._detail = "Responding normally (simulated)."
        self._last_success = now
        if now <= self._cursor:
            return ReadResult()  # a valid, empty result

        emissions = self._emissions(self._cursor, now)
        emissions.sort(key=lambda e: (e.available_at, e.event_time, e.order))
        if limit is not None and len(emissions) > limit:
            emissions = emissions[:limit]
            self._cursor = emissions[-1].available_at
        else:
            self._cursor = now
        return ReadResult(records=tuple(e.record for e in emissions))

    # ------------------------------------------------------------------ #
    # Generation
    # ------------------------------------------------------------------ #

    def _at(self, minutes: int) -> datetime:
        return self.config.anchor + timedelta(minutes=minutes)

    def _in_outage(self, t: datetime) -> bool:
        return any(o.start <= t < o.end for o in self._outages)

    def _in_machine_window(self, windows: Sequence[MachineWindow], mi: int, t: datetime) -> bool:
        for w in windows:
            if w.machine_index != mi:
                continue
            end = self._at(w.end_offset_minutes) if w.end_offset_minutes is not None else None
            if self._at(w.start_offset_minutes) <= t and (end is None or t < end):
                return True
        return False

    def _delay_for(self, mi: int, t: datetime) -> timedelta:
        for late in self.script.late_arrivals:
            if (
                late.machine_index == mi
                and self._at(late.start_offset_minutes) <= t < self._at(late.end_offset_minutes)
            ):
                return timedelta(minutes=late.delay_minutes)
        return timedelta(0)

    def _state_at(self, machine_id: str, mi: int, t: datetime) -> ObservedState:
        if self._in_machine_window(self.script.unknown_windows, mi, t):
            return ObservedState.UNKNOWN
        schedule = self._schedules.get(machine_id)
        if schedule is None:
            schedule = _RunSchedule(random.Random(f"{self.config.seed}|{machine_id}|runs"))
            self._schedules[machine_id] = schedule
        return schedule.state_at(int((t - EPOCH).total_seconds()))

    def _sensor_value(self, machine_id: str, mi: int, profile: SensorProfile, t: datetime) -> float:
        minutes = (t - EPOCH).total_seconds() / 60.0
        phase = (zlib.crc32(f"{machine_id}|{profile.code}".encode()) % 628) / 100.0
        base = profile.baseline + profile.amplitude * math.sin(
            2 * math.pi * minutes / profile.period_minutes + phase
        )
        rng = random.Random(f"{self.config.seed}|{machine_id}|{profile.code}|{int((t - EPOCH).total_seconds())}")
        noise = max(-3.0, min(3.0, rng.gauss(0.0, 1.0))) * profile.noise
        spike = 0.0
        for sp in self.script.spikes:
            start = self._at(sp.offset_minutes)
            if (
                sp.machine_index == mi
                and sp.sensor_code == profile.code
                and start <= t < start + timedelta(minutes=sp.duration_minutes)
            ):
                spike += sp.magnitude
        return round(base + noise + spike, 4)

    def _has_sensor_gap(self, mi: int, code: str, t: datetime) -> bool:
        return any(
            g.machine_index == mi
            and g.sensor_code == code
            and self._at(g.start_offset_minutes) <= t < self._at(g.end_offset_minutes)
            for g in self.script.sensor_gaps
        )

    def _emissions(self, lo: datetime, hi: datetime) -> list[_Emission]:
        """All emissions with ``lo < available_at <= hi``."""
        cfg = self.config
        step = timedelta(seconds=cfg.step_seconds)
        sid = cfg.scenario_id
        max_delay = max((timedelta(minutes=late.delay_minutes) for late in self.script.late_arrivals), default=timedelta(0))
        first = (lo - max_delay - EPOCH) // step + 1
        last = (hi - EPOCH) // step
        out: list[_Emission] = []
        serial = 0

        for gi in range(first, last + 1):
            t = EPOCH + gi * step
            if self._in_outage(t):
                continue
            epoch_s = int((t - EPOCH).total_seconds())
            for mi, mid in enumerate(self._machine_ids):
                if self._in_machine_window(self.script.silent_windows, mi, t):
                    continue
                available = t + self._delay_for(mi, t)
                if not (lo < available <= hi):
                    continue
                state = self._state_at(mid, mi, t)
                serial += 1
                out.append(
                    _Emission(
                        available,
                        t,
                        (1, mi, 0, serial),
                        RawRecord(
                            RecordType.MACHINE_STATE.value,
                            {
                                "machine_id": mid,
                                "state": state.value,
                                "event_time": to_iso_z(t),
                                "source_type": SourceType.SIMULATED.value,
                                "source_event_id": f"{sid}:S:{mid}:{epoch_s}",
                                "scenario_id": sid,
                            },
                        ),
                    )
                )
                for pi, profile in enumerate(cfg.profiles):
                    if gi % profile.every_steps != 0 or self._has_sensor_gap(mi, profile.code, t):
                        continue
                    serial += 1
                    payload = {
                        "machine_id": mid,
                        "sensor_id": profile.code,
                        "value": self._sensor_value(mid, mi, profile, t),
                        "event_time": to_iso_z(t),
                        "source_type": SourceType.SIMULATED.value,
                        "source_event_id": f"{sid}:V:{mid}:{profile.code}:{epoch_s}",
                        "scenario_id": sid,
                    }
                    if profile.unit is not None:
                        payload["unit"] = profile.unit
                    out.append(
                        _Emission(
                            available, t, (1, mi, pi + 1, serial),
                            RawRecord(RecordType.SENSOR_READING.value, payload),
                        )
                    )

        # Source-health records for scripted outages (historical diagnostics).
        for outage in self._outages:
            for when, health in ((outage.start, SourceHealth.UNAVAILABLE), (outage.end, SourceHealth.HEALTHY)):
                if lo < when <= hi:
                    serial += 1
                    out.append(
                        _Emission(
                            when,
                            when,
                            (0, 0, 0, serial),
                            RawRecord(
                                RecordType.SOURCE_HEALTH.value,
                                {
                                    "health": health.value,
                                    "event_time": to_iso_z(when),
                                    "source_type": SourceType.SIMULATED.value,
                                    "source_event_id": f"{sid}:H:{int((when - EPOCH).total_seconds())}:{health.value}",
                                    "scenario_id": sid,
                                    "detail": "Simulated source interruption (synthetic scenario).",
                                },
                            ),
                        )
                    )

        # Deliberately invalid records, to exercise validation and quarantine.
        if self._machine_ids:
            for inj in self.script.invalid_injections:
                when = self._at(inj.offset_minutes)
                if lo < when <= hi and inj.kind in INVALID_KINDS:
                    mi = min(inj.machine_index, len(self._machine_ids) - 1)
                    serial += 1
                    out.append(
                        _Emission(
                            when, when, (2, mi, 0, serial),
                            self._invalid_record(inj.kind, self._machine_ids[mi], mi, when),
                        )
                    )
        return out

    def _invalid_record(self, kind: str, machine_id: str, mi: int, when: datetime) -> RawRecord:
        sid = self.config.scenario_id
        epoch_s = int((when - EPOCH).total_seconds())
        event_id = f"{sid}:BAD:{kind}:{epoch_s}:{mi}"
        sensor_profile = self.config.profiles[0]
        state_payload: dict = {
            "machine_id": machine_id,
            "state": ObservedState.RUNNING.value,
            "event_time": to_iso_z(when),
            "source_type": SourceType.SIMULATED.value,
            "source_event_id": event_id,
            "scenario_id": sid,
        }
        sensor_payload: dict = {
            "machine_id": machine_id,
            "sensor_id": sensor_profile.code,
            "value": 1.0,
            "event_time": to_iso_z(when),
            "source_type": SourceType.SIMULATED.value,
            "source_event_id": event_id,
            "scenario_id": sid,
        }
        if sensor_profile.unit is not None:
            sensor_payload["unit"] = sensor_profile.unit

        if kind == "nan_value":
            sensor_payload["value"] = float("nan")
            return RawRecord("sensor_reading", sensor_payload)
        if kind == "inf_value":
            sensor_payload["value"] = float("inf")
            return RawRecord("sensor_reading", sensor_payload)
        if kind == "non_numeric":
            sensor_payload["value"] = "abc"
            return RawRecord("sensor_reading", sensor_payload)
        if kind == "unknown_sensor":
            sensor_payload["sensor_id"] = "SIM-DOES-NOT-EXIST"
            return RawRecord("sensor_reading", sensor_payload)
        if kind == "unit_mismatch":
            sensor_payload["unit"] = "wrong-unit"
            return RawRecord("sensor_reading", sensor_payload)
        if kind == "bad_timestamp":
            state_payload["event_time"] = "not-a-timestamp"
            return RawRecord("machine_state", state_payload)
        if kind == "missing_timestamp":
            del state_payload["event_time"]
            return RawRecord("machine_state", state_payload)
        if kind == "naive_timestamp":
            state_payload["event_time"] = when.replace(tzinfo=None).isoformat()
            return RawRecord("machine_state", state_payload)
        if kind == "invalid_state":
            state_payload["state"] = "FAULT"
            return RawRecord("machine_state", state_payload)
        if kind == "unknown_machine":
            state_payload["machine_id"] = "SIM-GHOST"
            return RawRecord("machine_state", state_payload)
        # missing_machine_id
        del state_payload["machine_id"]
        return RawRecord("machine_state", state_payload)


def default_sensor_definitions(
    profiles: Sequence[SensorProfile] = DEFAULT_SENSOR_PROFILES,
) -> list[dict]:
    """Sensor definitions the simulator expects every simulated machine to have."""
    return [
        {
            "sensor_code": p.code,
            "measurement_type": p.measurement_type,
            "unit": p.unit,
            "lower_threshold": p.lower_threshold,
            "upper_threshold": p.upper_threshold,
        }
        for p in profiles
    ]
