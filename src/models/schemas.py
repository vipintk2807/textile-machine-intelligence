"""Enumerations, data contracts and exceptions shared by every layer.

This module is deliberately standard-library only so that the core business
logic, the simulator and the validators can be imported and tested without the
UI or database stacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class MachineStatus(StrEnum):
    """Effective machine status shown to users."""

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"
    DISCONNECTED = "DISCONNECTED"


class ObservedState(StrEnum):
    """States a data source may *report* for a machine.

    DISCONNECTED is a monitoring condition (source health), never a state that a
    source reports about the physical machine, so it is intentionally absent.
    """

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"


class SourceHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class SourceType(StrEnum):
    SIMULATED = "SIMULATED"
    REAL_SOURCE = "REAL_SOURCE"  # reserved: no real adapter exists in this release


class QualityStatus(StrEnum):
    """Quality of an *accepted* record. Rejected records are never stored as readings."""

    VALID = "VALID"
    DELAYED = "DELAYED"  # valid, but arrived out of event-time order


class RecordType(StrEnum):
    MACHINE_STATE = "machine_state"
    SENSOR_READING = "sensor_reading"
    SOURCE_HEALTH = "source_health"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AnomalyStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AnomalyCategory(StrEnum):
    SENSOR_THRESHOLD = "SENSOR_THRESHOLD"
    STATE_DURATION = "STATE_DURATION"
    DATA_QUALITY = "DATA_QUALITY"
    CONNECTIVITY = "CONNECTIVITY"


class RuleCode(StrEnum):
    SENSOR_ABOVE_UPPER = "SENSOR_ABOVE_UPPER"
    SENSOR_BELOW_LOWER = "SENSOR_BELOW_LOWER"
    SENSOR_STALE = "SENSOR_STALE"
    STATE_DURATION_EXCEEDED = "STATE_DURATION_EXCEEDED"
    RECORD_REJECTED = "RECORD_REJECTED"
    RECORD_OUT_OF_ORDER = "RECORD_OUT_OF_ORDER"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class IntervalCategory(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"
    GAP = "GAP"


class GapReason(StrEnum):
    NO_OBSERVATIONS = "NO_OBSERVATIONS"
    BEFORE_FIRST_OBSERVATION = "BEFORE_FIRST_OBSERVATION"
    EVIDENCE_EXPIRED = "EVIDENCE_EXPIRED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class FreshnessLabel(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    NO_DATA = "NO_DATA"


# Illustrative maintenance categories. These are a convenience list for the
# form, not an industry standard; "Other" is always permitted.
MAINTENANCE_TYPES: tuple[str, ...] = (
    "Inspection",
    "Preventive maintenance",
    "Corrective repair",
    "Cleaning / lubrication",
    "Calibration",
    "Part replacement",
    "Other",
)

# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found while validating a record or a form value."""

    code: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message} [{self.code}]"


class AppError(Exception):
    """Base class for expected, user-presentable application errors."""


class ValidationFailed(AppError):
    def __init__(self, issues: list[ValidationIssue] | tuple[ValidationIssue, ...]):
        self.issues: tuple[ValidationIssue, ...] = tuple(issues)
        super().__init__("; ".join(str(i) for i in self.issues) or "Validation failed")

    @property
    def field_errors(self) -> dict[str, str]:
        """First message per field, convenient for form rendering."""
        out: dict[str, str] = {}
        for issue in self.issues:
            out.setdefault(issue.field, issue.message)
        return out


class NotFoundError(AppError):
    pass


class DuplicateError(AppError):
    pass


class PersistenceError(AppError):
    """A database operation failed; nothing from the failed unit of work was saved."""


class ExportError(AppError):
    pass


class ConfigError(AppError):
    pass


class RuleConfigError(AppError):
    pass


class StateTransitionError(AppError):
    """An anomaly status change that is not allowed."""


# --------------------------------------------------------------------------- #
# Adapter / ingestion contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawRecord:
    """A record exactly as the adapter produced it, before validation."""

    record_type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class SourceError:
    """Structured failure from an adapter. Never confused with an empty result."""

    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class ReadResult:
    """Outcome of one adapter read: either records (possibly none) or an error."""

    records: tuple[RawRecord, ...] = ()
    error: SourceError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def is_valid_empty(self) -> bool:
        return self.error is None and not self.records


@dataclass(frozen=True)
class SourceHealthReport:
    health: SourceHealth
    last_success: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True)
class StateRecord:
    machine_id: str
    state: ObservedState
    event_time: datetime
    source_type: SourceType
    source_event_id: str | None = None
    source_sequence: int | None = None
    scenario_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SensorRecord:
    machine_id: str
    sensor_code: str
    value: float
    unit: str | None
    event_time: datetime
    source_type: SourceType
    source_event_id: str | None = None
    scenario_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthRecord:
    health: SourceHealth
    event_time: datetime
    source_type: SourceType
    detail: str | None = None
    source_event_id: str | None = None
    scenario_id: str | None = None


@dataclass(frozen=True)
class SensorSpec:
    """The slice of a sensor definition the validator needs."""

    id: int
    machine_id: str
    code: str
    unit: str | None


@dataclass(frozen=True)
class ValidationOutcome:
    record: StateRecord | SensorRecord | HealthRecord | None
    issues: tuple[ValidationIssue, ...] = ()
    machine_ref: str | None = None  # machine id as supplied, if it was a usable string

    @property
    def accepted(self) -> bool:
        return self.record is not None and not self.issues
