"""Ensure dot/count audits reject unknown patterns and incorrect accounting."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.run_interleaved import TranscriptAudit
from benchmarks.run_large_corpus import CountingWriter
from benchmarks.prepare_interleaved_agent_cases import prepare


A = "2026-09-10T12:00:00Z INFO service-a cache refresh completed"
B = "2026-09-10T12:00:01Z INFO service-b cache refresh completed"


def audit(source: list[str], rendered: str) -> dict:
    """Validate an independently supplied transcript in small write chunks."""
    writer = TranscriptAudit(source)
    for start in range(0, len(rendered), 7):
        writer.write(rendered[start : start + 7])
    return writer.finish()


class TranscriptAuditTests(unittest.TestCase):
    """Test the benchmark oracle with deliberately corrupted transcripts."""

    def test_mixed_repeat_count_accounts_for_every_source_record(self) -> None:
        current = A.replace("12:00:00", "12:00:04")
        report = audit(
            [A, B, A, B, current, current],
            f"{A}\n{B}\n.... [4 similar before stop]\n",
        )
        self.assertEqual(report["records"], 6)
        self.assertEqual(report["full_records"], 2)
        self.assertEqual(report["suppressed_records"], 4)

    def test_missing_or_extra_occurrences_are_rejected(self) -> None:
        for rendered in (A + "\n", A + "\n" + B + "\n" + B + "\n"):
            with self.subTest(rendered=rendered), self.assertRaises(ValueError):
                audit([A, B], rendered)

    def test_count_cannot_hide_an_unfamiliar_message(self) -> None:
        for changed in (A.replace("service-a", "service-c"), "ERROR changed state", "{malformed"):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                audit([A, B, changed], f"{A}\n{B}\n. [1 similar before stop]\n")

    def test_adjacent_only_audit_rejects_mixed_patterns(self) -> None:
        writer = TranscriptAudit([A, B, A], interleaving=False)
        with self.assertRaisesRegex(ValueError, "adjacent"):
            writer.write(f"{A}\n{B}\n. [1 similar before stop]\n")

    def test_wrong_adjacent_pattern_and_count_are_rejected(self) -> None:
        for source in ([A, B], [A, A, A]):
            with self.subTest(source=source), self.assertRaises(ValueError):
                audit(source, A + "\n. [1 similar before stop]\n")

    def test_literal_source_markers_are_not_mistaken_for_generated_records(self) -> None:
        source = ["[4 similar before stop]", ".. [2 similar in 0s]"]
        self.assertEqual(audit(source, "\n".join(source) + "\n")["full_records"], 2)

    def test_large_corpus_credit_requires_a_full_source_record(self) -> None:
        writer = CountingWriter()
        writer.set_current(A, {"instance-a"})
        writer.write(f"{A}\n")
        self.assertEqual(writer.units, {"instance-a"})
        writer.set_current(B, {"instance-b"})
        writer.write(". [1 similar before stop]\n")
        self.assertEqual(writer.units, {"instance-a"})


class RetainedEvaluationTests(unittest.TestCase):
    """Keep release evidence tied to the actual producer, evaluator, and fixtures."""

    def test_real_log_report_matches_source_and_required_gates(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = json.loads((root / "benchmarks/interleaved-results.json").read_text())
        self.assertEqual(report["qurtail_sha256"], hashlib.sha256((root / "qurtail.py").read_bytes()).hexdigest())
        for path, expected in report["evaluator_sha256"].items():
            self.assertEqual(hashlib.sha256((root / path).read_bytes()).hexdigest(), expected)
        self.assertEqual(report["manifest_sha256"], hashlib.sha256(
            (root / "benchmarks/corpus/manifest.json").read_bytes()).hexdigest())
        self.assertTrue(report["passed"])
        self.assertTrue(all(report["gates"].values()))
        self.assertEqual({r["dataset"] for r in report["results"]},
                         {"loghub-zookeeper", "loghub-linux", "loghub-openssh"})
        for dataset in report["results"]:
            for file in dataset["files"]:
                view = file["views"]["current"]
                self.assertEqual(view["records"], view["full_records"] +
                                 view["suppressed_records"])
                self.assertTrue(view["all_occurrences_accounted_for"])
                if dataset["dataset"] == "loghub-zookeeper":
                    self.assertGreaterEqual(view["byte_reduction"], 0.25)

    def test_agent_receipts_match_regenerated_evidence_and_answer_keys(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = json.loads((root / "benchmarks/interleaved-agent-results.json").read_text())
        for field, path in (("receipts_sha256", "benchmarks/interleaved-agent-receipts.json"),
                            ("scorer_sha256", "benchmarks/score_interleaved_agents.py")):
            self.assertEqual(report[field], hashlib.sha256((root / path).read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            manifest = prepare(Path(directory))
        self.assertEqual(report["fixture_manifest"], manifest)
        keys = {case["id"]: case["expected"] for case in manifest["cases"]}
        self.assertEqual(len(report["results"]), 6)
        for result in report["results"]:
            self.assertEqual(result["answers"], keys[result["case"]])
            self.assertTrue(result["passed"])
        for view in ("raw", "compact"):
            self.assertEqual(report["total_task_and_evidence_tokens"][view],
                             sum(r["task_and_evidence_tokens"] for r in report["results"] if r["view"] == view))
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
