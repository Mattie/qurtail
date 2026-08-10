"""Verify the reproducible large-corpus tooling without network access."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from benchmarks.large_corpus import (
    CorpusError,
    MANIFEST_PATH,
    _extract_zip,
    _extract_tar,
    build_index,
    count_lines,
    extract_dataset,
    inspect_dataset,
    load_manifest,
    selected_datasets,
    verify_artifact,
)
from benchmarks.run_large_corpus import (
    GroundTruth,
    _has_generic_signal,
    _json_sha256,
    _release_gates,
    evaluate_dataset,
    verify_index_record,
)


def write_fixture_completion_marker(root: Path, dataset_id: str) -> None:
    """Bind a fixture marker to every file currently under its dataset root."""
    extracted_files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != ".complete.json":
            extracted_files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    (root / ".complete.json").write_text(
        json.dumps(
            {
                "dataset": dataset_id,
                "artifacts": [],
                "extracted_files": extracted_files,
            }
        )
        + "\n",
        encoding="utf-8",
    )


class LargeCorpusManifestTests(unittest.TestCase):
    """Keep the committed inventory large, pinned, and locally reproducible."""

    def test_manifest_has_multiple_long_and_massive_datasets(self) -> None:
        manifest = load_manifest()
        datasets = manifest["datasets"]
        line_counts = [int(dataset["official_line_count"]) for dataset in datasets]

        self.assertGreaterEqual(sum(line_counts), 20_000_000)
        self.assertGreaterEqual(sum(lines >= 100_000 for lines in line_counts), 6)
        self.assertGreaterEqual(sum(lines >= 1_000_000 for lines in line_counts), 3)
        self.assertGreaterEqual(
            len({dataset["family"] for dataset in datasets}),
            5,
        )

    def test_manifest_pins_provenance_downloads_and_usage_terms(self) -> None:
        manifest = load_manifest()

        for dataset in manifest["datasets"]:
            with self.subTest(dataset=dataset["id"]):
                self.assertTrue(dataset["provenance_url"].startswith("https://"))
                self.assertTrue(dataset["source_revision"])
                self.assertEqual(dataset["license"]["status"], "local-research")
                self.assertTrue(dataset["license"]["url"].startswith("https://"))
                self.assertLessEqual(
                    int(dataset["minimum_line_count"]),
                    int(dataset["expected_line_count"]),
                )
                for artifact in dataset["artifacts"]:
                    self.assertTrue(artifact["url"].startswith("https://"))
                    self.assertGreater(int(artifact["size"]), 0)
                    self.assertRegex(
                        artifact["checksum"],
                        r"^(md5|sha256):[0-9a-f]+$",
                    )
                truth = dataset.get("ground_truth")
                if truth:
                    self.assertGreater(int(truth["expected_anomaly_units"]), 0)

    def test_dataset_selection_rejects_unknown_ids(self) -> None:
        manifest = load_manifest()

        with self.assertRaisesRegex(CorpusError, "unknown dataset"):
            selected_datasets(manifest, ["missing-dataset"])

    def test_manifest_is_valid_json_with_stable_schema(self) -> None:
        serialized = MANIFEST_PATH.read_text(encoding="utf-8")

        self.assertEqual(json.loads(serialized)["schema_version"], 1)
        self.assertTrue(serialized.endswith("\n"))


class LargeCorpusFilesystemTests(unittest.TestCase):
    """Exercise archive safety and record counting using small temporary data."""

    def test_verified_zip_extracts_and_indexes_logs_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "fixture.zip"
            with zipfile.ZipFile(archive, "w") as destination:
                destination.writestr("fixture/events.log", "one\ntwo\nthree\n")
                destination.writestr("fixture/anomaly_label.csv", "id,label\n1,normal\n")
            digest = hashlib.md5(archive.read_bytes()).hexdigest()
            artifact = {
                "filename": archive.name,
                "size": archive.stat().st_size,
                "checksum": f"md5:{digest}",
                "archive": "zip",
            }
            dataset = {
                "id": "fixture",
                "title": "Fixture",
                "family": "test",
                "official_line_count": 3,
                "expected_line_count": 3,
                "minimum_line_count": 3,
                "artifacts": [artifact],
                "log_globs": ["**/*.log"],
                "label_globs": ["**/*label*.csv"],
            }

            sha256 = verify_artifact(archive, artifact)
            extracted = extract_dataset(
                dataset,
                [(archive, sha256)],
                root / "data",
            )
            record = inspect_dataset(dataset, root / "data")

            self.assertTrue((extracted / ".complete.json").is_file())
            marker = json.loads(
                (extracted / ".complete.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(marker["extracted_files"]), 2)
            self.assertEqual(record["lines"], 3)
            self.assertEqual(len(record["log_files"]), 1)
            self.assertEqual(len(record["label_files"]), 1)
            verify_index_record(root, record)

            with self.assertRaisesRegex(CorpusError, "does not match"):
                extract_dataset(
                    dataset,
                    [(archive, "0" * 64)],
                    root / "data",
                )

            events = extracted / "fixture" / "events.log"
            events.write_text("one\ntwo\nother\n", encoding="utf-8")
            with self.assertRaisesRegex(CorpusError, "hash changed"):
                verify_index_record(root, record)
            with self.assertRaisesRegex(CorpusError, "extracted files changed"):
                inspect_dataset(dataset, root / "data")
            with self.assertRaisesRegex(CorpusError, "extracted files changed"):
                build_index(
                    {"schema_version": 1, "datasets": [dataset]},
                    root,
                    [dataset],
                )

    def test_zip_extraction_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "unsafe.zip"
            destination = root / "destination"
            destination.mkdir()
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escaped.log", "unsafe\n")

            with self.assertRaisesRegex(CorpusError, "unsafe archive member"):
                _extract_zip(archive, destination)
            self.assertFalse((root / "escaped.log").exists())

    def test_tar_extraction_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "unsafe.tar.gz"
            destination = root / "destination"
            destination.mkdir()
            payload = b"unsafe\n"
            with tarfile.open(archive, "w:gz") as output:
                member = tarfile.TarInfo("../escaped.log")
                member.size = len(payload)
                output.addfile(member, io.BytesIO(payload))

            with self.assertRaisesRegex(CorpusError, "unsafe archive member"):
                _extract_tar(archive, destination)
            self.assertFalse((root / "escaped.log").exists())

    def test_line_count_includes_a_final_unterminated_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.log"
            path.write_bytes(b"first\nsecond")

            self.assertEqual(count_lines(path), 2)

    def test_inspection_rejects_a_dataset_below_its_scale_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            dataset_root = data_root / "small"
            dataset_root.mkdir(parents=True)
            (dataset_root / "events.log").write_text("one\ntwo\n", encoding="utf-8")
            write_fixture_completion_marker(dataset_root, "small")
            dataset = {
                "id": "small",
                "title": "Small",
                "family": "test",
                "official_line_count": 100,
                "expected_line_count": 100,
                "minimum_line_count": 100,
                "log_globs": ["*.log"],
                "label_globs": [],
            }

            with self.assertRaisesRegex(CorpusError, "expected at least 100"):
                inspect_dataset(dataset, data_root)

    def test_selective_index_builds_accumulate_completed_datasets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus_root = Path(temporary)
            data_root = corpus_root / "data"
            datasets = []
            for dataset_id in ("first", "second"):
                root = data_root / dataset_id
                root.mkdir(parents=True)
                (root / "events.log").write_text("event\n", encoding="utf-8")
                write_fixture_completion_marker(root, dataset_id)
                datasets.append(
                    {
                        "id": dataset_id,
                        "title": dataset_id.title(),
                        "family": "test",
                        "official_line_count": 1,
                        "expected_line_count": 1,
                        "minimum_line_count": 1,
                        "log_globs": ["*.log"],
                        "label_globs": [],
                    }
                )
            manifest = {"schema_version": 1, "datasets": datasets}

            build_index(manifest, corpus_root, [datasets[0]])
            index = build_index(manifest, corpus_root, [datasets[1]])

            self.assertEqual(
                [record["id"] for record in index["datasets"]],
                ["first", "second"],
            )


class LargeCorpusBenchmarkTests(unittest.TestCase):
    """Check that proof-corpus baselines measure compression and signal recall."""

    def test_committed_hdfs_report_matches_sources_and_release_gates(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = json.loads(
            (root / "benchmarks" / "hdfs-full-results.json").read_text(
                encoding="utf-8"
            )
        )
        result = report["results"][0]

        self.assertEqual(
            report["qurtail_sha256"],
            hashlib.sha256((root / "qurtail.py").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            report["manifest_sha256"],
            hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        )
        for relative_path, expected_hash in report["evaluator_sha256"].items():
            self.assertEqual(
                expected_hash,
                hashlib.sha256((root / relative_path).read_bytes()).hexdigest(),
            )
        corpus_binding = report["corpus_index"]["loghub-hdfs-v1"]
        self.assertEqual(
            corpus_binding["sha256"],
            _json_sha256(corpus_binding["record"]),
        )
        self.assertTrue(
            all(
                len(file_record["sha256"]) == 64
                for key in ("log_files", "label_files")
                for file_record in corpus_binding["record"][key]
            )
        )
        self.assertEqual(result["dataset"], "loghub-hdfs-v1")
        self.assertEqual(result["input_records"], 11_175_629)
        self.assertEqual(result["available_anomaly_units"], 16_838)
        self.assertEqual(
            result["methods"]["qurtail"]["retained_anomaly_units"],
            16_838,
        )
        self.assertLessEqual(result["elapsed_seconds"], 240)
        self.assertTrue(result["release_gates_passed"])
        self.assertTrue(all(result["release_gates"].values()))

    def test_generic_signal_scan_keeps_terms_and_http_error_statuses(self) -> None:
        for line in (
            b"ERROR connection refused\n",
            b"Warning: disk unavailable\n",
            b'127.0.0.1 "GET / HTTP/1.1" 503 0\n',
        ):
            with self.subTest(line=line):
                self.assertTrue(_has_generic_signal(line))

        self.assertFalse(_has_generic_signal(b"INFO request completed 200\n"))

    def test_hdfs_duration_gate_uses_complete_replay_time(self) -> None:
        result = {
            "dataset": "loghub-hdfs-v1",
            "input_records": 11_175_629,
            "qurtail_seconds": 223.0,
            "baseline_scan_seconds": 277.0,
            "elapsed_seconds": 500.0,
            "limit": None,
            "methods": {
                "qurtail": {"anomaly_unit_recall": 1.0},
            },
        }

        gates = _release_gates(result)

        self.assertFalse(gates["complete_hdfs_replay_below_four_minutes"])

    def test_hdfs_ground_truth_maps_only_annotated_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "anomaly_label.csv").write_text(
                "BlockId,Label\nblk_1,Normal\nblk_-2,Anomaly\n",
                encoding="utf-8",
            )
            dataset = {
                "ground_truth": {
                    "kind": "block-label-csv",
                    "path": "anomaly_label.csv",
                    "unit": "block",
                }
            }

            truth = GroundTruth(dataset, root)

            self.assertEqual(
                truth.units_for(
                    "events.log", 1, b"received blk_-2 and blk_1\n"
                ),
                {"blk_-2"},
            )

    def test_evaluator_compares_baselines_and_retains_anomaly_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus_root = Path(temporary)
            dataset_root = corpus_root / "data" / "fixture"
            dataset_root.mkdir(parents=True)
            events = (
                b"- INFO heartbeat\n" * 100
                + b"A1 ERROR disk failure\n"
                + b"A1 ERROR disk failure\n"
            )
            (dataset_root / "events.log").write_bytes(events)
            dataset = {
                "id": "fixture",
                "source_revision": "test-revision",
                "ground_truth": {
                    "kind": "inline-field",
                    "unit": "line",
                    "field_index": 0,
                    "normal_value": "-",
                    "expected_anomaly_units": 2,
                },
            }
            index_record = {
                "log_files": [
                    {
                        "path": "fixture/events.log",
                        "lines": 102,
                        "bytes": len(events),
                    }
                ]
            }

            result = evaluate_dataset(
                dataset,
                index_record,
                corpus_root,
                run_qurtail=True,
                qurtail_options={"dot_every": 10},
            )

            self.assertEqual(result["input_records"], 102)
            self.assertEqual(result["available_anomaly_units"], 2)
            self.assertEqual(result["methods"]["grep"]["output_records"], 2)
            self.assertEqual(
                result["methods"]["adjacent_exact"]["output_records"],
                2,
            )
            self.assertEqual(
                result["methods"]["qurtail"]["anomaly_unit_recall"],
                1.0,
            )
            self.assertGreater(result["methods"]["qurtail"]["byte_reduction"], 0)
            self.assertGreater(result["qurtail_seconds"], 0)

    def test_suppressed_stable_identifiers_do_not_count_as_visible(self) -> None:
        first = "11111111-1111-1111-1111-111111111111"
        second = "22222222-2222-2222-2222-222222222222"
        with tempfile.TemporaryDirectory() as temporary:
            corpus_root = Path(temporary)
            dataset_root = corpus_root / "data" / "fixture"
            dataset_root.mkdir(parents=True)
            events = (
                f'{{"message":"instance failed {first}"}}\n'
                f'{{"message":"instance failed {second}"}}\n'
            ).encode("utf-8")
            (dataset_root / "events.log").write_bytes(events)
            (dataset_root / "anomaly_labels.txt").write_text(
                f"{first}\n{second}\n",
                encoding="utf-8",
            )
            dataset = {
                "id": "fixture",
                "source_revision": "test-revision",
                "ground_truth": {
                    "kind": "identifier-list",
                    "path": "anomaly_labels.txt",
                    "unit": "vm-instance",
                    "expected_anomaly_units": 2,
                },
            }
            index_record = {
                "log_files": [
                    {
                        "path": "fixture/events.log",
                        "lines": 2,
                        "bytes": len(events),
                    }
                ]
            }

            result = evaluate_dataset(
                dataset,
                index_record,
                corpus_root,
                run_qurtail=True,
            )

        self.assertEqual(result["available_anomaly_units"], 2)
        self.assertEqual(
            result["methods"]["qurtail"]["retained_anomaly_units"],
            1,
        )
        self.assertEqual(
            result["methods"]["qurtail"]["anomaly_unit_recall"],
            0.5,
        )


if __name__ == "__main__":
    unittest.main()
