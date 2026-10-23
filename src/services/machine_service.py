"""Machine registry operations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.validation import validate_machine_fields
from src.models.orm import DataSource, Machine
from src.models.schemas import DuplicateError, NotFoundError, ValidationFailed, ValidationIssue
from src.utils.time_utils import utc_now


class MachineService:
    def __init__(self, session: Session, clock: Callable[[], datetime] = utc_now):
        self.session = session
        self.clock = clock

    def _check_source(self, source_id: int) -> None:
        source = self.session.get(DataSource, source_id)
        if source is None or not source.enabled:
            raise ValidationFailed(
                [ValidationIssue("INVALID_SOURCE", "source_id", "Select an available monitoring source.")]
            )

    def create(
        self,
        *,
        machine_id: str,
        name: str,
        machine_type: str,
        source_id: int,
        location: str | None = None,
        description: str | None = None,
    ) -> Machine:
        clean, issues = validate_machine_fields(machine_id, name, machine_type, location, description)
        if issues:
            raise ValidationFailed(issues)
        self._check_source(source_id)
        exists = self.session.scalar(
            select(func.count()).select_from(Machine).where(
                func.lower(Machine.machine_id) == clean["machine_id"].lower()
            )
        )
        if exists:
            raise DuplicateError(f"A machine with ID '{clean['machine_id']}' already exists.")
        now = self.clock()
        machine = Machine(
            machine_id=clean["machine_id"],
            name=clean["name"],
            machine_type=clean["machine_type"],
            location=clean["location"],
            description=clean["description"],
            source_id=source_id,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        self.session.add(machine)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise DuplicateError(f"A machine with ID '{clean['machine_id']}' already exists.") from exc
        return machine

    def get(self, machine_id: str) -> Machine:
        machine = self.session.get(Machine, machine_id)
        if machine is None:
            raise NotFoundError(f"Machine '{machine_id}' was not found.")
        return machine

    def list(
        self,
        *,
        search: str | None = None,
        machine_type: str | None = None,
        location: str | None = None,
        enabled: bool | None = None,
        source_id: int | None = None,
    ) -> list[Machine]:
        stmt = select(Machine).order_by(Machine.machine_id)
        if search:
            like = f"%{search.strip().lower()}%"
            stmt = stmt.where(
                func.lower(Machine.machine_id).like(like) | func.lower(Machine.name).like(like)
            )
        if machine_type:
            stmt = stmt.where(Machine.machine_type == machine_type)
        if location:
            stmt = stmt.where(Machine.location == location)
        if enabled is not None:
            stmt = stmt.where(Machine.enabled == enabled)
        if source_id is not None:
            stmt = stmt.where(Machine.source_id == source_id)
        return list(self.session.scalars(stmt))

    def update(
        self,
        machine_id: str,
        *,
        name: str,
        machine_type: str,
        location: str | None = None,
        description: str | None = None,
        source_id: int | None = None,
    ) -> Machine:
        """Update descriptive fields. Historical events keep their original source."""
        machine = self.get(machine_id)
        clean, issues = validate_machine_fields(
            machine_id, name, machine_type, location, description, require_id=False
        )
        if issues:
            raise ValidationFailed(issues)
        if source_id is not None and source_id != machine.source_id:
            self._check_source(source_id)
            machine.source_id = source_id
        machine.name = clean["name"]
        machine.machine_type = clean["machine_type"]
        machine.location = clean["location"]
        machine.description = clean["description"]
        machine.updated_at = self.clock()
        self.session.flush()
        return machine

    def set_enabled(self, machine_id: str, enabled: bool) -> Machine:
        """Deactivate/activate monitoring. History is never deleted."""
        machine = self.get(machine_id)
        machine.enabled = enabled
        machine.updated_at = self.clock()
        self.session.flush()
        return machine

    def distinct_values(self, column: str) -> list[str]:
        col = {"machine_type": Machine.machine_type, "location": Machine.location}[column]
        rows = self.session.scalars(select(col).where(col.is_not(None)).distinct().order_by(col))
        return [r for r in rows if r]
