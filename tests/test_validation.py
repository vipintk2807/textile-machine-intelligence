"""Ingestion and form validation (synthetic records)."""

import unittest
from datetime import UTC, datetime, timedelta

from src.core.validation import (
    ValidationContext,
    validate_machine_fields,
    validate_maintenance_fields,
    validate_record,
)
from src.models.schemas import RawRecord, SensorSpec, SourceType

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CTX = ValidationContext(
    expected_source_type=SourceType.SIMULATED,
    machines={"M1": True, "OFF": False},
    sensors={
        ("M1", "S1"): SensorSpec(1, "M1", "S1", "sim-units"),
        ("M1", "S2"): SensorSpec(2, "M1", "S2", None),
    },
    now=NOW,
)


def sensor(**over):
    p = {"machine_id": "M1", "sensor_id": "S1", "value": 5.0, "unit": "sim-units",
         "event_time": "2026-03-01T11:59:00Z", "source_type": "SIMULATED", "source_event_id": "e1"}
    p.update(over)
    return RawRecord("sensor_reading", p)


def state(**over):
    p = {"machine_id": "M1", "state": "RUNNING", "event_time": "2026-03-01T11:59:00Z",
         "source_type": "SIMULATED", "source_event_id": "s1"}
    p.update(over)
    return RawRecord("machine_state", p)


def codes(raw):
    return {i.code for i in validate_record(raw, CTX).issues}


class SensorValidationTests(unittest.TestCase):
    def test_valid(self):
        out = validate_record(sensor(), CTX)
        self.assertTrue(out.accepted)
        self.assertEqual(out.record.value, 5.0)

    def test_zero_is_preserved(self):
        out = validate_record(sensor(value=0), CTX)
        self.assertTrue(out.accepted)
        self.assertEqual(out.record.value, 0.0)

    def test_negative_allowed(self):
        self.assertTrue(validate_record(sensor(value=-3.5), CTX).accepted)

    def test_rejections(self):
        cases = {
            "MISSING_VALUE": sensor(value=None),
            "NON_NUMERIC_VALUE": sensor(value="abc"),
            "NON_FINITE_VALUE": sensor(value=float("nan")),
            "UNKNOWN_SENSOR": sensor(sensor_id="NOPE"),
            "UNKNOWN_MACHINE": sensor(machine_id="GHOST"),
            "MACHINE_DISABLED": sensor(machine_id="OFF"),
            "MISSING_TIMESTAMP": sensor(event_time=None),
            "MALFORMED_TIMESTAMP": sensor(event_time="garbage"),
            "NAIVE_TIMESTAMP_NOT_ALLOWED": sensor(event_time="2026-03-01T11:00:00"),
            "TIMESTAMP_IN_FUTURE": sensor(event_time="2026-03-01T13:00:00Z"),
            "UNIT_MISMATCH": sensor(unit="other"),
            "SOURCE_TYPE_MISMATCH": sensor(source_type="REAL_SOURCE"),
            "MISSING_SOURCE_TYPE": sensor(source_type=None),
            "INVALID_SOURCE_EVENT_ID": sensor(source_event_id="  "),
            "MISSING_SENSOR_ID": sensor(sensor_id=None),
            "INVALID_IDENTIFIER": sensor(machine_id="bad id!"),
        }
        for code, raw in cases.items():
            with self.subTest(code=code):
                self.assertIn(code, codes(raw))

    def test_infinity_bool_and_huge_int(self):
        for v in (float("inf"), float("-inf"), 10**400):
            with self.subTest(v=v):
                self.assertIn("NON_FINITE_VALUE", codes(sensor(value=v)))
        self.assertIn("NON_NUMERIC_VALUE", codes(sensor(value=True)))

    def test_unit_not_invented(self):
        out = validate_record(sensor(sensor_id="S2", unit=None), CTX)
        self.assertTrue(out.accepted)
        self.assertIsNone(out.record.unit)
        self.assertFalse(out.record.metadata["unit_verified"])

    def test_unit_inherited_from_definition_is_flagged(self):
        raw = sensor()
        payload = dict(raw.payload)
        del payload["unit"]
        out = validate_record(RawRecord("sensor_reading", payload), CTX)
        self.assertEqual(out.record.unit, "sim-units")
        self.assertTrue(out.record.metadata["unit_from_definition"])

    def test_all_issues_reported(self):
        self.assertGreaterEqual(len(codes(sensor(value="x", event_time="bad", unit="zz"))), 3)


class StateValidationTests(unittest.TestCase):
    def test_valid_and_case_insensitive(self):
        self.assertTrue(validate_record(state(state="stopped"), CTX).accepted)

    def test_invalid_state_values(self):
        self.assertIn("INVALID_STATE", codes(state(state="FAULT")))
        self.assertIn("MISSING_STATE", codes(state(state=None)))

    def test_disconnected_is_not_a_reportable_state(self):
        out = validate_record(state(state="DISCONNECTED"), CTX)
        self.assertFalse(out.accepted)
        self.assertIn("source health", out.issues[0].message)

    def test_unknown_machine_and_missing_id(self):
        self.assertIn("UNKNOWN_MACHINE", codes(state(machine_id="GHOST")))
        self.assertIn("MISSING_MACHINE_ID", codes(state(machine_id=None)))

    def test_bad_sequence(self):
        self.assertIn("INVALID_SEQUENCE", codes(state(source_sequence=-1)))
        self.assertTrue(validate_record(state(source_sequence=3), CTX).accepted)


class OtherRecordTests(unittest.TestCase):
    def test_health_record(self):
        good = RawRecord("source_health", {"health": "unavailable", "event_time": "2026-03-01T11:00:00Z",
                                           "source_type": "SIMULATED"})
        self.assertTrue(validate_record(good, CTX).accepted)
        bad = RawRecord("source_health", {"health": "BROKEN", "event_time": "2026-03-01T11:00:00Z",
                                          "source_type": "SIMULATED"})
        self.assertIn("INVALID_HEALTH", {i.code for i in validate_record(bad, CTX).issues})

    def test_unsupported_type_and_payload(self):
        self.assertIn("UNSUPPORTED_RECORD_TYPE", codes(RawRecord("weird", {})))
        self.assertIn("INVALID_PAYLOAD", codes(RawRecord("machine_state", "text")))


class FormValidationTests(unittest.TestCase):
    def test_machine_fields_trim_and_require(self):
        clean, issues = validate_machine_fields("  M-9 ", " Name ", " Knitting ", "  ", None)
        self.assertFalse(issues)
        self.assertEqual((clean["machine_id"], clean["name"], clean["location"]), ("M-9", "Name", None))
        _, issues = validate_machine_fields("", "", "")
        self.assertEqual({i.field for i in issues}, {"machine_id", "name", "machine_type"})
        _, issues = validate_machine_fields("a b", "n", "t")
        self.assertEqual(issues[0].code, "INVALID_IDENTIFIER")

    def test_maintenance_rules(self):
        base = dict(event_type="Inspection", description="Checked", now=NOW,
                    start_time=NOW - timedelta(hours=2), end_time=NOW - timedelta(hours=1))
        clean, issues = validate_maintenance_fields(**base)
        self.assertFalse(issues)
        _, issues = validate_maintenance_fields(**{**base, "end_time": NOW - timedelta(hours=3)})
        self.assertEqual(issues[0].code, "END_BEFORE_START")
        _, issues = validate_maintenance_fields(**{**base, "start_time": NOW + timedelta(days=1)})
        self.assertIn("TIMESTAMP_IN_FUTURE", {i.code for i in issues})
        _, issues = validate_maintenance_fields(**{**base, "event_type": "Magic", "description": " "})
        self.assertEqual({i.field for i in issues}, {"event_type", "description"})
        _, issues = validate_maintenance_fields(**{**base, "start_time": None})
        self.assertEqual(issues[0].field, "start_time")


if __name__ == "__main__":
    unittest.main()
