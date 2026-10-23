"""Pure validation for ingestion records and user-entered forms.

Every function returns explicit :class:`ValidationIssue` objects (stable
``code``) instead of raising on the first problem, so rejected records can be
quarantined with a complete diagnosis.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from src.models.schemas import (
    HealthRecord,
    MAINTENANCE_TYPES,
    ObservedState,
    RawRecord,
    RecordType,
    SensorRecord,
    SensorSpec,
    SourceHealth,
    SourceType,
    StateRecord,
    ValidationIssue,
    ValidationOutcome,
)
from src.utils.time_utils import NaivePolicy, TimestampError, parse_timestamp

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
MAX_EVENT_ID_LEN = 128
MAX_METADATA_JSON_LEN = 2000
MAX_NAME_LEN = 120
MAX_TEXT_LEN = 2000


@dataclass(frozen=True)
class ValidationContext:
    """Everything the record validator needs to know about the system state."""

    expected_source_type: SourceType
    machines: Mapping[str, bool]  # machine_id -> monitoring enabled
    sensors: Mapping[tuple[str, str], SensorSpec]  # (machine_id, sensor_code) -> spec
    now: datetime
    future_tolerance: timedelta = field(default=timedelta(minutes=5))
    naive_policy: NaivePolicy = NaivePolicy.REJECT
    source_tz: tzinfo | None = None


def _issue(code: str, field_name: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, field=field_name, message=message)


# --------------------------------------------------------------------------- #
# Small field validators
# --------------------------------------------------------------------------- #


def check_identifier(value: Any, field_name: str, missing_code: str) -> tuple[str | None, list]:
    """Validate an identifier (machine id / sensor code). Returns (clean value, issues)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, [_issue(missing_code, field_name, f"{field_name} is required.")]
    if not isinstance(value, str):
        return None, [_issue("INVALID_IDENTIFIER", field_name, f"{field_name} must be text.")]
    clean = value.strip()
    if not IDENTIFIER_RE.match(clean):
        return None, [
            _issue(
                "INVALID_IDENTIFIER",
                field_name,
                f"{field_name} must be 1-64 characters: letters, digits, '_', '-' or '.', "
                "starting with a letter or digit.",
            )
        ]
    return clean, []


def check_event_time(
    value: Any, ctx: ValidationContext, field_name: str = "event_time"
) -> tuple[datetime | None, list]:
    try:
        parsed = parse_timestamp(value, ctx.naive_policy, ctx.source_tz)
    except TimestampError as exc:
        return None, [_issue(exc.code, field_name, str(exc))]
    if parsed > ctx.now + ctx.future_tolerance:
        return None, [
            _issue(
                "TIMESTAMP_IN_FUTURE",
                field_name,
                "Event time is later than the current time plus the allowed tolerance.",
            )
        ]
    return parsed, []


def check_optional_event_id(value: Any) -> tuple[str | None, list]:
    if value is None:
        return None, []
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_EVENT_ID_LEN:
        return None, [
            _issue(
                "INVALID_SOURCE_EVENT_ID",
                "source_event_id",
                f"source_event_id must be non-empty text up to {MAX_EVENT_ID_LEN} characters.",
            )
        ]
    return value.strip(), []


def check_optional_metadata(value: Any) -> tuple[dict[str, Any], list]:
    if value is None:
        return {}, []
    if not isinstance(value, Mapping):
        return {}, [_issue("INVALID_METADATA", "metadata", "metadata must be an object.")]
    try:
        text = json.dumps(dict(value), default=str)
    except (TypeError, ValueError):
        return {}, [_issue("INVALID_METADATA", "metadata", "metadata is not serialisable.")]
    if len(text) > MAX_METADATA_JSON_LEN:
        return {}, [_issue("INVALID_METADATA", "metadata", "metadata is too large.")]
    return dict(value), []


def check_source_type(payload: Mapping[str, Any], ctx: ValidationContext) -> list:
    raw = payload.get("source_type")
    if raw is None:
        return [_issue("MISSING_SOURCE_TYPE", "source_type", "source_type is required.")]
    if raw != ctx.expected_source_type.value:
        return [
            _issue(
                "SOURCE_TYPE_MISMATCH",
                "source_type",
                f"Record claims source_type {raw!r} but this adapter is "
                f"{ctx.expected_source_type.value}.",
            )
        ]
    return []


def check_numeric_value(value: Any) -> tuple[float | None, list]:
    """Accept only finite int/float. ``0`` is a legitimate value and is preserved."""
    if value is None:
        return None, [_issue("MISSING_VALUE", "value", "value is required.")]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, [_issue("NON_NUMERIC_VALUE", "value", "value must be a number.")]
    try:
        number = float(value)
    except OverflowError:
        return None, [_issue("NON_FINITE_VALUE", "value", "value is out of numeric range.")]
    if not math.isfinite(number):
        return None, [_issue("NON_FINITE_VALUE", "value", "value must be finite (not NaN/inf).")]
    return number, []


def _machine_issues(
    machine_id: str | None, ctx: ValidationContext, field_name: str = "machine_id"
) -> list:
    if machine_id is None:
        return []
    if machine_id not in ctx.machines:
        return [
            _issue(
                "UNKNOWN_MACHINE",
                field_name,
                f"Machine {machine_id!r} is not registered.",
            )
        ]
    if not ctx.machines[machine_id]:
        return [
            _issue(
                "MACHINE_DISABLED",
                field_name,
                f"Monitoring is disabled for machine {machine_id!r}.",
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# Record validators
# --------------------------------------------------------------------------- #


def validate_state_payload(payload: Mapping[str, Any], ctx: ValidationContext) -> ValidationOutcome:
    issues: list[ValidationIssue] = []
    machine_id, errs = check_identifier(payload.get("machine_id"), "machine_id", "MISSING_MACHINE_ID")
    issues += errs
    issues += _machine_issues(machine_id, ctx)

    state: ObservedState | None = None
    raw_state = payload.get("state")
    if raw_state is None or (isinstance(raw_state, str) and not raw_state.strip()):
        issues.append(_issue("MISSING_STATE", "state", "state is required."))
    elif not isinstance(raw_state, str):
        issues.append(_issue("INVALID_STATE", "state", "state must be text."))
    else:
        try:
            state = ObservedState(raw_state.strip().upper())
        except ValueError:
            hint = (
                " DISCONNECTED is a monitoring condition and is reported through source health."
                if raw_state.strip().upper() == "DISCONNECTED"
                else ""
            )
            issues.append(
                _issue(
                    "INVALID_STATE",
                    "state",
                    f"state must be one of RUNNING, STOPPED or UNKNOWN.{hint}",
                )
            )

    event_time, errs = check_event_time(payload.get("event_time"), ctx)
    issues += errs
    issues += check_source_type(payload, ctx)
    event_id, errs = check_optional_event_id(payload.get("source_event_id"))
    issues += errs
    metadata, errs = check_optional_metadata(payload.get("metadata"))
    issues += errs

    sequence = payload.get("source_sequence")
    if sequence is not None and (
        isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0
    ):
        issues.append(
            _issue(
                "INVALID_SEQUENCE",
                "source_sequence",
                "source_sequence must be a non-negative integer.",
            )
        )
        sequence = None

    machine_ref = payload.get("machine_id") if isinstance(payload.get("machine_id"), str) else None
    if issues or machine_id is None or state is None or event_time is None:
        return ValidationOutcome(None, tuple(issues), machine_ref)
    record = StateRecord(
        machine_id=machine_id,
        state=state,
        event_time=event_time,
        source_type=ctx.expected_source_type,
        source_event_id=event_id,
        source_sequence=sequence,
        scenario_id=_optional_text(payload.get("scenario_id")),
        metadata=metadata,
    )
    return ValidationOutcome(record, (), machine_ref)


def validate_sensor_payload(
    payload: Mapping[str, Any], ctx: ValidationContext
) -> ValidationOutcome:
    issues: list[ValidationIssue] = []
    machine_id, errs = check_identifier(payload.get("machine_id"), "machine_id", "MISSING_MACHINE_ID")
    issues += errs
    machine_known_ok = machine_id is not None
    machine_issues = _machine_issues(machine_id, ctx)
    issues += machine_issues

    sensor_code, errs = check_identifier(payload.get("sensor_id"), "sensor_id", "MISSING_SENSOR_ID")
    issues += errs

    spec: SensorSpec | None = None
    if machine_known_ok and sensor_code is not None and not machine_issues:
        spec = ctx.sensors.get((machine_id, sensor_code))  # type: ignore[arg-type]
        if spec is None:
            issues.append(
                _issue(
                    "UNKNOWN_SENSOR",
                    "sensor_id",
                    f"Sensor {sensor_code!r} is not defined for machine {machine_id!r}.",
                )
            )

    value, errs = check_numeric_value(payload.get("value"))
    issues += errs
    event_time, errs = check_event_time(payload.get("event_time"), ctx)
    issues += errs
    issues += check_source_type(payload, ctx)
    event_id, errs = check_optional_event_id(payload.get("source_event_id"))
    issues += errs
    metadata, errs = check_optional_metadata(payload.get("metadata"))
    issues += errs

    # Unit handling: never invent a unit, never convert.
    supplied_unit: str | None = None
    raw_unit = payload.get("unit")
    if raw_unit is not None:
        if not isinstance(raw_unit, str):
            issues.append(_issue("INVALID_UNIT", "unit", "unit must be text."))
        elif raw_unit.strip():
            supplied_unit = raw_unit.strip()
    stored_unit = supplied_unit
    if spec is not None:
        if spec.unit is not None and supplied_unit is not None and supplied_unit != spec.unit:
            issues.append(
                _issue(
                    "UNIT_MISMATCH",
                    "unit",
                    f"Unit {supplied_unit!r} does not match the sensor definition ({spec.unit!r}).",
                )
            )
        elif spec.unit is not None and supplied_unit is None:
            stored_unit = spec.unit
            metadata = {**metadata, "unit_from_definition": True}
        elif spec.unit is None:
            metadata = {**metadata, "unit_verified": False}

    machine_ref = payload.get("machine_id") if isinstance(payload.get("machine_id"), str) else None
    if issues or spec is None or value is None or event_time is None or machine_id is None:
        return ValidationOutcome(None, tuple(issues), machine_ref)
    record = SensorRecord(
        machine_id=machine_id,
        sensor_code=spec.code,
        value=value,
        unit=stored_unit,
        event_time=event_time,
        source_type=ctx.expected_source_type,
        source_event_id=event_id,
        scenario_id=_optional_text(payload.get("scenario_id")),
        metadata=metadata,
    )
    return ValidationOutcome(record, (), machine_ref)


def validate_health_payload(payload: Mapping[str, Any], ctx: ValidationContext) -> ValidationOutcome:
    issues: list[ValidationIssue] = []
    health: SourceHealth | None = None
    raw = payload.get("health")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        issues.append(_issue("MISSING_HEALTH", "health", "health is required."))
    elif not isinstance(raw, str):
        issues.append(_issue("INVALID_HEALTH", "health", "health must be text."))
    else:
        try:
            health = SourceHealth(raw.strip().upper())
        except ValueError:
            issues.append(
                _issue(
                    "INVALID_HEALTH",
                    "health",
                    "health must be HEALTHY, DEGRADED, UNAVAILABLE or UNKNOWN.",
                )
            )
    event_time, errs = check_event_time(payload.get("event_time"), ctx)
    issues += errs
    issues += check_source_type(payload, ctx)
    event_id, errs = check_optional_event_id(payload.get("source_event_id"))
    issues += errs
    detail = payload.get("detail")
    if detail is not None and (not isinstance(detail, str) or len(detail) > 500):
        issues.append(_issue("INVALID_DETAIL", "detail", "detail must be text up to 500 characters."))
        detail = None
    if issues or health is None or event_time is None:
        return ValidationOutcome(None, tuple(issues), None)
    record = HealthRecord(
        health=health,
        event_time=event_time,
        source_type=ctx.expected_source_type,
        detail=detail,
        source_event_id=event_id,
        scenario_id=_optional_text(payload.get("scenario_id")),
    )
    return ValidationOutcome(record, (), None)


def validate_record(raw: RawRecord, ctx: ValidationContext) -> ValidationOutcome:
    """Validate and normalise one raw adapter record."""
    if not isinstance(raw.payload, Mapping):
        return ValidationOutcome(
            None, (_issue("INVALID_PAYLOAD", "payload", "Record payload must be an object."),)
        )
    if raw.record_type == RecordType.MACHINE_STATE:
        return validate_state_payload(raw.payload, ctx)
    if raw.record_type == RecordType.SENSOR_READING:
        return validate_sensor_payload(raw.payload, ctx)
    if raw.record_type == RecordType.SOURCE_HEALTH:
        return validate_health_payload(raw.payload, ctx)
    return ValidationOutcome(
        None,
        (
            _issue(
                "UNSUPPORTED_RECORD_TYPE",
                "record_type",
                f"Record type {raw.record_type!r} is not supported.",
            ),
        ),
    )


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:100]
    return None


# --------------------------------------------------------------------------- #
# Form-level validators (machines, maintenance)
# --------------------------------------------------------------------------- #


def validate_machine_fields(
    machine_id: Any,
    name: Any,
    machine_type: Any,
    location: Any = None,
    description: Any = None,
    *,
    require_id: bool = True,
) -> tuple[dict[str, str | None], list[ValidationIssue]]:
    """Validate and trim machine registration/edit fields."""
    issues: list[ValidationIssue] = []
    clean: dict[str, str | None] = {}

    if require_id:
        mid, errs = check_identifier(machine_id, "machine_id", "REQUIRED")
        issues += errs
        clean["machine_id"] = mid

    clean["name"] = _required_text(name, "name", MAX_NAME_LEN, issues)
    clean["machine_type"] = _required_text(machine_type, "machine_type", MAX_NAME_LEN, issues)
    clean["location"] = _optional_bounded_text(location, "location", MAX_NAME_LEN, issues)
    clean["description"] = _optional_bounded_text(description, "description", MAX_TEXT_LEN, issues)
    return clean, issues


def validate_maintenance_fields(
    *,
    event_type: Any,
    start_time: datetime | None,
    end_time: datetime | None,
    description: Any,
    finding: Any = None,
    responsible: Any = None,
    notes: Any = None,
    now: datetime,
    future_tolerance: timedelta = timedelta(minutes=5),
) -> tuple[dict[str, Any], list[ValidationIssue]]:
    """Validate maintenance record fields. Times must already be aware UTC datetimes."""
    issues: list[ValidationIssue] = []
    clean: dict[str, Any] = {}

    etype = _required_text(event_type, "event_type", MAX_NAME_LEN, issues)
    if etype is not None and etype not in MAINTENANCE_TYPES:
        issues.append(_issue("INVALID_EVENT_TYPE", "event_type", "Select a supported event type."))
        etype = None
    clean["event_type"] = etype

    if start_time is None:
        issues.append(_issue("REQUIRED", "start_time", "Start time is required."))
    elif start_time.tzinfo is None:
        issues.append(_issue("INVALID_TIMESTAMP", "start_time", "Start time must be timezone-aware."))
        start_time = None
    elif start_time > now + future_tolerance:
        issues.append(
            _issue("TIMESTAMP_IN_FUTURE", "start_time", "Start time cannot be in the future.")
        )
    clean["start_time"] = start_time

    if end_time is not None:
        if end_time.tzinfo is None:
            issues.append(
                _issue("INVALID_TIMESTAMP", "end_time", "Completion time must be timezone-aware.")
            )
            end_time = None
        elif end_time > now + future_tolerance:
            issues.append(
                _issue("TIMESTAMP_IN_FUTURE", "end_time", "Completion time cannot be in the future.")
            )
        elif start_time is not None and end_time < start_time:
            issues.append(
                _issue(
                    "END_BEFORE_START",
                    "end_time",
                    "Completion time cannot be earlier than the start time.",
                )
            )
    clean["end_time"] = end_time

    clean["description"] = _required_text(description, "description", MAX_TEXT_LEN, issues)
    clean["finding"] = _optional_bounded_text(finding, "finding", MAX_TEXT_LEN, issues)
    clean["responsible"] = _optional_bounded_text(responsible, "responsible", MAX_NAME_LEN, issues)
    clean["notes"] = _optional_bounded_text(notes, "notes", MAX_TEXT_LEN, issues)
    return clean, issues


def _required_text(value: Any, field_name: str, max_len: int, issues: list) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        issues.append(_issue("REQUIRED", field_name, f"{field_name.replace('_', ' ')} is required."))
        return None
    if not isinstance(value, str):
        issues.append(_issue("INVALID_TEXT", field_name, f"{field_name} must be text."))
        return None
    clean = value.strip()
    if len(clean) > max_len:
        issues.append(
            _issue("TOO_LONG", field_name, f"{field_name} must be at most {max_len} characters.")
        )
        return None
    return clean


def _optional_bounded_text(value: Any, field_name: str, max_len: int, issues: list) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        issues.append(_issue("INVALID_TEXT", field_name, f"{field_name} must be text."))
        return None
    clean = value.strip()
    if not clean:
        return None
    if len(clean) > max_len:
        issues.append(
            _issue("TOO_LONG", field_name, f"{field_name} must be at most {max_len} characters.")
        )
        return None
    return clean
