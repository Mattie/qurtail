"""Verify the performance harness and low-risk streaming resource gates."""

from __future__ import annotations

import unittest

from benchmarks.run_performance import measure


class PerformanceHarnessTests(unittest.TestCase):
    """Keep release measurements executable without making unit tests time-sensitive."""

    def test_measurement_reports_every_release_metric(self) -> None:
        result = measure(record_count=500, width=256)

        self.assertEqual(result["record_count"], 500)
        self.assertEqual(result["record_width"], 256)
        self.assertEqual(result["memory_record_count"], 500)
        self.assertEqual(result["timing_trials"], 5)
        self.assertEqual(len(result["timing_samples"]), 5)
        self.assertEqual(
            result["framing_cpu_ratio"],
            result["total_qurtail_cpu_seconds"]
            / max(result["total_framing_cpu_seconds"], 1e-12),
        )
        self.assertEqual(
            set(result["gates"]),
            {
                "at_least_50000_records_per_second",
                "peak_additional_memory_below_64_mib",
                "first_record_below_100_ms",
            },
        )
        self.assertTrue(result["passed"], result)
        self.assertTrue(all(result["gates"].values()), result)
        self.assertLess(result["peak_additional_bytes"], 64 * 1024 * 1024)
        self.assertLess(result["first_record_seconds"], 0.100)


if __name__ == "__main__":
    unittest.main()
