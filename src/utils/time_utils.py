"""Timestamp handling, time-zone conversion and half-open reporting windows.

Conventions
-----------
* Internal timestamps are timezone-aware UTC.
* Reporting windows are half-open: ``[start, end)``.
* Naive timestamps are never silently interpreted: a policy must be chosen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_YEAR = 2000
MAX_YEAR = 2100


class NaivePolicy(StrEnum):
    """What to do with a timestamp that carries no UTC offset."""

    REJECT = "reject"
    ASSUME_UTC = "assume_utc"
    ASSUME_SOURCE_TZ = "assume_source_tz"


class TimestampError(ValueError):
    """Raised for an unusable timestamp. ``code`` is a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_timestamp(
    value: Any,
    naive_policy: NaivePolicy = NaivePolicy.REJECT,
    source_tz: tzinfo | None = None,
) -> datetime:
    """Parse an ISO-8601 string or datetime into an aware UTC datetime.

    Raises :class:`TimestampError` with one of the codes ``MISSING_TIMESTAMP``,
    ``MALFORMED_TIMESTAMP``, ``NAIVE_TIMESTAMP_NOT_ALLOWED`` or
    ``TIMESTAMP_OUT_OF_RANGE``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise TimestampError("MISSING_TIMESTAMP", "Timestamp is missing.")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise TimestampError(
                "MALFORMED_TIMESTAMP", "Timestamp is not valid ISO-8601."
            ) from exc
    else:
        raise TimestampError(
            "MALFORMED_TIMESTAMP",
            "Timestamp must be an ISO-8601 string or a datetime.",
        )

    if dt.tzinfo is None or dt.utcoffset() is None:
        if naive_policy is NaivePolicy.REJECT:
            raise TimestampError(
                "NAIVE_TIMESTAMP_NOT_ALLOWED",
                "Timestamp has no UTC offset and this source's time zone is not defined.",
            )
        if naive_policy is NaivePolicy.ASSUME_UTC:
            dt = dt.replace(tzinfo=UTC)
        else:
            if source_tz is None:
                raise TimestampError(
                    "NAIVE_TIMESTAMP_NOT_ALLOWED",
                    "Naive timestamp policy requires a source time zone.",
                )
            dt = dt.replace(tzinfo=source_tz)

    try:
        dt_utc = dt.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise TimestampError("TIMESTAMP_OUT_OF_RANGE", "Timestamp is out of range.") from exc
    if not (MIN_YEAR <= dt_utc.year <= MAX_YEAR):
        raise TimestampError(
            "TIMESTAMP_OUT_OF_RANGE",
            f"Timestamp year must be between {MIN_YEAR} and {MAX_YEAR}.",
        )
    return dt_utc


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` as aware UTC; naive datetimes are rejected, not guessed."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Naive datetime is not allowed; supply an aware datetime.")
    return dt.astimezone(UTC)


def to_iso_z(dt: datetime) -> str:
    """ISO-8601 UTC string with a trailing ``Z``."""
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


def floor_to_seconds(dt: datetime, seconds: int) -> datetime:
    """Floor an aware datetime to a multiple of ``seconds`` since the Unix epoch."""
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    epoch = int(ensure_utc(dt).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


# --------------------------------------------------------------------------- #
# Time zones
# --------------------------------------------------------------------------- #

COMMON_TIMEZONES: tuple[str, ...] = (
    "Asia/Kolkata",
    "UTC",
    "Asia/Dhaka",
    "Asia/Colombo",
    "Asia/Karachi",
    "Asia/Dubai",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Los_Angeles",
)


def get_zone(name: str) -> ZoneInfo:
    """Return a ZoneInfo or raise ``ValueError`` with a friendly message.

    On Windows the IANA database comes from the ``tzdata`` package (listed in
    requirements.txt).
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Time zone name is empty.")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(
            f"Unknown time zone '{name}'. Use an IANA name such as 'Asia/Kolkata' or 'UTC' "
            "(on Windows, make sure the 'tzdata' package is installed)."
        ) from exc


def format_local(dt: datetime | None, tz_name: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format an aware datetime in the reporting time zone; ``None`` becomes an em dash."""
    if dt is None:
        return "—"
    return ensure_utc(dt).astimezone(get_zone(tz_name)).strftime(fmt)


def local_naive_to_utc(naive: datetime, tz_name: str) -> datetime:
    """Interpret a naive wall-clock time in ``tz_name`` and convert it to UTC.

    * A wall-clock time that does not exist (spring-forward gap) raises ``ValueError``.
    * An ambiguous wall-clock time (fall-back overlap) resolves to the *first*
      occurrence (``fold=0``). This is documented behaviour.
    """
    if naive.tzinfo is not None:
        raise ValueError("Expected a naive datetime for local conversion.")
    zone = get_zone(tz_name)
    local = naive.replace(tzinfo=zone, fold=0)
    utc = local.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError(
            f"{naive:%Y-%m-%d %H:%M} does not exist in {tz_name} (daylight-saving gap)."
        )
    return utc


# --------------------------------------------------------------------------- #
# Intervals and windows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Interval:
    """A half-open interval ``[start, end)`` of aware UTC datetimes (may be empty)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        ensure_utc(self.start)
        ensure_utc(self.end)
        if self.end < self.start:
            raise ValueError("Interval end must not be earlier than start.")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Window:
    """A reporting window ``[start, end)``; start must be strictly before end."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        ensure_utc(self.start)
        ensure_utc(self.end)
        if not self.start < self.end:
            raise ValueError("Reporting window start must be before its end.")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, t: datetime) -> bool:
        """True when ``start <= t < end`` (start inclusive, end exclusive)."""
        return self.start <= t < self.end

    def clip(self, start: datetime, end: datetime) -> Interval | None:
        """Clip ``[start, end)`` to this window; ``None`` when nothing remains."""
        s = max(start, self.start)
        e = min(end, self.end)
        return Interval(s, e) if s < e else None


def local_range_to_window(start_local: datetime, end_local: datetime, tz_name: str) -> Window:
    """Build a UTC window from naive wall-clock start/end values in ``tz_name``."""
    start = local_naive_to_utc(start_local, tz_name)
    end = local_naive_to_utc(end_local, tz_name)
    if not start < end:
        raise ValueError("The start must be before the end.")
    return Window(start, end)


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping or touching intervals into a sorted, disjoint list."""
    merged: list[Interval] = []
    for iv in sorted((i for i in intervals if i.start < i.end), key=lambda i: i.start):
        if merged and iv.start <= merged[-1].end:
            if iv.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, iv.end)
        else:
            merged.append(iv)
    return merged


def format_duration(td: timedelta | None) -> str:
    """Compact human-readable duration such as ``1h 05m 30s``; ``None`` becomes an em dash."""
    if td is None:
        return "—"
    total = int(td.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{sign}{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{sign}{minutes}m {seconds:02d}s"
    return f"{sign}{seconds}s"
