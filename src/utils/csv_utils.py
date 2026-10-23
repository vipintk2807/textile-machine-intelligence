"""CSV serialisation with spreadsheet formula-injection mitigation.

Mitigation (documented in README): any *text* cell whose first character is one
of ``=``, ``+``, ``-``, ``@``, TAB or CR/LF is written with a leading single
quote (``'``) so that Excel and similar programs treat it as text rather than a
formula. Numeric values (int/float) are written unchanged, so a genuine negative
reading such as ``-3.5`` is *not* altered. The stored database value is never
modified; only the exported representation is.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from src.models.schemas import ExportError
from src.utils.time_utils import to_iso_z

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def sanitize_text_cell(text: str) -> str:
    """Neutralise a text cell that a spreadsheet could interpret as a formula."""
    if text and text.startswith(_DANGEROUS_PREFIXES):
        return "'" + text
    return text


def format_cell(value: Any) -> str | int | float:
    """Convert a Python value into a CSV-safe cell.

    ``None`` becomes an empty string (never zero), datetimes become ISO-8601 UTC,
    bools become ``true``/``false`` and non-finite floats become an empty string.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return to_iso_z(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else ""
    return sanitize_text_cell(str(value))


def rows_to_csv(
    columns: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
    max_rows: int = 200_000,
) -> str:
    """Serialise ``rows`` to CSV text with a header row.

    Raises :class:`ExportError` if more than ``max_rows`` rows are supplied -- an
    export is never silently truncated.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([sanitize_text_cell(c) for c in columns])
    count = 0
    try:
        for row in rows:
            count += 1
            if count > max_rows:
                raise ExportError(
                    f"The export has more than {max_rows:,} rows. Narrow the filters and try again."
                )
            writer.writerow([format_cell(row.get(c)) for c in columns])
    except ExportError:
        raise
    except Exception as exc:  # pragma: no cover - defensive wrapper
        raise ExportError(f"The export could not be created: {exc}") from exc
    return buffer.getvalue()
