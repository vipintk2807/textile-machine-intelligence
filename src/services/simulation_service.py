"""Lifecycle of the simulated source: initialise, poll, interrupt, reset.

Safety rules
------------
* Initialisation is an explicit, one-time action. It refuses to run again until the
  simulated data has been reset, so page reruns can never duplicate history.
* ``poll`` extends the stream from a persisted cursor; event ids are deterministic,
  so even a lost cursor only produces ignorable duplicates.
* ``reset_simulated_data`` removes ONLY records whose provenance is SIMULATED
  (state events, sensor readings, source-health events, quarantined records and
  anomalies). Machines, sensor definitions and user-entered maintenance records are
  never deleted; maintenance links to removed anomalies are cleared.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from src.core.settings_rules import RuntimeSettings
from src.data_sources.simulated_source import (
    DEMO_SCRIPT,
    SIM_SOURCE_NAME,
    SimulatedSource,
    SimulationConfig,
    default_sensor_definitions,
)
from src.models.orm import (
    Anomaly,
    DataSource,
    Machine,
    MachineStateEvent,
    MaintenanceEvent,
    RejectedRecord,
    SensorReading,
    SourceHealthEvent,
)
from src.models.schemas import SourceType, ValidationFailed, ValidationIssue
from src.services.ingestion_service import CycleReport, IngestionService
from src.services.machine_service import MachineService
from src.services.sensor_service import SensorService
from src.utils.time_utils import Interval, floor_to_seconds, parse_timestamp, to_iso_z, utc_now

RESET_PHRASE = "RESET SIMULATED DATA"
DEMO_MACHINE_TYPE = "Simulated (not a real machine)"


class SimulationService:
    def __init__(
        self,
        session: Session,
        settings: RuntimeSettings,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.session = session
        self.settings = settings
        self.clock = clock

    # ------------------------------ source row ------------------------------ #

    def get_or_create_source(self) -> DataSource:
        source = self.session.scalar(
            select(DataSource).where(DataSource.source_type == SourceType.SIMULATED.value)
        )
        if source is None:
            source = DataSource(
                name=SIM_SOURCE_NAME,
                source_type=SourceType.SIMULATED.value,
                enabled=True,
                configuration="{}",
                created_at=self.clock(),
            )
            self.session.add(source)
            self.session.flush()
        return source

    @staticmethod
    def _config(source: DataSource) -> dict:
        try:
            value = json.loads(source.configuration or "{}")
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}

    def _save_config(self, source: DataSource, config: dict) -> None:
        source.configuration = json.dumps(config)
        self.session.flush()

    def is_initialized(self) -> bool:
        return bool(self._config(self.get_or_create_source()).get("initialized"))

    def status(self) -> dict:
        source = self.get_or_create_source()
        cfg = self._config(source)
        return {
            "initialized": bool(cfg.get("initialized")),
            "seed": cfg.get("seed"),
            "anchor": cfg.get("anchor"),
            "cursor": cfg.get("cursor"),
            "interruptions": cfg.get("outage_windows", []),
        }

    # ------------------------------- adapters -------------------------------- #

    def _enabled_machine_ids(self, source: DataSource) -> list[str]:
        return sorted(
            self.session.scalars(
                select(Machine.machine_id).where(Machine.source_id == source.id, Machine.enabled.is_(True))
            )
        )

    def _provision_sensors(self, machine_ids: list[str]) -> None:
        sensors = SensorService(self.session, self.clock)
        for machine_id in machine_ids:
            sensors.ensure_definitions(machine_id, default_sensor_definitions())

    def _build_adapter(self, source: DataSource, cfg: dict, machine_ids: list[str]) -> SimulatedSource:
        sim_cfg = SimulationConfig(
            anchor=parse_timestamp(cfg["anchor"]),
            seed=int(cfg["seed"]),
            step_seconds=int(cfg.get("step_seconds", 60)),
            scenario_id=str(cfg["scenario_id"]),
        )
        extra = [
            Interval(parse_timestamp(a), parse_timestamp(b)) for a, b in cfg.get("outage_windows", [])
        ]
        adapter = SimulatedSource(
            sim_cfg, DEMO_SCRIPT, machine_ids,
            start_after=parse_timestamp(cfg["cursor"]), extra_outages=extra, name=source.name,
        )
        adapter.connect()
        return adapter

    # ------------------------------- actions --------------------------------- #

    def initialize_demo(
        self, *, seed: int, machine_count: int, lookback_hours: int, now: datetime | None = None
    ) -> CycleReport:
        """Create demo machines and back-fill clearly labelled synthetic history (once)."""
        now = now or self.clock()
        source = self.get_or_create_source()
        cfg = self._config(source)
        if cfg.get("initialized"):
            raise ValidationFailed(
                [ValidationIssue(
                    "ALREADY_INITIALIZED", "simulation",
                    "Simulated data is already initialised. Use 'Poll' to extend it, or reset "
                    "the simulated data first.",
                )]
            )
        anchor = floor_to_seconds(now, 60)
        machines = MachineService(self.session, self.clock)
        ids = []
        for i in range(1, machine_count + 1):
            machine_id = f"SIM-M{i:02d}"
            ids.append(machine_id)
            if self.session.get(Machine, machine_id) is None:
                machines.create(
                    machine_id=machine_id,
                    name=f"Simulated machine {i:02d}",
                    machine_type=DEMO_MACHINE_TYPE,
                    location="Synthetic demo",
                    description="SYNTHETIC demonstration machine. Not connected to any real equipment.",
                    source_id=source.id,
                )
        self._provision_sensors(ids)
        cursor = anchor - timedelta(hours=lookback_hours) - timedelta(microseconds=1)
        cfg = {
            "initialized": True, "seed": seed, "anchor": to_iso_z(anchor),
            "scenario_id": f"demo-seed{seed}", "step_seconds": 60,
            "cursor": cursor.isoformat(), "outage_windows": [],
        }
        self._save_config(source, cfg)
        return self._run(source, cfg, anchor)

    def poll(self, now: datetime | None = None) -> CycleReport:
        """Read newly available simulated records up to ``now`` and ingest them."""
        now = now or self.clock()
        source = self.get_or_create_source()
        cfg = self._config(source)
        if not cfg.get("initialized"):
            raise ValidationFailed(
                [ValidationIssue("NOT_INITIALIZED", "simulation", "Initialise the simulated data first (Settings).")]
            )
        self._provision_sensors(self._enabled_machine_ids(source))
        return self._run(source, cfg, now)

    def _run(self, source: DataSource, cfg: dict, now: datetime) -> CycleReport:
        machine_ids = self._enabled_machine_ids(source)
        adapter = self._build_adapter(source, cfg, machine_ids)
        try:
            ingestion = IngestionService(self.session, source, self.settings, self.clock)
            report = ingestion.run_cycle(adapter, now)
        finally:
            cursor = adapter.cursor
            adapter.close()
        cfg["cursor"] = cursor.isoformat()
        self._save_config(source, cfg)
        return report

    def start_interruption(self, minutes: int, now: datetime | None = None) -> None:
        """Schedule a synthetic source interruption starting now (demonstrates DISCONNECTED)."""
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 60:
            raise ValidationFailed(
                [ValidationIssue("INVALID_DURATION", "minutes", "Interruption must be 1-60 minutes.")]
            )
        now = now or self.clock()
        source = self.get_or_create_source()
        cfg = self._config(source)
        if not cfg.get("initialized"):
            raise ValidationFailed(
                [ValidationIssue("NOT_INITIALIZED", "simulation", "Initialise the simulated data first.")]
            )
        windows = [
            w for w in cfg.get("outage_windows", [])
            if parse_timestamp(w[1]) > now - timedelta(days=1)
        ]
        windows.append([to_iso_z(now), to_iso_z(now + timedelta(minutes=minutes))])
        cfg["outage_windows"] = windows
        self._save_config(source, cfg)

    def reset_simulated_data(self, confirmation: str) -> dict[str, int]:
        """DESTRUCTIVE (scoped). Requires the exact confirmation phrase."""
        if confirmation != RESET_PHRASE:
            raise ValidationFailed(
                [ValidationIssue("CONFIRMATION_REQUIRED", "confirmation", f"Type {RESET_PHRASE!r} to confirm.")]
            )
        sim = SourceType.SIMULATED.value
        source = self.get_or_create_source()
        anomaly_ids = list(self.session.scalars(select(Anomaly.id).where(Anomaly.source_type == sim)))
        counts: dict[str, int] = {}
        if anomaly_ids:
            self.session.execute(
                update(MaintenanceEvent)
                .where(MaintenanceEvent.related_anomaly_id.in_(anomaly_ids))
                .values(related_anomaly_id=None)
            )
        for label, stmt in (
            ("anomalies", delete(Anomaly).where(Anomaly.source_type == sim)),
            ("sensor_readings", delete(SensorReading).where(SensorReading.source_type == sim)),
            ("state_events", delete(MachineStateEvent).where(MachineStateEvent.source_type == sim)),
            ("rejected_records", delete(RejectedRecord).where(RejectedRecord.source_type == sim)),
            ("source_health_events", delete(SourceHealthEvent).where(SourceHealthEvent.source_id == source.id)),
        ):
            counts[label] = self.session.execute(stmt).rowcount or 0
        self._save_config(source, {})
        return counts
