"""Verify provenance and sanitization of the multi-project Codex workload corpus."""

import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

from benchmarks.capture_codex_workloads import (
    CORPUS_PATH,
    REPORT_PATH,
    REPEATS,
    ROOT,
    WORKLOADS,
    _environment,
    _sanitize,
)


class CodexWorkloadCorpusTests(unittest.TestCase):
    """Keep the live capture explicit, successful, and free of local path leakage."""

    def test_corpus_records_every_workload_and_successful_replay(self) -> None:
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        cases = corpus["cases"]
        workloads = {workload.name: workload for workload in WORKLOADS}

        self.assertIn("multiple local Git projects", corpus["methodology"])
        self.assertEqual(
            {case["name"] for case in cases},
            {workload.name for workload in WORKLOADS},
        )
        self.assertEqual(len({case["project"] for case in cases}), len(cases))
        self.assertGreaterEqual(len({case["technology"] for case in cases}), 3)
        for case in cases:
            with self.subTest(case=case["name"]):
                workload = workloads[case["name"]]
                self.assertEqual(case["project"], workload.project)
                self.assertEqual(case["technology"], workload.technology)
                self.assertEqual(
                    case["original_agent_command"],
                    workload.original_agent_command,
                )
                self.assertEqual(
                    case["source_observation"], workload.source_observation
                )
                self.assertEqual(case["success_signal"], workload.success_signal)
                self.assertEqual(case["repeats"], REPEATS)
                self.assertEqual(len(case["runs"]), REPEATS)
                self.assertEqual(
                    {run["role"] for run in case["runs"]},
                    {"agent_reference", "rerun"},
                )
                self.assertTrue(
                    all(run["exit_code"] == 0 for run in case["runs"])
                )
                self.assertEqual(
                    case["command_source"], "Codex task record"
                )
                self.assertTrue(case["source_observation"])
                raw_output = "\n".join(run["output"] for run in case["runs"])
                qurtail_output = case["qurtail"]["output"]
                self.assertEqual(case["qurtail"]["exit_code"], 0)
                self.assertIn(
                    case["success_signal"].casefold(), qurtail_output.casefold()
                )
                self.assertLess(len(qurtail_output), len(raw_output))

    def test_corpus_excludes_machine_local_paths(self) -> None:
        serialized = CORPUS_PATH.read_text(encoding="utf-8")

        self.assertNotIn(str(ROOT), serialized)
        self.assertNotIn(str(Path(sys.executable)), serialized)
        self.assertNotIn(str(Path.home()), serialized)
        self.assertIn("<elapsed>", serialized)
        lowered = serialized.casefold()
        self.assertIsNone(re.search(r"c:\\+users\\+", lowered))
        for fragment in ("/home/", "wsl.localhost", "local-user"):
            self.assertNotIn(fragment, lowered)
        self.assertIsNone(re.search(r"\b019f[0-9a-f-]{20,}\b", lowered))

    def test_sanitizer_removes_paths_ansi_package_names_and_timing(self) -> None:
        raw = (
            "\x1b[31m> private-package@1.2.3 test\x1b[0m\n"
            "RUN C:\\Users\\person\\project\n"
            "Start at 15:08:37\n"
            "Duration 1.23s (setup 42ms)"
        )

        sanitized = _sanitize(
            raw,
            [
                (r"C:\Users\person\project", "<repo:test>"),
                (r"C:\Users\person", "<home>"),
            ],
        )

        self.assertNotIn("\x1b", sanitized)
        self.assertNotIn("private-package", sanitized)
        self.assertNotIn(r"C:\Users\person", sanitized)
        self.assertIn("> <package>@<version> test", sanitized)
        self.assertIn("RUN <repo:test>", sanitized)
        self.assertIn("Start at <time>", sanitized)
        self.assertIn("Duration <elapsed> (setup <elapsed>)", sanitized)

    def test_child_environment_excludes_capture_paths_and_common_secrets(self) -> None:
        additions = {
            "example_api_key": "private",
            "AWS_SECRET_ACCESS_KEY": "private",
            "GITHUB_TOKEN": "private",
            "qurtail_corpus_test_root": r"C:\private",
            "PYTHONPATH": r"C:\foreign",
        }

        with patch.dict(os.environ, additions, clear=False):
            environment = _environment(WORKLOADS[1])

        for name in additions:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["CI"], "1")
        self.assertEqual(environment["NO_COLOR"], "1")

    def test_comparison_report_exists_and_states_its_limits(self) -> None:
        report = REPORT_PATH.read_text(encoding="utf-8")

        self.assertIn("# Codex live workload replay", report)
        self.assertIn("real qurtail CLI", report)
        self.assertIn("separate local Git projects", report)
        self.assertIn("## Original agent command lines", report)
        self.assertIn("copied from its Codex task record", report)
        for workload in WORKLOADS:
            self.assertEqual(
                report.count(f"`{workload.original_agent_command}`"),
                1,
            )
        self.assertIn("fresh reruns of those source commands", report)
        self.assertIn("do not claim byte-for-byte identity", report)
        self.assertIn("similarity = 1.0", report)
        self.assertIn("rather than fuzzy-match quality", report)
        self.assertIn("Live file-follow performance", report)


if __name__ == "__main__":
    unittest.main()
