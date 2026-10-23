"""SQLAlchemy ORM models (SQLite).

Timestamps are stored as naive UTC text via :class:`UTCDateTime` and always come
back as aware UTC datetimes. Event time and ingestion time are separate columns.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Store aware datetimes as naive UTC; return aware UTC. Rejects naive input."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Naive datetime cannot be stored; supply an aware datetime.")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    configuration: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Machine(Base):
    __tablename__ = "machines"

    machine_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    machine_type: Mapped[str] = mapped_column(String(120), nullable=False)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (Index("ix_machines_source", "source_id"),)


class SourceHealthEvent(Base):
    __tablename__ = "source_health_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False)
    health: Mapped[str] = mapped_column(String(16), nullable=False)
    event_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scenario_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_id", "source_event_id", name="uq_health_source_event"),
        CheckConstraint("health IN ('HEALTHY','DEGRADED','UNAVAILABLE','UNKNOWN')", name="ck_health"),
        Index("ix_health_source_time", "source_id", "event_time"),
    )


class SensorDefinition(Base):
    __tablename__ = "sensor_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.machine_id"), nullable=False)
    sensor_code: Mapped[str] = mapped_column(String(64), nullable=False)
    measurement_type: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)  # NULL = unit unknown
    lower_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    upper_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_severity: Mapped[str] = mapped_column(String(16), nullable=False, default="WARNING")
    stale_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("machine_id", "sensor_code", name="uq_sensor_machine_code"),
    )


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.machine_id"), nullable=False)
    sensor_id: Mapped[int] = mapped_column(ForeignKey("sensor_definitions.id"), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(16), nullable=False, default="VALID")
    scenario_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        UniqueConstraint("source_id", "source_event_id", name="uq_reading_source_event"),
        Index("ix_readings_machine_sensor_time", "machine_id", "sensor_id", "event_time"),
        Index("ix_readings_time", "event_time"),
    )


class MachineStateEvent(Base):
    """Accepted state *observations*. Transitions are flagged (``is_transition``)."""

    __tablename__ = "machine_state_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.machine_id"), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_transition: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    event_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_status: Mapped[str] = mapped_column(String(16), nullable=False, default="VALID")
    scenario_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        UniqueConstraint("source_id", "source_event_id", name="uq_state_source_event"),
        CheckConstraint("state IN ('RUNNING','STOPPED','UNKNOWN')", name="ck_state_value"),
        Index("ix_state_machine_time", "machine_id", "event_time"),
        Index("ix_state_time", "event_time"),
    )


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.machine_id"), nullable=False)
    sensor_id: Mapped[int | None] = mapped_column(ForeignKey("sensor_definitions.id"), nullable=True)
    rule_code: Mapped[str] = mapped_column(String(48), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    observed_event_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    investigation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')", name="ck_anomaly_status"),
        Index("ix_anomaly_machine_status", "machine_id", "status"),
        Index("ix_anomaly_status_time", "status", "detected_at"),
    )


class MaintenanceEvent(Base):
    """User-entered maintenance records (never generated by a data source)."""

    __tablename__ = "maintenance_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.machine_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    finding: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_anomaly_id: Mapped[int | None] = mapped_column(ForeignKey("anomalies.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (Index("ix_maint_machine_time", "machine_id", "start_time"),)


class RejectedRecord(Base):
    """Quarantine for records that failed validation (never used in calculations)."""

    __tablename__ = "rejected_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    record_type: Mapped[str] = mapped_column(String(32), nullable=False)
    machine_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    issues_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scenario_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "source_event_id", name="uq_rejected_source_event"),
        Index("ix_rejected_received", "received_at"),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
