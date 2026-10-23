"""Shared test helpers. Database-backed tests need SQLAlchemy; without it they are SKIPPED
(never silently passed). tests/test_environment.py fails if required packages are missing."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import sqlalchemy  # noqa: F401

    HAS_SQLALCHEMY = True
except ImportError:  # pragma: no cover - depends on environment
    HAS_SQLALCHEMY = False

requires_sqlalchemy = unittest.skipUnless(HAS_SQLALCHEMY, "SQLAlchemy is not installed in this environment")

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def minutes_ago(n: int, now: datetime = NOW) -> datetime:
    return now - timedelta(minutes=n)


class DbTestCase(unittest.TestCase):
    """Creates a fresh temporary SQLite database for every test."""

    def setUp(self) -> None:
        from src.repositories.database import Database

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        self.db = Database(self.dir / "test.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.dispose()
        self._tmp.cleanup()

    # -- factories (each call uses its own committed unit of work) ---------- #
    def make_source(self, session) -> object:
        from src.services.simulation_service import SimulationService
        from src.core.settings_rules import RuntimeSettings

        return SimulationService(session, RuntimeSettings(), lambda: NOW).get_or_create_source()

    def make_machine(self, session, source, machine_id: str = "M1", **kw):
        from src.services.machine_service import MachineService

        return MachineService(session, lambda: NOW).create(
            machine_id=machine_id, name=kw.get("name", f"Machine {machine_id}"),
            machine_type=kw.get("machine_type", "Test type"), source_id=source.id,
        )

    def make_sensor(self, session, machine_id: str = "M1", code: str = "S1", unit="u", upper=75.0, lower=None):
        from src.services.sensor_service import SensorService

        return SensorService(session, lambda: NOW).create_definition(
            machine_id, code, "Test signal", unit=unit, lower_threshold=lower, upper_threshold=upper
        )

    def ingestion(self, session, source, settings=None):
        from src.core.settings_rules import RuntimeSettings
        from src.services.ingestion_service import IngestionService

        return IngestionService(session, source, settings or RuntimeSettings(), lambda: NOW)


def state_raw(machine: str, state: str, when: datetime, event_id: str | None = None, **extra):
    from src.models.schemas import RawRecord

    payload = {"machine_id": machine, "state": state, "event_time": iso(when), "source_type": "SIMULATED"}
    if event_id:
        payload["source_event_id"] = event_id
    payload.update(extra)
    return RawRecord("machine_state", payload)


def sensor_raw(machine: str, code: str, value, when: datetime, event_id: str | None = None, unit="u", **extra):
    from src.models.schemas import RawRecord

    payload = {"machine_id": machine, "sensor_id": code, "value": value, "event_time": iso(when),
               "source_type": "SIMULATED"}
    if unit is not None:
        payload["unit"] = unit
    if event_id:
        payload["source_event_id"] = event_id
    payload.update(extra)
    return RawRecord("sensor_reading", payload)


def health_raw(health: str, when: datetime, event_id: str | None = None):
    from src.models.schemas import RawRecord

    payload = {"health": health, "event_time": iso(when), "source_type": "SIMULATED"}
    if event_id:
        payload["source_event_id"] = event_id
    return RawRecord("source_health", payload)
