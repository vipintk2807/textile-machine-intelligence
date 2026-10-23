"""Runtime settings model and validation (pure).

All defaults below are illustrative software configuration values, not
textile-industry standards.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from src.core.anomaly_rules import DEFAULT_RULE_SEVERITIES
from src.models.schemas import ObservedState, Severity, ValidationIssue
from src.utils.time_utils import get_zone

FRESHNESS_RANGE = (10, 86_400)
SENSOR_STALE_RANGE = (10, 604_800)
STATE_LIMIT_RANGE = (60, 604_800)
SIM_MACHINES_RANGE = (1, 12)
SIM_LOOKBACK_RANGE = (1, 24)
SIM_SEED_RANGE = (0, 2_147_483_647)

SETTING_KEYS = (
    "freshness_seconds",
    "sensor_stale_seconds",
    "timezone",
    "max_running_seconds",
    "max_stopped_seconds",
    "severities",
    "sim_seed",
    "sim_machine_count",
    "sim_lookback_hours",
)


@dataclass(frozen=True)
class RuntimeSettings:
    freshness_seconds: int = 300
    sensor_stale_seconds: int = 900
    timezone: str = "Asia/Kolkata"
    max_running_seconds: int | None = None  # None = rule disabled
    max_stopped_seconds: int | None = None
    severities: Mapping[str, Severity] = field(default_factory=lambda: dict(DEFAULT_RULE_SEVERITIES))
    sim_seed: int = 42
    sim_machine_count: int = 4
    sim_lookback_hours: int = 6

    @property
    def freshness(self) -> timedelta:
        return timedelta(seconds=self.freshness_seconds)

    @property
    def sensor_stale(self) -> timedelta:
        return timedelta(seconds=self.sensor_stale_seconds)

    def state_limits(self) -> dict[ObservedState, timedelta | None]:
        def td(v: int | None) -> timedelta | None:
            return None if v is None else timedelta(seconds=v)

        return {
            ObservedState.RUNNING: td(self.max_running_seconds),
            ObservedState.STOPPED: td(self.max_stopped_seconds),
        }

    def severity_for(self, rule_code: str) -> Severity:
        return self.severities.get(rule_code, DEFAULT_RULE_SEVERITIES.get(rule_code, Severity.WARNING))


def _int_in_range(value: Any, lo: int, hi: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if lo <= value <= hi else None


def validate_settings_changes(
    changes: Mapping[str, Any], current: RuntimeSettings
) -> tuple[RuntimeSettings, list[ValidationIssue]]:
    """Apply ``changes`` to ``current``; return new settings and any issues (unchanged if invalid)."""
    issues: list[ValidationIssue] = []
    new = current

    def bad(key: str, message: str) -> None:
        issues.append(ValidationIssue("INVALID_SETTING", key, message))

    for key, value in changes.items():
        if key not in SETTING_KEYS:
            bad(key, "Unknown setting.")
        elif key == "freshness_seconds":
            v = _int_in_range(value, *FRESHNESS_RANGE)
            if v is None:
                bad(key, f"Must be a whole number of seconds between {FRESHNESS_RANGE[0]} and {FRESHNESS_RANGE[1]}.")
            else:
                new = replace(new, freshness_seconds=v)
        elif key == "sensor_stale_seconds":
            v = _int_in_range(value, *SENSOR_STALE_RANGE)
            if v is None:
                bad(key, f"Must be a whole number of seconds between {SENSOR_STALE_RANGE[0]} and {SENSOR_STALE_RANGE[1]}.")
            else:
                new = replace(new, sensor_stale_seconds=v)
        elif key == "timezone":
            try:
                get_zone(value)
                new = replace(new, timezone=value.strip())
            except (ValueError, AttributeError) as exc:
                bad(key, str(exc))
        elif key in ("max_running_seconds", "max_stopped_seconds"):
            if value is None:
                new = replace(new, **{key: None})
            else:
                v = _int_in_range(value, *STATE_LIMIT_RANGE)
                if v is None:
                    bad(key, f"Must be empty (disabled) or between {STATE_LIMIT_RANGE[0]} and {STATE_LIMIT_RANGE[1]} seconds.")
                else:
                    new = replace(new, **{key: v})
        elif key == "severities":
            if not isinstance(value, Mapping):
                bad(key, "Severities must be a mapping of rule code to severity.")
                continue
            merged = dict(new.severities)
            ok = True
            for rule, sev in value.items():
                try:
                    if rule not in DEFAULT_RULE_SEVERITIES:
                        raise ValueError
                    merged[rule] = Severity(sev)
                except ValueError:
                    bad(key, f"Invalid rule or severity: {rule!r} -> {sev!r}.")
                    ok = False
            if ok:
                new = replace(new, severities=merged)
        elif key == "sim_seed":
            v = _int_in_range(value, *SIM_SEED_RANGE)
            if v is None:
                bad(key, f"Seed must be a whole number between {SIM_SEED_RANGE[0]} and {SIM_SEED_RANGE[1]}.")
            else:
                new = replace(new, sim_seed=v)
        elif key == "sim_machine_count":
            v = _int_in_range(value, *SIM_MACHINES_RANGE)
            if v is None:
                bad(key, f"Machine count must be between {SIM_MACHINES_RANGE[0]} and {SIM_MACHINES_RANGE[1]}.")
            else:
                new = replace(new, sim_machine_count=v)
        elif key == "sim_lookback_hours":
            v = _int_in_range(value, *SIM_LOOKBACK_RANGE)
            if v is None:
                bad(key, f"Lookback must be between {SIM_LOOKBACK_RANGE[0]} and {SIM_LOOKBACK_RANGE[1]} hours.")
            else:
                new = replace(new, sim_lookback_hours=v)
    return (current if issues else new), issues
