"""Verify the documented benchmark matrix without optional report dependencies."""

from collections import Counter
import unittest

from benchmarks.run_benchmarks import (
    BENCHMARKS_PATH,
    SCENARIOS,
    _grep,
    _has_marker_line,
    _load_cases,
    _qurtail,
    _stream,
)


class BenchmarkMatrixTests(unittest.TestCase):
    """Keep the role matrix complete and each scenario behaviorally meaningful."""

    def test_matrix_has_three_unique_scenarios_per_role(self) -> None:
        self.assertEqual(
            Counter(scenario.role for scenario in SCENARIOS),
            {
                "Software engineer": 3,
                "IT operator": 3,
                "Database administrator": 3,
            },
        )
        self.assertEqual(
            len({scenario.slug for scenario in SCENARIOS}),
            len(SCENARIOS),
        )
        self.assertEqual(
            len({scenario.case_name for scenario in SCENARIOS}),
            len(SCENARIOS),
        )

    def test_generated_artifact_set_matches_the_matrix(self) -> None:
        expected = {scenario.slug for scenario in SCENARIOS}

        self.assertEqual(
            {path.stem for path in BENCHMARKS_PATH.glob("*.md")},
            expected | {"codex-workload-comparison"},
        )
        self.assertEqual(
            {path.stem for path in BENCHMARKS_PATH.glob("*.toml")},
            expected | {"codex-workload-replay"},
        )

    def test_every_scenario_retains_its_measured_signals(self) -> None:
        cases = _load_cases()

        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario.slug):
                lines, baseline, failure = _stream(
                    cases[scenario.case_name], scenario
                )
                grep_output = _grep(lines, scenario.grep_pattern)
                qurtail_output = _qurtail(lines, scenario.options)

                self.assertNotIn(baseline, grep_output)
                self.assertIn(failure, grep_output)
                self.assertIn(baseline, qurtail_output)
                self.assertIn(failure, qurtail_output)
                self.assertTrue(_has_marker_line(qurtail_output))


if __name__ == "__main__":
    unittest.main()
