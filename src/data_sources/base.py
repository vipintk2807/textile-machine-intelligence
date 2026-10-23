"""Adapter contract for every machine-data source.

Pipeline: Source -> Adapter -> Validation -> Normalisation -> Duplicate/ordering
checks -> Application services -> SQLite.

An adapter is **read-only**: it only produces records. It never issues commands to
a machine or controller, never touches the database and never imports UI code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from src.models.schemas import ReadResult, SourceHealthReport, SourceType


class SourceAdapter(ABC):
    """Interface that the simulator (and any future approved adapter) implements."""

    source_type: SourceType
    name: str

    @abstractmethod
    def connect(self) -> None:
        """Prepare the source (lightweight for the simulator)."""

    @abstractmethod
    def read_records(self, now: datetime, limit: int | None = None) -> ReadResult:
        """Return a batch of raw records, or a structured error.

        A failure MUST be reported through ``ReadResult.error``; returning an empty
        batch to hide a failure is a contract violation.
        """

    @abstractmethod
    def get_health(self) -> SourceHealthReport:
        """Report availability and the time of the last successful interaction."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
