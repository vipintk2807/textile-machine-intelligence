"""Rule-based anomaly detection (pure, deterministic, no persistence).

Comparison semantics (explicit and tested)
------------------------------------------
* Upper threshold: triggers when ``value > upper`` (strictly greater).
* Lower threshold: triggers when ``value < lower`` (strictly less).
* A value exactly equal to a threshold does **not** trigger.
* Stale sensor: triggers when ``now - last_event_time > stale_after``.
* State duration: triggers when ``now - state_since > limit``.

All threshold values are *configuration*, never universal limits for textile
machinery. An anomaly means a configured rule fired; it is not a fault diagnosis.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.core.freshness import classify_freshness
from src.models.schemas import (
    AnomalyCategory,
    FreshnessLabel,
    ObservedState,
    RuleCode,
    RuleConfigError,
    Severity,
)
from src.utils.time_utils import format_duration

DEFAULT_RULE_SEVERITIES: dict[str, Severity] = {
    RuleCode.SENSOR_STALE.value: Severity.WARNING,
    RuleCode.STATE_DURATION_EXCEEDED.value: Severity.INFO,
    RuleCode.RECORD_REJECTED.value: Severity.WARNING,
    RuleCode.RECORD_OUT_OF_ORDER.value: Severity.INFO,
    RuleCode.SOURCE_UNAVAILABLE.value: Severity.WARNING,
}


@dataclass(frozen=True)
class RuleHit:
    rule_code: RuleCode
    category: AnomalyCategory
    severity: Severity
    description: str
    observed_value: float | None = None
    threshold_value: float | None = None
    observed_time: datetime | None = None


def validate_threshold_config(lower: float | None, upper: float | None) -> None:
    """Raise :class:`RuleConfigError` for an unusable threshold configuration."""
    for name, value in (("lower", lower), ("upper", upper)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuleConfigError(f"The {name} threshold must be a finite number.")
    if lower is not None and upper is not None and lower >= upper:
        raise RuleConfigError("The lower threshold must be smaller than the upper threshold.")


def evaluate_threshold(
    *,
    sensor_label: str,
    value: float,
    unit: str | None,
    lower: float | None,
    upper: float | None,
    severity: Severity,
    observed_time: datetime | None = None,
) -> list[RuleHit]:
    """Evaluate configured thresholds for one reading (strict comparisons)."""
    validate_threshold_config(lower, upper)
    if not math.isfinite(value):
        return []  # invalid readings are rejected upstream and never evaluated
    unit_text = f" {unit}" if unit else " (unit unknown)"
    hits: list[RuleHit] = []
    if upper is not None and value > upper:
        hits.append(
            RuleHit(
                RuleCode.SENSOR_ABOVE_UPPER,
                AnomalyCategory.SENSOR_THRESHOLD,
                severity,
                f"{sensor_label} reading {value:g}{unit_text} is above the configured upper "
                f"threshold {upper:g} (illustrative configuration, not a validated limit).",
                value,
                upper,
                observed_time,
            )
        )
    if lower is not None and value < lower:
        hits.append(
            RuleHit(
                RuleCode.SENSOR_BELOW_LOWER,
                AnomalyCategory.SENSOR_THRESHOLD,
                severity,
                f"{sensor_label} reading {value:g}{unit_text} is below the configured lower "
                f"threshold {lower:g} (illustrative configuration, not a validated limit).",
                value,
                lower,
                observed_time,
            )
        )
    return hits


def evaluate_sensor_staleness(
    *,
    sensor_label: str,
    last_event_time: datetime | None,
    now: datetime,
    stale_after: timedelta | None,
    severity: Severity,
) -> RuleHit | None:
    """Flag an expected reading that has not arrived recently.

    Returns ``None`` when the rule is disabled, or when no reading has ever been
    received (nothing to compare against -- shown as "no data" in the UI).
    """
    if stale_after is None or last_event_time is None:
        return None
    if stale_after <= timedelta(0):
        raise RuleConfigError("The stale-after duration must be positive.")
    freshness = classify_freshness(last_event_time, now, stale_after)
    if freshness.label is not FreshnessLabel.STALE or freshness.age is None:
        return None
    return RuleHit(
        RuleCode.SENSOR_STALE,
        AnomalyCategory.DATA_QUALITY,
        severity,
        f"No new {sensor_label} reading for {format_duration(freshness.age)} "
        f"(configured limit {format_duration(stale_after)}). This is a data-availability "
        "condition, not evidence of a machine fault.",
        None,
        stale_after.total_seconds(),
        last_event_time,
    )


def evaluate_state_duration(
    *,
    state: ObservedState,
    state_since: datetime | None,
    now: datetime,
    limits: Mapping[ObservedState, timedelta | None],
    severity: Severity,
) -> RuleHit | None:
    """Flag a machine that has been in a state longer than a configured limit."""
    limit = limits.get(state)
    if limit is None or state_since is None:
        return None
    if limit <= timedelta(0):
        raise RuleConfigError("A state duration limit must be positive.")
    elapsed = now - state_since
    if elapsed <= limit:
        return None
    return RuleHit(
        RuleCode.STATE_DURATION_EXCEEDED,
        AnomalyCategory.STATE_DURATION,
        severity,
        f"The machine has been reported {state.value} for {format_duration(elapsed)}, longer than "
        f"the configured limit of {format_duration(limit)}. This limit is a demonstration "
        "setting, not a textile-industry standard.",
        None,
        limit.total_seconds(),
        state_since,
    )


def rejection_hit(code_list: list[str], observed_time: datetime | None, severity: Severity) -> RuleHit:
    codes = ", ".join(sorted(set(code_list)))
    return RuleHit(
        RuleCode.RECORD_REJECTED,
        AnomalyCategory.DATA_QUALITY,
        severity,
        f"A source record for this machine failed validation ({codes}) and was quarantined. "
        "This is a data-quality condition, not a machine fault.",
        None,
        None,
        observed_time,
    )


def out_of_order_hit(
    stream_label: str, event_time: datetime, latest_time: datetime, severity: Severity
) -> RuleHit:
    return RuleHit(
        RuleCode.RECORD_OUT_OF_ORDER,
        AnomalyCategory.DATA_QUALITY,
        severity,
        f"A {stream_label} record with event time {event_time:%Y-%m-%d %H:%M:%S} UTC arrived after a "
        f"newer record ({latest_time:%Y-%m-%d %H:%M:%S} UTC). It was stored with its original "
        "event time and does not replace the current state.",
        None,
        None,
        event_time,
    )


def source_unavailable_hit(detail: str, observed_time: datetime, severity: Severity) -> RuleHit:
    return RuleHit(
        RuleCode.SOURCE_UNAVAILABLE,
        AnomalyCategory.CONNECTIVITY,
        severity,
        f"The monitoring source was reported unavailable ({detail}). Machine states during this "
        "period are unobserved; they are not treated as stopped.",
        None,
        None,
        observed_time,
    )
