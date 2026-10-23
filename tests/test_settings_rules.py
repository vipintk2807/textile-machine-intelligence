import unittest

from src.core.settings_rules import RuntimeSettings, validate_settings_changes
from src.models.schemas import ObservedState, Severity


class SettingsRuleTests(unittest.TestCase):
    def setUp(self):
        self.cur = RuntimeSettings()

    def test_valid_changes(self):
        new, issues = validate_settings_changes(
            {"freshness_seconds": 120, "timezone": "UTC", "max_running_seconds": 7200,
             "severities": {"SENSOR_STALE": "CRITICAL"}}, self.cur)
        self.assertFalse(issues)
        self.assertEqual(new.freshness_seconds, 120)
        self.assertEqual(new.severity_for("SENSOR_STALE"), Severity.CRITICAL)
        self.assertEqual(new.state_limits()[ObservedState.RUNNING].total_seconds(), 7200)
        self.assertIsNone(new.state_limits()[ObservedState.STOPPED])

    def test_invalid_values_leave_settings_unchanged(self):
        for changes in ({"freshness_seconds": 5}, {"freshness_seconds": True}, {"freshness_seconds": "300"},
                        {"timezone": "Mars/Base"}, {"max_running_seconds": 10}, {"sim_machine_count": 99},
                        {"severities": {"NOPE": "INFO"}}, {"severities": {"SENSOR_STALE": "LOUD"}},
                        {"unknown_key": 1}):
            with self.subTest(changes=changes):
                new, issues = validate_settings_changes(changes, self.cur)
                self.assertTrue(issues)
                self.assertEqual(new, self.cur)

    def test_limit_can_be_disabled(self):
        on, _ = validate_settings_changes({"max_running_seconds": 600}, self.cur)
        off, issues = validate_settings_changes({"max_running_seconds": None}, on)
        self.assertFalse(issues)
        self.assertIsNone(off.max_running_seconds)

    def test_default_severities_never_critical(self):
        self.assertNotIn(Severity.CRITICAL, RuntimeSettings().severities.values())


if __name__ == "__main__":
    unittest.main()
