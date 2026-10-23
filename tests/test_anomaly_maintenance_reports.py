"""Anomaly workflow, maintenance records, reports and CSV export (SYNTHETIC data, temp DB)."""

import unittest
from datetime import timedelta

from tests.helpers import HAS_SQLALCHEMY, NOW, DbTestCase, minutes_ago, requires_sqlalchemy, sensor_raw, state_raw

if HAS_SQLALCHEMY:
    from src.core.anomaly_rules import RuleHit
    from src.core.settings_rules import RuntimeSettings
    from src.models.schemas import (
        AnomalyCategory, ExportError, RuleCode, Severity, StateTransitionError, ValidationFailed,
    )
    from src.services.anomaly_service import AnomalyService
    from src.services.maintenance_service import MaintenanceService
    from src.services.report_service import ReportService
    from src.utils.time_utils import Window


def hit(value=90.0):
    return RuleHit(RuleCode.SENSOR_ABOVE_UPPER, AnomalyCategory.SENSOR_THRESHOLD, Severity.WARNING,
                   "above threshold", value, 75.0, NOW)


@requires_sqlalchemy
class AnomalyWorkflowTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with self.db.session_scope() as s:
            self.make_machine(s, self.make_source(s))
            self.sensor_id = self.make_sensor(s).id

    def raise_one(self, s, value=90.0):
        return AnomalyService(s, lambda: NOW).raise_hit(
            machine_id="M1", sensor_id=self.sensor_id, hit=hit(value), source_type="SIMULATED", now=NOW)

    def test_open_ack_resolve_flow(self):
        with self.db.session_scope() as s:
            a, created = self.raise_one(s)
            self.assertTrue(created)
            svc = AnomalyService(s, lambda: NOW)
            svc.acknowledge(a.id, "looking")
            self.assertEqual(svc.get(a.id).status, "ACKNOWLEDGED")
            with self.assertRaises(StateTransitionError):
                svc.acknowledge(a.id)
            with self.assertRaises(ValidationFailed):
                svc.resolve(a.id, "  ")
            self.assertEqual(svc.get(a.id).status, "ACKNOWLEDGED")  # failed resolve changed nothing
            svc.resolve(a.id, "Checked and cleared")
            with self.assertRaises(StateTransitionError):
                svc.resolve(a.id, "again")
            r = svc.get(a.id)
            self.assertEqual((r.status, r.resolution_note), ("RESOLVED", "Checked and cleared"))
            self.assertIsNotNone(r.resolved_at)

    def test_acknowledged_anomaly_keeps_accumulating_until_resolved(self):
        with self.db.session_scope() as s:
            a, _ = self.raise_one(s)
            AnomalyService(s, lambda: NOW).acknowledge(a.id)
            b, created = self.raise_one(s, 95.0)
            self.assertFalse(created)
            self.assertEqual((b.id, b.occurrence_count, b.observed_value), (a.id, 2, 95.0))
            AnomalyService(s, lambda: NOW).resolve(a.id, "done")
            c, created = self.raise_one(s)
            self.assertTrue(created)  # new episode after resolution
            self.assertNotEqual(c.id, a.id)

    def test_filters_and_counts(self):
        with self.db.session_scope() as s:
            a, _ = self.raise_one(s)
            svc = AnomalyService(s, lambda: NOW)
            self.assertEqual(svc.active_count(), 1)
            svc.resolve(a.id, "ok")
            self.assertEqual(svc.active_count(), 0)
            self.assertEqual(len(svc.list(statuses=["RESOLVED"])), 1)
            self.assertEqual(svc.list(statuses=["OPEN"]), [])


@requires_sqlalchemy
class MaintenanceTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with self.db.session_scope() as s:
            src = self.make_source(s)
            self.make_machine(s, src, "M1")
            self.make_machine(s, src, "M2")
            self.sensor_id = self.make_sensor(s).id

    def create(self, s, **kw):
        base = dict(machine_id="M1", event_type="Inspection", start_time=minutes_ago(120), end_time=minutes_ago(60),
                    description="Routine check")
        base.update(kw)
        return MaintenanceService(s, lambda: NOW).create(**base)

    def test_create_update_list(self):
        with self.db.session_scope() as s:
            rec = self.create(s, finding="Looked normal", responsible="Team A")
            svc = MaintenanceService(s, lambda: NOW)
            svc.update(rec.id, event_type="Calibration", start_time=rec.start_time, end_time=None, description="Changed")
        with self.db.session_scope() as s:
            rows = MaintenanceService(s, lambda: NOW).list(machine_id="M1")
            self.assertEqual((rows[0].event_type, rows[0].end_time, rows[0].description), ("Calibration", None, "Changed"))

    def test_validation(self):
        with self.db.session_scope() as s:
            for kw in (dict(description=" "), dict(end_time=minutes_ago(300)), dict(start_time=NOW + timedelta(days=2)),
                       dict(event_type="Magic"), dict(machine_id="NOPE"), dict(start_time=None)):
                with self.subTest(kw=kw), self.assertRaises(ValidationFailed):
                    self.create(s, **kw)

    def test_anomaly_link_must_belong_to_same_machine(self):
        with self.db.session_scope() as s:
            a, _ = AnomalyService(s, lambda: NOW).raise_hit(machine_id="M2", hit=hit(), source_type="SIMULATED")
            with self.assertRaises(ValidationFailed):
                self.create(s, related_anomaly_id=a.id)
            b, _ = AnomalyService(s, lambda: NOW).raise_hit(machine_id="M1", hit=hit(), source_type="SIMULATED")
            self.assertEqual(self.create(s, related_anomaly_id=b.id).related_anomaly_id, b.id)


@requires_sqlalchemy
class ReportTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with self.db.session_scope() as s:
            src = self.make_source(s)
            self.make_machine(s, src)
            self.make_sensor(s)
            self.source_id = src.id
            ing = self.ingestion(s, src)
            ing.ingest_batch(
                [state_raw("M1", "RUNNING", minutes_ago(60), "a"), state_raw("M1", "RUNNING", minutes_ago(59), "b"),
                 state_raw("M1", "STOPPED", minutes_ago(30), "c"), sensor_raw("M1", "S1", -3.5, minutes_ago(20), "v1"),
                 sensor_raw("M1", "S1", 0, minutes_ago(10), "v2")], NOW)
            MaintenanceService(s, lambda: NOW).create(
                machine_id="M1", event_type="Inspection", start_time=minutes_ago(50), end_time=None,
                description="=HYPERLINK(\"http://x\")")
        self.window = Window(minutes_ago(120), NOW)

    def report(self, s):
        return ReportService(s, RuntimeSettings(freshness_seconds=600), lambda: NOW)

    def test_transitions_only_filter(self):
        with self.db.session_scope() as s:
            r = self.report(s)
            self.assertEqual(r.state_events(self.window, transitions_only=True).total, 2)
            self.assertEqual(r.state_events(self.window, transitions_only=False).total, 3)

    def test_half_open_window_excludes_end_instant(self):
        with self.db.session_scope() as s:
            w = Window(minutes_ago(120), minutes_ago(30))  # STOPPED event is exactly at the end
            self.assertEqual(self.report(s).state_events(w, transitions_only=False).total, 2)

    def test_runtime_rows_have_gap_and_share(self):
        with self.db.session_scope() as s:
            row = self.report(s).runtime_rows(Window(minutes_ago(60), NOW))[0]
        self.assertAlmostEqual(row["running_seconds"] + row["stopped_seconds"] + row["unknown_seconds"]
                               + row["monitoring_gap_seconds"], row["total_seconds"])
        self.assertIsNotNone(row["known_state_runtime_share_percent"])

    def test_csv_exports_have_provenance_period_and_safe_cells(self):
        with self.db.session_scope() as s:
            r = self.report(s)
            sensors = r.export_csv("sensor_readings", self.window)
            self.assertIn("SIMULATED (synthetic data)", sensors)
            self.assertIn("report_start_utc", sensors.splitlines()[0])
            self.assertIn(",-3.5,", sensors)        # genuine negative number untouched
            self.assertIn(",0.0,", sensors)         # zero is exported as a number, not blanked
            maint = r.export_csv("maintenance", self.window)
            self.assertIn("'=HYPERLINK", maint)     # formula neutralised
            self.assertIn("USER-ENTERED", maint)
            for kind in ReportService.EXPORTS:
                self.assertTrue(r.export_csv(kind, self.window).startswith(ReportService.EXPORTS[kind][0]))

    def test_export_never_silently_truncates(self):
        with self.db.session_scope() as s:
            with self.assertRaises(ExportError):
                self.report(s).export_csv("state_history", self.window, transitions_only=False, max_rows=1)

    def test_unknown_export_kind(self):
        with self.db.session_scope() as s:
            with self.assertRaises(ExportError):
                self.report(s).export_csv("bogus", self.window)


if __name__ == "__main__":
    unittest.main()
