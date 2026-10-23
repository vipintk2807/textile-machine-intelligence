"""Timestamp, time-zone and window tests (synthetic inputs only)."""

import unittest
from datetime import UTC, datetime, timedelta, timezone

from src.utils.time_utils import (
    Interval,
    NaivePolicy,
    TimestampError,
    Window,
    floor_to_seconds,
    format_duration,
    local_naive_to_utc,
    local_range_to_window,
    merge_intervals,
    parse_timestamp,
    to_iso_z,
)


def utc(h, m=0, s=0, day=1):
    return datetime(2026, 3, day, h, m, s, tzinfo=UTC)


class ParseTimestampTests(unittest.TestCase):
    def test_z_suffix_and_offset_convert_to_utc(self):
        self.assertEqual(parse_timestamp("2026-03-01T10:00:00Z"), utc(10))
        self.assertEqual(parse_timestamp("2026-03-01T15:30:00+05:30"), utc(10))

    def test_naive_rejected_by_default(self):
        with self.assertRaises(TimestampError) as ctx:
            parse_timestamp("2026-03-01T10:00:00")
        self.assertEqual(ctx.exception.code, "NAIVE_TIMESTAMP_NOT_ALLOWED")

    def test_naive_policies(self):
        self.assertEqual(parse_timestamp("2026-03-01T10:00:00", NaivePolicy.ASSUME_UTC), utc(10))
        ist = timezone(timedelta(hours=5, minutes=30))
        self.assertEqual(
            parse_timestamp("2026-03-01T15:30:00", NaivePolicy.ASSUME_SOURCE_TZ, ist), utc(10)
        )
        with self.assertRaises(TimestampError):
            parse_timestamp("2026-03-01T15:30:00", NaivePolicy.ASSUME_SOURCE_TZ, None)

    def test_missing_and_malformed(self):
        for value, code in [
            (None, "MISSING_TIMESTAMP"),
            ("  ", "MISSING_TIMESTAMP"),
            ("yesterday", "MALFORMED_TIMESTAMP"),
            (1700000000, "MALFORMED_TIMESTAMP"),
            ("1999-01-01T00:00:00Z", "TIMESTAMP_OUT_OF_RANGE"),
        ]:
            with self.subTest(value=value):
                with self.assertRaises(TimestampError) as ctx:
                    parse_timestamp(value)
                self.assertEqual(ctx.exception.code, code)

    def test_round_trip_iso_z(self):
        self.assertEqual(to_iso_z(utc(10, 5)), "2026-03-01T10:05:00Z")

    def test_floor(self):
        self.assertEqual(floor_to_seconds(utc(10, 5, 42), 60), utc(10, 5))


class WindowTests(unittest.TestCase):
    def test_half_open_contains(self):
        w = Window(utc(9), utc(10))
        self.assertTrue(w.contains(utc(9)))
        self.assertFalse(w.contains(utc(10)))

    def test_invalid_window(self):
        with self.assertRaises(ValueError):
            Window(utc(10), utc(10))
        with self.assertRaises(ValueError):
            Window(datetime(2026, 3, 1, 9), utc(10))  # naive

    def test_clip(self):
        w = Window(utc(9), utc(10))
        self.assertEqual(w.clip(utc(8), utc(9, 30)), Interval(utc(9), utc(9, 30)))
        self.assertIsNone(w.clip(utc(10), utc(11)))
        self.assertIsNone(w.clip(utc(8), utc(9)))

    def test_merge_intervals(self):
        merged = merge_intervals(
            [Interval(utc(9), utc(10)), Interval(utc(10), utc(11)), Interval(utc(12), utc(13))]
        )
        self.assertEqual(merged, [Interval(utc(9), utc(11)), Interval(utc(12), utc(13))])


class LocalTimeTests(unittest.TestCase):
    def test_kolkata_offset(self):
        self.assertEqual(local_naive_to_utc(datetime(2026, 3, 1, 15, 30), "Asia/Kolkata"), utc(10))

    def test_dst_gap_rejected_and_overlap_first(self):
        # US spring-forward 2026-03-08 02:30 does not exist; fall-back 2026-11-01 01:30 is ambiguous.
        with self.assertRaises(ValueError):
            local_naive_to_utc(datetime(2026, 3, 8, 2, 30), "America/New_York")
        first = local_naive_to_utc(datetime(2026, 11, 1, 1, 30), "America/New_York")
        self.assertEqual(first, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))

    def test_dst_day_has_23_hours(self):
        w = local_range_to_window(datetime(2026, 3, 8), datetime(2026, 3, 9), "America/New_York")
        self.assertEqual(w.duration, timedelta(hours=23))

    def test_bad_zone(self):
        with self.assertRaises(ValueError):
            local_naive_to_utc(datetime(2026, 3, 1), "Not/AZone")

    def test_format_duration(self):
        self.assertEqual(format_duration(timedelta(minutes=45)), "45m 00s")
        self.assertEqual(format_duration(timedelta(hours=1, minutes=5, seconds=30)), "1h 05m 30s")
        self.assertEqual(format_duration(None), "—")


if __name__ == "__main__":
    unittest.main()
