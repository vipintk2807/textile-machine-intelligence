"""Simulation lifecycle and an end-to-end workflow without the UI (SYNTHETIC data, temp DB)."""

import unittest
from datetime import timedelta

from src.core.settings_rules import RuntimeSettings
from tests.helpers import HAS_SQLALCHEMY, NOW, DbTestCase, requires_sqlalchemy

if HAS_SQLALCHEMY:
    from sqlalchemy import func, select

    from src.models.orm import Anomaly, Machine, MachineStateEvent, RejectedRecord, SensorReading
    from src.models.schemas import MachineStatus, ValidationFailed
    from src.repositories.database import Database
    from src.services.anomaly_service import AnomalyService
    from src.services.machine_service import MachineService
    from src.services.maintenance_service import MaintenanceService
    from src.services.overview_service import machine_overviews, status_counts
    from src.services.report_service import ReportService
    from src.services.simulation_service import RESET_PHRASE, SimulationService
    from src.utils.time_utils import Window


def n(session, model):
    return session.scalar(select(func.count()).select_from(model))


@requires_sqlalchemy
class SimulationLifecycleTests(DbTestCase):
    SETTINGS = RuntimeSettings(freshness_seconds=300, timezone="UTC")

    def init(self, s, count=4):
        return SimulationService(s, self.SETTINGS, lambda: NOW).initialize_demo(
            seed=42, machine_count=count, lookback_hours=6, now=NOW)

    def test_initialise_creates_labelled_data_once(self):
        with self.db.session_scope() as s:
            report = self.init(s)
            self.assertEqual(report.status, "OK")
            self.assertEqual(n(s, Machine), 4)
            self.assertGreater(n(s, MachineStateEvent), 1000)
            self.assertEqual(n(s, RejectedRecord), 10)
            self.assertTrue(all(t == "SIMULATED" for t in s.scalars(select(MachineStateEvent.source_type)).all()))
            with self.assertRaises(ValidationFailed):  # a rerun must not duplicate history
                self.init(s)
            self.assertEqual(n(s, Machine), 4)

    def test_demo_situations_are_visible_in_status(self):
        with self.db.session_scope() as s:
            self.init(s)
            ov = {o.machine.machine_id: o for o in machine_overviews(s, self.SETTINGS, NOW)}
            self.assertEqual(ov["SIM-M03"].view.status, MachineStatus.UNKNOWN)   # silent, source healthy
            self.assertTrue(ov["SIM-M03"].view.is_stale)
            self.assertIn(ov["SIM-M01"].view.status, (MachineStatus.RUNNING, MachineStatus.STOPPED))
            counts = status_counts(list(ov.values()))
            self.assertEqual(counts["active"], 4)
            rules = set(s.scalars(select(Anomaly.rule_code)).all())
            self.assertTrue({"SENSOR_ABOVE_UPPER", "RECORD_REJECTED", "SENSOR_STALE", "SOURCE_UNAVAILABLE"} <= rules)

    def test_poll_is_idempotent_and_extends(self):
        with self.db.session_scope() as s:
            self.init(s)
            before = n(s, MachineStateEvent)
            svc = SimulationService(s, self.SETTINGS, lambda: NOW)
            self.assertEqual(svc.poll(NOW).status, "EMPTY")  # nothing new at the same instant
            self.assertEqual(n(s, MachineStateEvent), before)
            later = NOW + timedelta(minutes=10)
            svc.poll(later)
            grown = n(s, MachineStateEvent)
            self.assertGreater(grown, before)
            svc.poll(later)
            self.assertEqual(n(s, MachineStateEvent), grown)

    def test_interruption_shows_disconnected_then_recovers(self):
        with self.db.session_scope() as s:
            self.init(s)
            svc = SimulationService(s, self.SETTINGS, lambda: NOW)
            start = NOW + timedelta(minutes=1)
            svc.start_interruption(2, start)
            report = svc.poll(start + timedelta(seconds=30))
            self.assertEqual(report.status, "SOURCE_ERROR")
            ov = machine_overviews(s, self.SETTINGS, start + timedelta(seconds=30))
            self.assertTrue(all(o.view.status == MachineStatus.DISCONNECTED for o in ov))
            after = svc.poll(start + timedelta(minutes=5))
            self.assertEqual(after.status, "OK")
            ov = machine_overviews(s, self.SETTINGS, start + timedelta(minutes=5))
            self.assertFalse(any(o.view.status == MachineStatus.DISCONNECTED for o in ov))

    def test_reset_removes_only_simulated_data_and_keeps_user_records(self):
        with self.db.session_scope() as s:
            self.init(s)
            anomaly = s.scalars(select(Anomaly)).first()
            MaintenanceService(s, lambda: NOW).create(
                machine_id=anomaly.machine_id, event_type="Inspection", start_time=NOW - timedelta(hours=1),
                end_time=None, description="User note", related_anomaly_id=anomaly.id)
            svc = SimulationService(s, self.SETTINGS, lambda: NOW)
            with self.assertRaises(ValidationFailed):
                svc.reset_simulated_data("reset")
            self.assertGreater(n(s, MachineStateEvent), 0)
            counts = svc.reset_simulated_data(RESET_PHRASE)
            self.assertGreater(counts["state_events"], 0)
            self.assertEqual((n(s, MachineStateEvent), n(s, SensorReading), n(s, Anomaly), n(s, RejectedRecord)), (0, 0, 0, 0))
            self.assertEqual(n(s, Machine), 4)                      # machines kept
            records = MaintenanceService(s).list()
            self.assertEqual(len(records), 1)                       # user maintenance kept
            self.assertIsNone(records[0].related_anomaly_id)        # dangling link cleared
            self.assertFalse(svc.is_initialized())


@requires_sqlalchemy
class EndToEndTests(DbTestCase):
    SETTINGS = RuntimeSettings(freshness_seconds=300, timezone="UTC")

    def test_full_workflow_survives_reopening_the_database(self):
        with self.db.session_scope() as s:
            SimulationService(s, self.SETTINGS, lambda: NOW).initialize_demo(seed=7, machine_count=3, lookback_hours=6, now=NOW)
            src = SimulationService(s, self.SETTINGS).get_or_create_source()
            MachineService(s, lambda: NOW).create(machine_id="USER-1", name="User machine", machine_type="Knitting", source_id=src.id)
            alert = s.scalar(select(Anomaly).where(Anomaly.rule_code == "SENSOR_ABOVE_UPPER"))
            AnomalyService(s, lambda: NOW).acknowledge(alert.id, "reviewing")
            MaintenanceService(s, lambda: NOW).create(
                machine_id=alert.machine_id, event_type="Inspection", start_time=NOW - timedelta(hours=2),
                end_time=NOW - timedelta(hours=1), description="Inspection after spike", related_anomaly_id=alert.id)
            alert_id = alert.id
        path = self.db.path
        self.db.dispose()

        reopened = Database(path)
        try:
            reopened.initialize()
            with reopened.session_scope() as s:
                self.assertEqual(AnomalyService(s).get(alert_id).status, "ACKNOWLEDGED")
                self.assertEqual(len(MaintenanceService(s).list()), 1)
                window = Window(NOW - timedelta(hours=6), NOW)
                report = ReportService(s, self.SETTINGS, lambda: NOW)
                rows = report.runtime_rows(window)
                self.assertEqual({r["machine_id"] for r in rows}, {"SIM-M01", "SIM-M02", "SIM-M03", "USER-1"})
                for r in rows:
                    total = r["running_seconds"] + r["stopped_seconds"] + r["unknown_seconds"] + r["monitoring_gap_seconds"]
                    self.assertAlmostEqual(total, r["total_seconds"])
                user_row = next(r for r in rows if r["machine_id"] == "USER-1")
                self.assertIsNone(user_row["known_state_runtime_share_percent"])  # no data -> n/a, not 0 %
                self.assertEqual(user_row["monitoring_gap_seconds"], user_row["total_seconds"])
                csv = report.export_csv("anomalies", Window(NOW - timedelta(hours=6), NOW + timedelta(minutes=1)))
                self.assertIn("SIMULATED (synthetic data)", csv)
        finally:
            reopened.dispose()
            self.db = Database(path)  # so tearDown can dispose cleanly


if __name__ == "__main__":
    unittest.main()
