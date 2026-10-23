"""Simulator contract tests. All generated data is SYNTHETIC."""

import unittest
from collections import Counter
from datetime import UTC, datetime, timedelta

from src.core.validation import ValidationContext, validate_record
from src.data_sources.simulated_source import (
    DEFAULT_SENSOR_PROFILES,
    DEMO_SCRIPT,
    EMPTY_SCRIPT,
    SimulatedSource,
    SimulationConfig,
)
from src.models.schemas import (
    SensorSpec,
    SourceHealth,
    SourceType,
)
from src.utils.time_utils import Interval, parse_timestamp

ANCHOR = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
MACHINES = ["SIM-M01", "SIM-M02", "SIM-M03", "SIM-M04"]


def make(script=EMPTY_SCRIPT, seed=42, machines=MACHINES, lookback=120, **kw):
    src = SimulatedSource(SimulationConfig(anchor=ANCHOR, seed=seed), script, machines,
                          start_after=ANCHOR - timedelta(minutes=lookback), **kw)
    src.connect()
    return src


def ctx(machines=MACHINES):
    sensors = {(m, p.code): SensorSpec(i, m, p.code, p.unit)
               for i, m in enumerate(machines) for p in DEFAULT_SENSOR_PROFILES}
    return ValidationContext(SourceType.SIMULATED, {m: True for m in machines}, sensors, ANCHOR)


def payload_key(r):
    return tuple(sorted((k, str(v)) for k, v in r.payload.items()))


class DeterminismTests(unittest.TestCase):
    def test_same_seed_same_output(self):
        a = make().read_records(ANCHOR).records
        b = make().read_records(ANCHOR).records
        self.assertEqual([payload_key(r) for r in a], [payload_key(r) for r in b])
        self.assertGreater(len(a), 0)

    def test_different_seed_differs(self):
        a = make(seed=1).read_records(ANCHOR).records
        b = make(seed=2).read_records(ANCHOR).records
        self.assertNotEqual([payload_key(r) for r in a], [payload_key(r) for r in b])

    def test_chunked_reads_equal_single_read(self):
        whole = {payload_key(r) for r in make().read_records(ANCHOR).records}
        src = make()
        chunked = set()
        for minutes in (30, 75, 120):
            now = ANCHOR - timedelta(minutes=120) + timedelta(minutes=minutes)
            chunked |= {payload_key(r) for r in src.read_records(now).records}
        self.assertEqual(whole, chunked)

    def test_rereading_returns_valid_empty_not_duplicates(self):
        src = make()
        self.assertGreater(len(src.read_records(ANCHOR).records), 0)
        again = src.read_records(ANCHOR)
        self.assertTrue(again.is_valid_empty)

    def test_event_ids_unique(self):
        ids = [r.payload["source_event_id"] for r in make(DEMO_SCRIPT, lookback=360).read_records(ANCHOR).records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_limit_truncates_and_cursor_progresses(self):
        src = make()
        first = src.read_records(ANCHOR, limit=10)
        self.assertEqual(len(first.records), 10)
        self.assertLess(src.cursor, ANCHOR)


class ContractTests(unittest.TestCase):
    def test_all_clean_records_pass_validation(self):
        result = make().read_records(ANCHOR)
        c = ctx()
        for raw in result.records:
            out = validate_record(raw, c)
            self.assertTrue(out.accepted, (raw, out.issues))
            self.assertEqual(out.record.source_type, SourceType.SIMULATED)

    def test_event_times_are_aware_utc_and_within_range(self):
        for raw in make().read_records(ANCHOR).records:
            t = parse_timestamp(raw.payload["event_time"])
            self.assertLessEqual(t, ANCHOR)
            self.assertGreater(t, ANCHOR - timedelta(minutes=121))

    def test_sensor_without_unit_omits_unit(self):
        recs = [r for r in make().read_records(ANCHOR).records if r.record_type == "sensor_reading"]
        s2 = [r for r in recs if r.payload["sensor_id"] == "SIM-S2"]
        self.assertTrue(s2 and all("unit" not in r.payload for r in s2))

    def test_state_vocabulary_is_valid(self):
        states = {r.payload["state"] for r in make().read_records(ANCHOR).records if r.record_type == "machine_state"}
        self.assertTrue(states <= {"RUNNING", "STOPPED", "UNKNOWN"})

    def test_all_records_labelled_simulated(self):
        for raw in make(DEMO_SCRIPT, lookback=360).read_records(ANCHOR).records:
            self.assertEqual(raw.payload.get("source_type"), "SIMULATED")


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.result = make(DEMO_SCRIPT, lookback=360).read_records(ANCHOR)
        self.c = ctx()
        self.outcomes = [(r, validate_record(r, self.c)) for r in self.result.records]

    def test_injected_invalid_records_rejected_with_expected_codes(self):
        rejected = [o for _, o in self.outcomes if not o.accepted]
        self.assertEqual(len(rejected), 10)
        codes = Counter(i.code for o in rejected for i in o.issues)
        for expected in ("NON_FINITE_VALUE", "UNKNOWN_SENSOR", "MALFORMED_TIMESTAMP", "UNIT_MISMATCH",
                         "INVALID_STATE", "UNKNOWN_MACHINE", "NON_NUMERIC_VALUE",
                         "NAIVE_TIMESTAMP_NOT_ALLOWED", "MISSING_MACHINE_ID"):
            self.assertIn(expected, codes)

    def test_no_clean_record_rejected(self):
        for raw, out in self.outcomes:
            if ":BAD:" not in str(raw.payload.get("source_event_id", "")):
                self.assertTrue(out.accepted, out.issues)

    def test_spike_crosses_illustrative_threshold(self):
        values = [r.payload["value"] for r in self.result.records
                  if r.record_type == "sensor_reading" and r.payload.get("sensor_id") == "SIM-S1"
                  and isinstance(r.payload.get("value"), float) and r.payload["value"] == r.payload["value"]
                  and r.payload["value"] != float("inf")]
        threshold = DEFAULT_SENSOR_PROFILES[0].upper_threshold
        self.assertTrue(any(v > threshold for v in values))
        self.assertLess(sorted(values)[-2], 200)
        normal = [v for v in values if v <= threshold]
        self.assertGreater(len(normal), len(values) - 5)

    def test_outage_withholds_records_and_emits_health_events(self):
        health = [r for r in self.result.records if r.record_type == "source_health"]
        self.assertEqual([h.payload["health"] for h in health], ["UNAVAILABLE", "HEALTHY"])
        start = ANCHOR - timedelta(minutes=300)
        end = ANCHOR - timedelta(minutes=285)
        for r in self.result.records:
            if r.record_type == "machine_state" and ":BAD:" not in r.payload["source_event_id"]:
                t = parse_timestamp(r.payload["event_time"])
                self.assertFalse(start <= t < end)

    def test_silent_machine_has_no_recent_records(self):
        recent = [r for r in self.result.records if r.payload.get("machine_id") == "SIM-M03"
                  and r.record_type == "machine_state"
                  and parse_timestamp(r.payload["event_time"]) >= ANCHOR - timedelta(minutes=45)]
        self.assertEqual(recent, [])

    def test_sensor_gap_omits_only_that_sensor(self):
        gap_start, gap_end = ANCHOR - timedelta(minutes=90), ANCHOR - timedelta(minutes=60)
        def in_gap(r):
            return gap_start <= parse_timestamp(r.payload["event_time"]) < gap_end
        s2 = [r for r in self.result.records if r.payload.get("machine_id") == "SIM-M01"
              and r.payload.get("sensor_id") == "SIM-S2" and ":BAD:" not in r.payload["source_event_id"]]
        self.assertFalse(any(in_gap(r) for r in s2))
        s1 = [r for r in self.result.records if r.payload.get("machine_id") == "SIM-M01"
              and r.payload.get("sensor_id") == "SIM-S1" and ":BAD:" not in r.payload["source_event_id"]]
        self.assertTrue(any(in_gap(r) for r in s1))

    def test_unknown_window_reports_unknown(self):
        unknown = [r for r in self.result.records if r.payload.get("machine_id") == "SIM-M02"
                   and r.payload.get("state") == "UNKNOWN"]
        self.assertGreaterEqual(len(unknown), 10)

    def test_late_arrivals_are_out_of_event_time_order(self):
        recs = [r for r in self.result.records if r.record_type == "machine_state"
                and r.payload.get("machine_id") == "SIM-M04" and ":BAD:" not in r.payload["source_event_id"]]
        times = [parse_timestamp(r.payload["event_time"]) for r in recs]
        self.assertNotEqual(times, sorted(times))


class FailureAndEmptyTests(unittest.TestCase):
    def test_outage_is_an_error_not_an_empty_result(self):
        outage = Interval(ANCHOR - timedelta(minutes=10), ANCHOR + timedelta(minutes=10))
        src = make(extra_outages=[outage], lookback=30)
        res = src.read_records(ANCHOR)
        self.assertFalse(res.ok)
        self.assertFalse(res.is_valid_empty)
        self.assertEqual(res.error.code, "SOURCE_UNAVAILABLE")
        self.assertEqual(src.get_health().health, SourceHealth.UNAVAILABLE)

    def test_recovery_resumes_without_replaying_the_gap(self):
        outage = Interval(ANCHOR - timedelta(minutes=10), ANCHOR + timedelta(minutes=10))
        src = make(extra_outages=[outage], lookback=30)
        self.assertFalse(src.read_records(ANCHOR).ok)
        later = ANCHOR + timedelta(minutes=20)
        res = src.read_records(later)
        self.assertTrue(res.ok)
        times = {parse_timestamp(r.payload["event_time"]) for r in res.records if r.record_type == "machine_state"}
        self.assertFalse(any(outage.start <= t < outage.end for t in times))
        self.assertEqual(src.get_health().health, SourceHealth.HEALTHY)

    def test_no_machines_gives_valid_empty(self):
        res = make(machines=[]).read_records(ANCHOR)
        self.assertTrue(res.is_valid_empty)

    def test_not_connected_is_an_error(self):
        src = SimulatedSource(SimulationConfig(anchor=ANCHOR), EMPTY_SCRIPT, MACHINES)
        self.assertEqual(src.read_records(ANCHOR).error.code, "NOT_CONNECTED")

    def test_close_cleans_up(self):
        src = make()
        src.close()
        self.assertFalse(src.read_records(ANCHOR).ok)
        self.assertEqual(src.get_health().health, SourceHealth.UNKNOWN)

    def test_adapter_has_no_control_methods(self):
        public = {n for n in dir(SimulatedSource) if not n.startswith("_")}
        self.assertFalse({n for n in public if any(w in n for w in ("write", "send", "command", "start_machine", "stop_machine"))})


if __name__ == "__main__":
    unittest.main()
