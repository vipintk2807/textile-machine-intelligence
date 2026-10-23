"""Anomaly rules, freshness and effective-status derivation (synthetic inputs)."""

import unittest
from datetime import UTC, datetime, timedelta

from src.core.anomaly_rules import (
    evaluate_sensor_staleness,
    evaluate_state_duration,
    evaluate_threshold,
    validate_threshold_config,
)
from src.core.freshness import classify_freshness, derive_machine_status
from src.core.intervals import ResolvedPoint
from src.models.schemas import (
    FreshnessLabel,
    MachineStatus,
    ObservedState as S,
    RuleCode,
    RuleConfigError,
    Severity,
    SourceHealth,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
TH = timedelta(minutes=5)


def hits(value, lower=None, upper=None):
    return evaluate_threshold(sensor_label="S1", value=value, unit="sim-units", lower=lower,
                              upper=upper, severity=Severity.WARNING)


class ThresholdTests(unittest.TestCase):
    def test_strict_boundaries(self):
        self.assertEqual(hits(75.0, upper=75.0), [])          # equal -> no trigger
        self.assertEqual(hits(75.0001, upper=75.0)[0].rule_code, RuleCode.SENSOR_ABOVE_UPPER)
        self.assertEqual(hits(10.0, lower=10.0), [])
        self.assertEqual(hits(9.999, lower=10.0)[0].rule_code, RuleCode.SENSOR_BELOW_LOWER)

    def test_within_range_and_zero(self):
        self.assertEqual(hits(0, lower=-1, upper=1), [])
        self.assertEqual(hits(0, lower=0.5)[0].rule_code, RuleCode.SENSOR_BELOW_LOWER)

    def test_no_thresholds_no_hits(self):
        self.assertEqual(hits(1e9), [])

    def test_hit_carries_evidence(self):
        h = hits(90.0, upper=75.0)[0]
        self.assertEqual((h.observed_value, h.threshold_value), (90.0, 75.0))
        self.assertIn("illustrative", h.description)
        self.assertEqual(h.severity, Severity.WARNING)

    def test_invalid_configuration(self):
        for lo, up in [(5, 5), (6, 5), (float("nan"), None), (None, float("inf"))]:
            with self.subTest(lo=lo, up=up):
                with self.assertRaises(RuleConfigError):
                    validate_threshold_config(lo, up)


class StalenessAndDurationTests(unittest.TestCase):
    def test_stale_boundary(self):
        kw = dict(sensor_label="S1", now=NOW, stale_after=TH, severity=Severity.WARNING)
        self.assertIsNone(evaluate_sensor_staleness(last_event_time=NOW - TH, **kw))
        self.assertIsNotNone(evaluate_sensor_staleness(last_event_time=NOW - TH - timedelta(seconds=1), **kw))
        self.assertIsNone(evaluate_sensor_staleness(last_event_time=None, **kw))  # no data != anomaly
        self.assertIsNone(evaluate_sensor_staleness(last_event_time=NOW - timedelta(days=1),
                          sensor_label="S1", now=NOW, stale_after=None, severity=Severity.INFO))

    def test_state_duration(self):
        limits = {S.RUNNING: timedelta(hours=2), S.STOPPED: None}
        kw = dict(now=NOW, limits=limits, severity=Severity.INFO)
        self.assertIsNone(evaluate_state_duration(state=S.RUNNING, state_since=NOW - timedelta(hours=2), **kw))
        self.assertIsNotNone(evaluate_state_duration(state=S.RUNNING, state_since=NOW - timedelta(hours=3), **kw))
        self.assertIsNone(evaluate_state_duration(state=S.STOPPED, state_since=NOW - timedelta(days=9), **kw))
        with self.assertRaises(RuleConfigError):
            evaluate_state_duration(state=S.RUNNING, state_since=NOW, now=NOW,
                                    limits={S.RUNNING: timedelta(0)}, severity=Severity.INFO)


class FreshnessTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(classify_freshness(None, NOW, TH).label, FreshnessLabel.NO_DATA)
        self.assertEqual(classify_freshness(NOW - TH, NOW, TH).label, FreshnessLabel.CURRENT)
        self.assertEqual(classify_freshness(NOW - TH - timedelta(seconds=1), NOW, TH).label, FreshnessLabel.STALE)
        self.assertEqual(classify_freshness(NOW + timedelta(seconds=30), NOW, TH).age, timedelta(0))


def view(point, health=SourceHealth.HEALTHY):
    return derive_machine_status(latest=point, state_since=None, source_health=health, now=NOW, threshold=TH)


class StatusTests(unittest.TestCase):
    def test_current_running_and_stopped(self):
        self.assertEqual(view(ResolvedPoint(NOW - timedelta(minutes=1), S.RUNNING)).status, MachineStatus.RUNNING)
        self.assertEqual(view(ResolvedPoint(NOW - timedelta(minutes=1), S.STOPPED)).status, MachineStatus.STOPPED)

    def test_stale_running_is_unknown_not_stopped(self):
        v = view(ResolvedPoint(NOW - timedelta(hours=1), S.RUNNING))
        self.assertEqual(v.status, MachineStatus.UNKNOWN)
        self.assertTrue(v.is_stale)
        self.assertEqual(v.last_known_state, S.RUNNING)

    def test_no_history_is_unknown(self):
        v = view(None)
        self.assertEqual(v.status, MachineStatus.UNKNOWN)
        self.assertIsNone(v.last_known_state)

    def test_source_unavailable_is_disconnected_not_stopped(self):
        v = view(ResolvedPoint(NOW - timedelta(minutes=1), S.RUNNING), SourceHealth.UNAVAILABLE)
        self.assertEqual(v.status, MachineStatus.DISCONNECTED)
        self.assertEqual(v.last_known_state, S.RUNNING)
        self.assertFalse(v.is_confirmed)

    def test_degraded_does_not_override_state(self):
        v = view(ResolvedPoint(NOW - timedelta(minutes=1), S.RUNNING), SourceHealth.DEGRADED)
        self.assertEqual(v.status, MachineStatus.RUNNING)

    def test_reported_unknown_and_ambiguous(self):
        self.assertEqual(view(ResolvedPoint(NOW, S.UNKNOWN)).status, MachineStatus.UNKNOWN)
        self.assertEqual(view(ResolvedPoint(NOW, S.UNKNOWN, ambiguous=True)).status, MachineStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
