#!/usr/bin/env python3
"""Targeted tests for the SUPRA boundary-analysis definitions."""

from pathlib import Path
import tempfile
import unittest

import analyze


def trade(
    trade_id: int,
    timestamp: int,
    price: float = 1.0,
    volume: float = 10.0,
    side: str = "buy",
) -> analyze.Trade:
    return analyze.Trade(
        trade_id=trade_id,
        timestamp=timestamp,
        price=price,
        volume=volume,
        side=side,
        rpi=0,
        date="1970-01-01",
    )


class BoundaryAnalysisTests(unittest.TestCase):
    def test_percentile_interpolates(self) -> None:
        self.assertEqual(analyze.percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(analyze.percentile([5], 0.1), 5.0)

    def test_boundary_event_requires_both_narrow_legs(self) -> None:
        boundary = 300_000
        rows = [
            trade(1, boundary - 260, side="sell", volume=10.0),
            trade(2, boundary + 40, side="buy", volume=9.0),
            trade(3, boundary + 299_000, side="buy"),
        ]
        events, possible = analyze.build_boundary_events(rows)
        self.assertEqual(possible, 287)
        event = next(event for event in events if event.boundary_ms == boundary)
        self.assertTrue(event.both_legs)
        self.assertTrue(event.opposite_homogeneous_sides)
        self.assertAlmostEqual(event.balance_ratio, 0.9)
        self.assertTrue(event.last_before_boundary)
        self.assertTrue(event.first_after_boundary)

    def test_same_taker_side_is_not_an_opposite_pair(self) -> None:
        boundary = 300_000
        rows = [
            trade(1, boundary - 260, side="buy"),
            trade(2, boundary + 40, side="buy"),
            trade(3, boundary + 299_000),
        ]
        events, _ = analyze.build_boundary_events(rows)
        self.assertTrue(events[0].both_legs)
        self.assertFalse(events[0].opposite_homogeneous_sides)

    def test_wide_hit_and_narrow_pair_are_distinct(self) -> None:
        boundary = 300_000
        rows = [
            trade(1, boundary - 50_000),
            trade(2, boundary + 600),
            trade(3, boundary + 299_000),
        ]
        hits, possible = analyze.count_wide_hits(rows)
        events, _ = analyze.build_boundary_events(rows)
        self.assertEqual((hits, possible), (1, 287))
        event = next(event for event in events if event.boundary_ms == boundary)
        self.assertFalse(event.both_legs)

    def test_archive_dates_not_trade_activity_define_denominator(self) -> None:
        rows = [trade(1, 600_000), trade(2, analyze.DAY_MS - 600_000)]
        anchors = analyze.boundary_range(rows)
        self.assertEqual((anchors[0], anchors[-1], len(anchors)), (300_000, 86_100_000, 287))

    def test_shifted_phases_include_the_first_fully_observed_anchor(self) -> None:
        rows = [trade(1, 600_000), trade(2, analyze.DAY_MS - 600_000)]
        expectations = {
            0: (300_000, 86_100_000, 287),
            1: (1_000, 86_101_000, 288),
            299: (299_000, 86_399_000, 288),
        }
        for phase, expected in expectations.items():
            with self.subTest(phase=phase):
                anchors = analyze.boundary_range(rows, phase_seconds=phase)
                self.assertEqual((anchors[0], anchors[-1], len(anchors)), expected)

    def test_shifted_opening_event_is_counted(self) -> None:
        boundary = 1_000
        rows = [
            trade(1, boundary - 260, side="sell"),
            trade(2, boundary + 40, side="buy"),
            trade(3, analyze.DAY_MS - 1_000),
        ]
        events, possible = analyze.build_boundary_events(rows, phase_seconds=1)
        hits, wide_possible = analyze.count_wide_hits(rows, phase_seconds=1)
        self.assertEqual((possible, wide_possible), (288, 288))
        self.assertTrue(events[0].both_legs)
        self.assertEqual(events[0].boundary_ms, boundary)
        self.assertGreaterEqual(hits, 1)

    def test_shifted_phase_denominators_across_cadences(self) -> None:
        rows = [trade(1, 600_000), trade(2, analyze.DAY_MS - 600_000)]
        for cadence_ms in (60_000, 300_000, 600_000):
            with self.subTest(cadence_ms=cadence_ms):
                self.assertEqual(
                    len(analyze.boundary_range(rows, cadence_ms)),
                    analyze.DAY_MS // cadence_ms - 1,
                )
                self.assertEqual(
                    len(
                        analyze.boundary_range(
                            rows, cadence_ms, phase_seconds=1
                        )
                    ),
                    analyze.DAY_MS // cadence_ms,
                )

    def test_shifted_window_edges_are_inclusive(self) -> None:
        rows = [trade(1, 600_000), trade(2, analyze.DAY_MS - 600_000)]
        self.assertEqual(
            analyze.boundary_range(
                rows,
                phase_seconds=1,
                window_start_ms=-1_000,
                window_end_ms=100,
            )[0],
            1_000,
        )
        self.assertEqual(
            analyze.boundary_range(
                rows,
                phase_seconds=1,
                window_start_ms=-1_001,
                window_end_ms=100,
            )[0],
            301_000,
        )
        self.assertEqual(
            analyze.boundary_range(
                rows,
                phase_seconds=299,
                window_start_ms=-300,
                window_end_ms=1_000,
            )[-1],
            analyze.DAY_MS - 1_000,
        )
        self.assertEqual(
            analyze.boundary_range(
                rows,
                phase_seconds=299,
                window_start_ms=-300,
                window_end_ms=1_001,
            )[-1],
            analyze.DAY_MS - 301_000,
        )

    def test_core_trade_uses_five_minute_phase(self) -> None:
        self.assertTrue(analyze.is_core_trade(300_000 - 260))
        self.assertTrue(analyze.is_core_trade(300_000 + 40))
        self.assertFalse(analyze.is_core_trade(300_000 + 140))
        self.assertFalse(analyze.is_core_trade(60_000 + 40))

    def test_pinned_manifest_detects_changed_archive(self) -> None:
        expected = [
            {
                "symbol": "X",
                "date": "2026-01-01",
                "sha256": "a",
                "compressed_bytes": "5",
                "rows": "2",
            }
        ]
        observed = [dict(expected[0])]
        analyze.verify_pinned_sources(expected, observed)
        observed[0]["sha256"] = "b"
        with self.assertRaises(AssertionError):
            analyze.verify_pinned_sources(expected, observed)

    def test_verify_requires_a_pinned_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "source_manifest.csv"
            with self.assertRaisesRegex(FileNotFoundError, "--verify requires"):
                analyze.read_required_pinned_manifest(missing)

    def test_custom_output_directory_contains_charts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self.assertEqual(analyze.chart_output_directory(output_dir), output_dir)

        default_output = Path(analyze.__file__).resolve().parent
        self.assertEqual(
            analyze.chart_output_directory(default_output),
            default_output.parent.parent,
        )


if __name__ == "__main__":
    unittest.main()
