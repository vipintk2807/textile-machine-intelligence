"""Runtime/downtime/unknown/gap accounting. All data below is SYNTHETIC test data."""

import random
import unittest
from datetime import UTC, datetime, timedelta

from src.core.intervals import Observation, account_window, known_state_runtime_share
from src.models.schemas import GapReason, IntervalCategory, ObservedState as S
from src.utils.time_utils import Interval, Window

MIN = timedelta(minutes=1)


def t(h, m=0):
    return datetime(2026, 3, 1, h, m, tzinfo=UTC)


def obs(h, m, state, seq=None):
    return Observation(t(h, m), state, seq)


class RequiredScenarioTests(unittest.TestCase):
    """Synthetic scenario required by the specification (not an observed factory result)."""

    TRANSITIONS = [obs(9, 0, S.RUNNING), obs(9, 20, S.STOPPED), obs(9, 35, S.RUNNING)]

    def check(self, acc):
        self.assertEqual(acc.running, 45 * MIN)
        self.assertEqual(acc.stopped, 15 * MIN)
        self.assertEqual(acc.unknown, timedelta(0))
        self.assertEqual(acc.gap, timedelta(0))
        self.assertTrue(acc.is_balanced)
        self.assertAlmostEqual(acc.known_state_runtime_share, 75.0)

    def test_transition_only_semantics(self):
        self.check(account_window(self.TRANSITIONS, Window(t(9), t(10))))

    def test_with_heartbeats_and_timeout(self):
        heartbeats = []
        for minute in range(0, 60, 5):
            state = S.RUNNING if not (20 <= minute < 35) else S.STOPPED
            heartbeats.append(obs(9, minute, state))
        self.check(account_window(heartbeats, Window(t(9), t(10)), timedelta(minutes=10)))


class AccountingTests(unittest.TestCase):
    def test_empty_history_is_all_gap(self):
        acc = account_window([], Window(t(9), t(10)))
        self.assertEqual(acc.gap, 60 * MIN)
        self.assertIsNone(acc.known_state_runtime_share)
        self.assertEqual(acc.segments[0].gap_reason, GapReason.NO_OBSERVATIONS)

    def test_window_before_first_observation(self):
        acc = account_window([obs(9, 30, S.RUNNING)], Window(t(9), t(10)))
        self.assertEqual(acc.gap, 30 * MIN)
        self.assertEqual(acc.running, 30 * MIN)
        self.assertEqual(acc.segments[0].gap_reason, GapReason.BEFORE_FIRST_OBSERVATION)

    def test_state_carried_in_from_before_window(self):
        acc = account_window([obs(8, 0, S.STOPPED)], Window(t(9), t(10)))
        self.assertEqual(acc.stopped, 60 * MIN)

    def test_boundaries_half_open(self):
        # Event exactly at end has no effect; exactly at start counts.
        acc = account_window([obs(9, 0, S.RUNNING), obs(10, 0, S.STOPPED)], Window(t(9), t(10)))
        self.assertEqual((acc.running, acc.stopped), (60 * MIN, timedelta(0)))
        adjacent = account_window([obs(9, 0, S.RUNNING), obs(10, 0, S.STOPPED)], Window(t(10), t(11)))
        self.assertEqual((adjacent.running, adjacent.stopped), (timedelta(0), 60 * MIN))

    def test_adjacent_windows_sum_to_combined(self):
        data = [obs(9, 0, S.RUNNING), obs(9, 20, S.STOPPED), obs(9, 50, S.RUNNING)]
        a = account_window(data, Window(t(9), t(9, 40)))
        b = account_window(data, Window(t(9, 40), t(10, 30)))
        whole = account_window(data, Window(t(9), t(10, 30)))
        self.assertEqual(a.running + b.running, whole.running)
        self.assertEqual(a.stopped + b.stopped, whole.stopped)

    def test_explicit_unknown_is_not_downtime(self):
        acc = account_window(
            [obs(9, 0, S.RUNNING), obs(9, 30, S.UNKNOWN), obs(9, 45, S.RUNNING)], Window(t(9), t(10))
        )
        self.assertEqual((acc.running, acc.stopped, acc.unknown), (45 * MIN, timedelta(0), 15 * MIN))

    def test_evidence_timeout_creates_gap_not_downtime(self):
        acc = account_window([obs(9, 0, S.RUNNING)], Window(t(9), t(10)), timedelta(minutes=10))
        self.assertEqual(acc.running, 10 * MIN)
        self.assertEqual(acc.gap, 50 * MIN)
        self.assertEqual(acc.stopped, timedelta(0))
        self.assertEqual(acc.segments[-1].gap_reason, GapReason.EVIDENCE_EXPIRED)

    def test_outage_cuts_evidence_and_is_gap(self):
        outage = Interval(t(9, 20), t(9, 40))
        acc = account_window(
            [obs(9, 0, S.RUNNING), obs(9, 40, S.RUNNING)], Window(t(9), t(10)), None, [outage]
        )
        self.assertEqual(acc.running, 40 * MIN)
        self.assertEqual(acc.gap, 20 * MIN)
        reasons = [s.gap_reason for s in acc.segments if s.category == IntervalCategory.GAP]
        self.assertEqual(reasons, [GapReason.SOURCE_UNAVAILABLE])
        self.assertEqual(acc.stopped, timedelta(0))  # an outage is never downtime

    def test_duplicates_and_out_of_order_input(self):
        shuffled = [obs(9, 35, S.RUNNING), obs(9, 0, S.RUNNING), obs(9, 20, S.STOPPED),
                    obs(9, 20, S.STOPPED), obs(9, 0, S.RUNNING)]
        acc = account_window(shuffled, Window(t(9), t(10)))
        self.assertEqual((acc.running, acc.stopped), (45 * MIN, 15 * MIN))

    def test_same_timestamp_conflict_is_ambiguous_unknown(self):
        acc = account_window(
            [obs(9, 0, S.RUNNING), obs(9, 30, S.RUNNING), obs(9, 30, S.STOPPED)], Window(t(9), t(10))
        )
        self.assertEqual(acc.running, 30 * MIN)
        self.assertEqual(acc.unknown, 30 * MIN)
        self.assertEqual(acc.ambiguous_instants, 1)

    def test_same_timestamp_conflict_resolved_by_sequence(self):
        acc = account_window(
            [obs(9, 0, S.RUNNING, 1), obs(9, 0, S.STOPPED, 2)], Window(t(9), t(10))
        )
        self.assertEqual(acc.stopped, 60 * MIN)
        self.assertEqual(acc.ambiguous_instants, 0)

    def test_naive_event_time_rejected(self):
        with self.assertRaises(ValueError):
            account_window([Observation(datetime(2026, 3, 1, 9), S.RUNNING)], Window(t(9), t(10)))

    def test_bad_timeout(self):
        with self.assertRaises(ValueError):
            account_window([], Window(t(9), t(10)), timedelta(0))

    def test_share_none_when_no_known_time(self):
        self.assertIsNone(known_state_runtime_share(timedelta(0), timedelta(0)))
        self.assertEqual(known_state_runtime_share(timedelta(0), 10 * MIN), 0.0)  # real zero

    def test_invariant_holds_for_random_histories(self):
        rng = random.Random(1234)  # fixed seed -> deterministic
        for _ in range(300):
            n = rng.randint(0, 12)
            data = [
                Observation(t(8) + timedelta(minutes=rng.randint(0, 200)),
                            rng.choice(list(S)), rng.choice([None, 1, 2]))
                for _ in range(n)
            ]
            ostart = t(8) + timedelta(minutes=rng.randint(0, 150))
            outages = [Interval(ostart, ostart + timedelta(minutes=rng.randint(0, 40)))] if rng.random() < 0.5 else []
            timeout = rng.choice([None, timedelta(minutes=rng.randint(1, 30))])
            ws = t(8) + timedelta(minutes=rng.randint(0, 100))
            window = Window(ws, ws + timedelta(minutes=rng.randint(1, 120)))
            acc = account_window(data, window, timeout, outages)
            self.assertTrue(acc.is_balanced, (data, window, timeout, outages))
            self.assertEqual(acc.segments[0].start, window.start)
            self.assertEqual(acc.segments[-1].end, window.end)
            for a, b in zip(acc.segments, acc.segments[1:]):
                self.assertEqual(a.end, b.start)  # contiguous, no overlap


if __name__ == "__main__":
    unittest.main()
