"""State derivation, ingestion pipeline and anomaly creation against a real (temporary) SQLite DB.
All data is SYNTHETIC."""

import unittest
from datetime import timedelta
from unittest import mock

from tests.helpers import (
    HAS_SQLALCHEMY, NOW, DbTestCase, health_raw, minutes_ago, requires_sqlalchemy, sensor_raw, state_raw,
)

if HAS_SQLALCHEMY:
    from sqlalchemy import func, select

    from src.core.settings_rules import RuntimeSettings
    from src.data_sources.simulated_source import SimulatedSource, SimulationConfig
    from src.models.orm import Anomaly, MachineStateEvent, RejectedRecord, SensorReading
    from src.models.schemas import MachineStatus, ObservedState, PersistenceError, SourceHealth
    from src.services.state_service import StateService
    from src.utils.time_utils import Interval, Window

TH = timedelta(minutes=5)


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@requires_sqlalchemy
class StateDerivationTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with self.db.session_scope() as s:
            self.src = self.make_source(s)
            self.make_machine(s, self.src)
            self.source_id = self.src.id

    def ingest(self, records, now=NOW):
        with self.db.session_scope() as s:
            ing = self.ingestion(s, s.get(type(self.src), self.source_id))
            return ing.ingest_batch(records, now)

    def status(self, now=NOW):
        with self.db.session_scope() as s:
            return StateService(s).machine_status("M1", self.source_id, now, TH)

    def test_current_state_and_since(self):
        self.ingest([state_raw("M1", "STOPPED", minutes_ago(30), "a"), state_raw("M1", "RUNNING", minutes_ago(20), "b"),
                     state_raw("M1", "RUNNING", minutes_ago(1), "c")])
        view = self.status()
        self.assertEqual(view.status, MachineStatus.RUNNING)
        self.assertEqual(view.state_since, minutes_ago(20))

    def test_repeated_observations_do_not_create_transitions(self):
        self.ingest([state_raw("M1", "RUNNING", minutes_ago(m), f"r{m}") for m in (4, 3, 2, 1)])
        with self.db.session_scope() as s:
            self.assertEqual(count(s, MachineStateEvent), 4)
            transitions = s.scalar(select(func.count()).select_from(MachineStateEvent).where(MachineStateEvent.is_transition.is_(True)))
            self.assertEqual(transitions, 1)

    def test_late_record_does_not_overwrite_current_state(self):
        self.ingest([state_raw("M1", "RUNNING", minutes_ago(1), "new")])
        report = self.ingest([state_raw("M1", "STOPPED", minutes_ago(60), "old")])
        self.assertEqual(report.out_of_order, 1)
        self.assertEqual(self.status().status, MachineStatus.RUNNING)
        with self.db.session_scope() as s:
            old = s.scalar(select(MachineStateEvent).where(MachineStateEvent.source_event_id == "old"))
            self.assertEqual(old.quality_status, "DELAYED")
            new = s.scalar(select(MachineStateEvent).where(MachineStateEvent.source_event_id == "new"))
            self.assertEqual((new.previous_state, new.is_transition), ("STOPPED", True))  # re-derived by event time

    def test_stale_state_is_unknown_not_stopped(self):
        self.ingest([state_raw("M1", "RUNNING", minutes_ago(30), "a")])
        view = self.status()
        self.assertEqual((view.status, view.is_stale, view.last_known_state),
                         (MachineStatus.UNKNOWN, True, ObservedState.RUNNING))

    def test_duplicate_event_id_ignored(self):
        self.ingest([state_raw("M1", "RUNNING", minutes_ago(2), "dup")])
        report = self.ingest([state_raw("M1", "RUNNING", minutes_ago(2), "dup")])
        self.assertEqual((report.duplicates, report.accepted), (1, 0))
        with self.db.session_scope() as s:
            self.assertEqual(count(s, MachineStateEvent), 1)

    def test_same_values_without_event_id_are_not_duplicates(self):
        self.ingest([state_raw("M1", "RUNNING", minutes_ago(2)), state_raw("M1", "RUNNING", minutes_ago(2))])
        with self.db.session_scope() as s:
            self.assertEqual(count(s, MachineStateEvent), 2)

    def test_conflicting_same_timestamp_is_unknown(self):
        self.ingest([state_raw("M1", "RUNNING", minutes_ago(1), "x"), state_raw("M1", "STOPPED", minutes_ago(1), "y")])
        self.assertEqual(self.status().status, MachineStatus.UNKNOWN)

    def test_required_scenario_through_the_database(self):
        base = NOW - timedelta(hours=3)
        recs = [state_raw("M1", "RUNNING", base, "1"), state_raw("M1", "STOPPED", base + timedelta(minutes=20), "2"),
                state_raw("M1", "RUNNING", base + timedelta(minutes=35), "3")]
        self.ingest(recs, now=NOW)
        window = Window(base, base + timedelta(hours=1))
        with self.db.session_scope() as s:
            acc = StateService(s).account("M1", self.source_id, window, None)
        self.assertEqual((acc.running, acc.stopped, acc.unknown, acc.gap),
                         (timedelta(minutes=45), timedelta(minutes=15), timedelta(0), timedelta(0)))

    def test_outage_interval_derivation(self):
        self.ingest([health_raw("UNAVAILABLE", minutes_ago(50), "h1"), health_raw("HEALTHY", minutes_ago(30), "h2")])
        with self.db.session_scope() as s:
            outages = StateService(s).outage_intervals(self.source_id, Window(minutes_ago(60), NOW))
        self.assertEqual(outages, [Interval(minutes_ago(50), minutes_ago(30))])

    def test_disconnected_status_when_source_unavailable(self):
        self.ingest([state_raw("M1", "RUNNING", minutes_ago(1), "a"), health_raw("UNAVAILABLE", minutes_ago(1), "h")])
        view = self.status()
        self.assertEqual((view.status, view.last_known_state), (MachineStatus.DISCONNECTED, ObservedState.RUNNING))


@requires_sqlalchemy
class IngestionPipelineTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with self.db.session_scope() as s:
            src = self.make_source(s)
            self.make_machine(s, src)
            self.make_machine(s, src, "OFF")
            self.sensor = self.make_sensor(s, "M1", "S1", unit="u", upper=75.0)
            self.sensor_id = self.sensor.id
            self.source_id = src.id
            from src.services.machine_service import MachineService
            MachineService(s).set_enabled("OFF", False)

    def run_batch(self, records, settings=None, now=NOW):
        with self.db.session_scope() as s:
            src = s.get(type(self.make_source(s)), self.source_id)
            return self.ingestion(s, src, settings).ingest_batch(records, now)

    def test_valid_and_invalid_are_split_and_invalid_quarantined(self):
        report = self.run_batch([
            sensor_raw("M1", "S1", 10.0, minutes_ago(3), "ok"),
            sensor_raw("M1", "S1", float("nan"), minutes_ago(2), "nan"),
            sensor_raw("M1", "NOPE", 1.0, minutes_ago(2), "unk"),
            sensor_raw("GHOST", "S1", 1.0, minutes_ago(2), "ghost"),
            sensor_raw("OFF", "S1", 1.0, minutes_ago(2), "off"),
            sensor_raw("M1", "S1", 1.0, minutes_ago(2), "unit", unit="wrong"),
            state_raw("M1", "FAULT", minutes_ago(2), "badstate"),
        ])
        self.assertEqual((report.accepted_sensor, report.rejected), (1, 6))
        with self.db.session_scope() as s:
            self.assertEqual(count(s, SensorReading), 1)
            self.assertEqual(count(s, RejectedRecord), 6)
            self.assertEqual(s.scalar(select(SensorReading.value)), 10.0)

    def test_zero_reading_is_stored_not_dropped(self):
        self.run_batch([sensor_raw("M1", "S1", 0, minutes_ago(1), "zero")])
        with self.db.session_scope() as s:
            self.assertEqual(s.scalar(select(SensorReading.value)), 0.0)

    def test_rejected_record_raises_data_quality_anomaly_for_known_machine(self):
        self.run_batch([sensor_raw("M1", "S1", "abc", minutes_ago(1), "bad1"), sensor_raw("M1", "S1", "abd", minutes_ago(1), "bad2")])
        with self.db.session_scope() as s:
            rows = s.scalars(select(Anomaly)).all()
            self.assertEqual(len(rows), 1)  # one episode, two occurrences
            self.assertEqual((rows[0].rule_code, rows[0].category, rows[0].occurrence_count),
                             ("RECORD_REJECTED", "DATA_QUALITY", 2))

    def test_reingesting_same_batch_is_idempotent(self):
        batch = [sensor_raw("M1", "S1", 5.0, minutes_ago(3), "a"), state_raw("M1", "RUNNING", minutes_ago(3), "b"),
                 sensor_raw("M1", "S1", "x", minutes_ago(3), "c")]
        self.run_batch(batch)
        second = self.run_batch(batch)
        self.assertEqual((second.accepted, second.rejected, second.duplicates), (0, 0, 3))
        with self.db.session_scope() as s:
            self.assertEqual((count(s, SensorReading), count(s, MachineStateEvent), count(s, RejectedRecord)), (1, 1, 1))

    def test_threshold_anomaly_boundary_and_episode_dedup(self):
        self.run_batch([sensor_raw("M1", "S1", 75.0, minutes_ago(5), "eq")])  # equal -> no anomaly
        with self.db.session_scope() as s:
            self.assertEqual(count(s, Anomaly), 0)
        self.run_batch([sensor_raw("M1", "S1", 80.0, minutes_ago(4), "h1"), sensor_raw("M1", "S1", 90.0, minutes_ago(3), "h2")])
        with self.db.session_scope() as s:
            rows = s.scalars(select(Anomaly)).all()
            self.assertEqual(len(rows), 1)
            a = rows[0]
            self.assertEqual((a.rule_code, a.status, a.occurrence_count, a.observed_value, a.threshold_value, a.severity),
                             ("SENSOR_ABOVE_UPPER", "OPEN", 2, 90.0, 75.0, "WARNING"))
            self.assertEqual(a.source_type, "SIMULATED")

    def test_out_of_order_sensor_flagged_delayed(self):
        self.run_batch([sensor_raw("M1", "S1", 5.0, minutes_ago(1), "new")])
        report = self.run_batch([sensor_raw("M1", "S1", 6.0, minutes_ago(30), "old")])
        self.assertEqual(report.out_of_order, 1)
        with self.db.session_scope() as s:
            self.assertEqual(s.scalar(select(SensorReading.quality_status).where(SensorReading.source_event_id == "old")), "DELAYED")

    def test_stale_sensor_creates_data_quality_not_fault(self):
        self.run_batch([sensor_raw("M1", "S1", 5.0, minutes_ago(60), "old")])
        with self.db.session_scope() as s:
            src = s.get(type(self.make_source(s)), self.source_id)
            self.ingestion(s, src, RuntimeSettings(sensor_stale_seconds=900)).evaluate_periodic(NOW)
            a = s.scalar(select(Anomaly).where(Anomaly.rule_code == "SENSOR_STALE"))
            self.assertEqual(a.category, "DATA_QUALITY")
            self.assertIn("not evidence of a machine fault", a.description)

    def test_unknown_unit_sensor_accepts_reading_flagged_unverified(self):
        with self.db.session_scope() as s:
            self.make_sensor(s, "M1", "NOUNIT", unit=None, upper=None)
        self.run_batch([sensor_raw("M1", "NOUNIT", 3.0, minutes_ago(1), "nu", unit=None)])
        with self.db.session_scope() as s:
            row = s.scalar(select(SensorReading).where(SensorReading.source_event_id == "nu"))
            self.assertIsNone(row.unit)

    def test_database_failure_aborts_whole_batch(self):
        with mock.patch("src.services.sensor_service.SensorService.insert_reading", side_effect=PersistenceError("disk full")):
            with self.assertRaises(PersistenceError):
                self.run_batch([state_raw("M1", "RUNNING", minutes_ago(2), "s1"), sensor_raw("M1", "S1", 1.0, minutes_ago(2), "v1")])
        with self.db.session_scope() as s:
            self.assertEqual((count(s, MachineStateEvent), count(s, SensorReading)), (0, 0))


@requires_sqlalchemy
class SourceCycleTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with self.db.session_scope() as s:
            src = self.make_source(s)
            self.make_machine(s, src)
            self.source_id = src.id
            # The simulator emits SIM-S1/SIM-S2 readings, so the machine must have those sensors defined
            # (the app provisions them automatically; this test does it explicitly).
            from src.data_sources.simulated_source import default_sensor_definitions
            from src.services.sensor_service import SensorService
            SensorService(s, lambda: NOW).ensure_definitions("M1", default_sensor_definitions())

    def cycle(self, adapter, now):
        with self.db.session_scope() as s:
            src = s.get(type(self.make_source(s)), self.source_id)
            return self.ingestion(s, src).run_cycle(adapter, now)

    def adapter(self, outages=()):
        a = SimulatedSource(SimulationConfig(anchor=NOW), machine_ids=["M1"], start_after=NOW - timedelta(minutes=30),
                            extra_outages=outages)
        a.connect()
        return a

    def test_source_error_is_reported_not_hidden_and_marks_disconnected(self):
        outage = Interval(NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        report = self.cycle(self.adapter([outage]), NOW)
        self.assertEqual((report.status, report.source_health), ("SOURCE_ERROR", SourceHealth.UNAVAILABLE))
        with self.db.session_scope() as s:
            self.assertEqual(count(s, MachineStateEvent), 0)  # nothing fabricated
            view = StateService(s).machine_status("M1", self.source_id, NOW, TH)
            self.assertEqual(view.status, MachineStatus.DISCONNECTED)
            self.assertEqual(s.scalar(select(Anomaly.rule_code)), "SOURCE_UNAVAILABLE")

    def test_recovery_restores_health_and_preserves_gap(self):
        outage = Interval(NOW - timedelta(minutes=5), NOW + timedelta(minutes=5))
        adapter = self.adapter([outage])
        self.cycle(adapter, NOW)
        later = NOW + timedelta(minutes=20)
        report = self.cycle(adapter, later)
        self.assertEqual(report.status, "OK")
        with self.db.session_scope() as s:
            self.assertEqual(StateService(s).current_source_health(self.source_id), SourceHealth.HEALTHY)
            times = s.scalars(select(MachineStateEvent.event_time)).all()
            self.assertFalse(any(outage.start <= t < outage.end for t in times))
            acc = StateService(s).account("M1", self.source_id, Window(NOW - timedelta(minutes=10), NOW), TH)
            self.assertGreater(acc.gap, timedelta(0))
            self.assertEqual(acc.stopped, timedelta(0))  # an outage is never downtime

    def test_valid_empty_result_is_not_an_error(self):
        adapter = self.adapter()
        self.cycle(adapter, NOW)
        report = self.cycle(adapter, NOW)
        self.assertEqual(report.status, "EMPTY")

    def test_rejections_degrade_health(self):
        with self.db.session_scope() as s:
            src = s.get(type(self.make_source(s)), self.source_id)
            ing = self.ingestion(s, src)
            ing.ingest_batch([state_raw("M1", "FAULT", minutes_ago(1), "bad")], NOW)
            ing._record_health(SourceHealth.DEGRADED, NOW, "x")
            self.assertEqual(StateService(s).current_source_health(src.id), SourceHealth.DEGRADED)


if __name__ == "__main__":
    unittest.main()
