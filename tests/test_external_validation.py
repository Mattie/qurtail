"""Check external-corpus framing, null boundaries, and evidence denominators."""

import hashlib
import importlib.util
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmarks.validate_external_logs import (
    EvidenceAudit, main, physical_records, render_rca_rows, signal_checks,
)
from benchmarks.read_external_evidence import read_evidence
from benchmarks.fetch_external_validation import fetch_one, rca_entries


class ExternalValidationTests(unittest.TestCase):
    """Reject misleading accounting without depending on downloaded corpora."""

    def test_physical_framing_preserves_lone_carriage_returns_and_empty_records(self):
        self.assertEqual(physical_records(b"a\rb\n\nlast\r\n"), ["a\rb", "", "last"])
        self.assertEqual(physical_records(b"a\v\fb"), ["a\v\fb"])
        self.assertEqual(physical_records(b""), [])

    def test_rca_metadata_rejects_tampered_cached_files(self):
        def table_metadata(url):
            case = url.rsplit("/", 1)[1].split("?", 1)[0]
            return [{"path": f"{case}/logs.parquet", "size": 1, "oid": "fixture"}]

        with patch("benchmarks.fetch_external_validation.read_json", side_effect=table_metadata):
            entries = [entry for entry in rca_entries()
                       if entry["path"] in ("rcaeval/LICENSE", "rcaeval/README.md")]
        self.assertEqual(len(entries), 2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rcaeval").mkdir()
            for entry in entries:
                with self.subTest(path=entry["path"]):
                    (root / entry["path"]).write_bytes(b"tampered metadata")
                    with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                        fetch_one(root, entry)

    def test_rca_view_keeps_metadata_embedded_times_newlines_and_null_rows(self):
        records, metadata = render_rca_rows([
            {"timestamp": 2, "container_name": "worker", "message": "body 2026-01-01T01:02:03Z\n  trace"},
            {"timestamp": 1, "container_name": "other", "message": None},
            {"timestamp": 3, "container_name": "other", "message": ""},
        ])
        self.assertEqual(records, [
            '1970-01-01T00:00:02Z container="worker" body 2026-01-01T01:02:03Z',
            "  trace",
            '[RCAEval null message] 1970-01-01T00:00:01Z container="other"',
            '1970-01-01T00:00:03Z container="other" ',
        ])
        self.assertEqual(metadata["source_rows"], 3)
        self.assertEqual(metadata["null_messages"], 1)
        self.assertEqual(metadata["backwards_timestamp_steps"], 1)

    def test_mixed_count_is_accounted_for_without_claiming_individual_visibility(self):
        a, b = "INFO service-a completed its scheduled refresh", "INFO service-b waiting"
        with tempfile.TemporaryDirectory() as directory:
            audit = EvidenceAudit([a, b, a, a], Path(directory) / "out", lambda text: len(text))
            try:
                audit.write(f"{a}\n{b}\n.. [2 similar before stop]\n")
                report = audit.result()
                self.assertEqual(list(audit.visible), [1, 1, 0, 0])
                self.assertEqual(audit.resolved, [a, b])
                self.assertEqual(report["suppressed_records"], 2)
            finally:
                audit.output.close()

    def test_missing_raw_signal_does_not_inflate_retention_denominator(self):
        labels = {"required_signals": [
            {"type": "error", "value": "request_id=original", "importance": "critical"},
            {"type": "error", "value": "annotation absent", "importance": "critical", "aliases": ["failed"]},
        ], "evidence_spans": [{"start_line": 1, "end_line": 2},
                             {"start_line": 3, "end_line": 9}]}
        report = signal_checks(labels, "failed request_id=original", "failed request_id=other",
                               bytearray([1, 0, 1]), 3)
        self.assertEqual(report["critical_primary_present_raw"], 1)
        self.assertEqual(report["critical_primary_lost"], 1)
        self.assertEqual(report["primary_absent_from_raw"], 1)
        self.assertEqual(report["span_records_individually_visible"], 1)
        self.assertEqual(report["invalid_annotated_spans"], [[3, 9]])

    def test_saved_report_covers_pinned_selection_and_current_sources(self):
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "benchmarks/external-corpus-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        report = json.loads((root / "benchmarks/external-validation-results.json").read_text())
        self.assertEqual(report["manifest_sha256"], hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        for name, digest in {"qurtail.py": report["qurtail_sha256"], **report["evaluator_sha256"]}.items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)
        expected = {entry["path"]: entry["sha256"] for entry in manifest["files"]
                    if entry["path"].endswith((".log", ".parquet"))}
        actual = {case["input_path"]: case["source_sha256"] for case in report["results"]}
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len(report["results"]))
        self.assertTrue(report["all_protocol_audits_passed"])
        self.assertTrue(report["opt_out_matches_baseline"])
        self.assertTrue(report["passed"])
        self.assertTrue(all(view["audit_passed"] for case in report["results"]
                            for view in case["views"].values()))

    def test_cli_rejects_opt_out_byte_difference_even_when_all_audits_pass(self):
        # Token counts and Parquet metadata do not affect this text-only exit gate.
        packages = {
            "tiktoken": SimpleNamespace(__version__="test", get_encoding=lambda name:
                                       SimpleNamespace(encode=lambda text, **kwargs: text)),
            "pyarrow": SimpleNamespace(__version__="test"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b"A\nA\nA\n"
            (root / "input.log").write_bytes(data)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": [{"family": "rootly",
                "path": "input.log", "sha256": hashlib.sha256(data).hexdigest()}]}))
            output = root / "report.json"
            for density, expected_status in ((10, 0), (1, 1)):
                with self.subTest(dot_every=density):
                    # Both baselines account for all records; only dot density differs.
                    baseline = root / f"baseline_{density}.py"
                    baseline.write_text(
                        "from qurtail import _StreamReducer as Reducer\n"
                        "class _StreamReducer(Reducer):\n"
                        "    def __init__(self, output, **kwargs):\n"
                        f"        kwargs['dot_every'] = {density}\n"
                        "        super().__init__(output, interleaving=False, **kwargs)\n")
                    argv = ["validate", "--root", str(root), "--manifest", str(manifest),
                            "--baseline-source", str(baseline), "--output", str(output)]
                    with patch.dict(sys.modules, packages), patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                        status = main()
                    report = json.loads(output.read_text())
                    self.assertTrue(report["all_protocol_audits_passed"])
                    self.assertEqual(report["opt_out_matches_baseline"], expected_status == 0)
                    self.assertEqual(report["passed"], expected_status == 0)
                    self.assertEqual(status, expected_status)

    @unittest.skipUnless(importlib.util.find_spec("tiktoken"), "benchmark tokenizer optional")
    def test_payload_scoring_counts_rereads_rejects_tampering_and_excludes_truncation(self):
        from benchmarks.score_external_agents import score
        import tiktoken

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arms, receipts = {}, []
            for arm, view in (("a", "raw"), ("b", "baseline"), ("c", "current")):
                folder = root / "case" / arm
                folder.mkdir(parents=True)
                data = b"Evidence without final newline"
                (folder / "task.md").write_bytes(data)
                payload = read_evidence(folder, ["task.md", "task.md"])
                arms[arm] = {"view": view, "files": {"task.md": hashlib.sha256(data).hexdigest()}}
                receipts.append({"case": "case", "arm": arm, "answers": {"ok": True},
                                 "files_read": ["task.md", "task.md"], "delivery_complete": True})
            (root / "manifest.json").write_text(json.dumps({"cases": [
                {"id": "case", "expected": {"ok": True}, "arms": arms}]}))
            receipt_path = root / "receipts.json"

            def run(items):
                receipt_path.write_text(json.dumps({"protocol": "test", "receipts": items}))
                return score(root, receipt_path)

            report = run(receipts)
            encoder = tiktoken.get_encoding("o200k_base")
            expected_tokens = 2 * len(encoder.encode(payload[:len(payload) // 2]))
            self.assertEqual(report["complete_case_emitted_tokens"]["current"], expected_tokens)
            receipts[2]["delivery_complete"] = False
            self.assertEqual(run(receipts)["complete_cases"], [])
            with self.assertRaisesRegex(ValueError, "missing"):
                run(receipts[:2])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                run(receipts + receipts[:1])
            (root / "case/a/task.md").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "changed"):
                run(receipts)


if __name__ == "__main__":
    unittest.main()
