"""Measure baselines and qurtail output on the large local corpus."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.large_corpus import (  # noqa: E402
    CorpusError,
    DEFAULT_CORPUS_ROOT,
    INDEX_NAME,
    MANIFEST_PATH,
    hash_file,
    load_manifest,
    selected_datasets,
)
from benchmarks.run_performance import MIN_RECORDS_PER_SECOND  # noqa: E402
from qurtail import _StreamReducer  # noqa: E402


HTTP_ERROR_STATUS = re.compile(rb'\"\s+[45][0-9]{2}(?:\s|$)')
BLOCK_ID = re.compile(rb"blk_-?\d+")
APPLICATION_ID = re.compile(r"application_\d+_\d+")
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def _format_bytes(size: int) -> str:
    """Render a compact byte count in reports."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _json_sha256(value: object) -> str:
    """Hash a JSON value using a stable serialization."""
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _has_generic_signal(line: bytes) -> bool:
    """Recognize the benchmark's generic warning and error grep terms."""
    lowered = line.lower()
    return (
        b"error" in lowered
        or b"warn" in lowered
        or b"critical" in lowered
        or b"fatal" in lowered
        or b"panic" in lowered
        or b"exception" in lowered
        or b"fail" in lowered
        or b"denied" in lowered
        or b"refused" in lowered
        or b"unavailable" in lowered
        or b"deadlock" in lowered
        or b"segfault" in lowered
        or b"crash" in lowered
        or (b'\"' in lowered and HTTP_ERROR_STATUS.search(lowered) is not None)
    )


def _corpus_root(value: str | None) -> Path:
    """Resolve the configured local corpus location."""
    selected = value or os.environ.get("QURTAIL_CORPUS_ROOT")
    return Path(selected).expanduser().resolve() if selected else DEFAULT_CORPUS_ROOT


def load_index(corpus_root: Path) -> dict[str, object]:
    """Load the local index and require it to match the committed manifest."""
    index_path = corpus_root / INDEX_NAME
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(
            f"cannot load {index_path}; run benchmarks/large_corpus.py verify"
        ) from error
    expected_hash = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    if index.get("manifest_sha256") != expected_hash:
        raise CorpusError(
            "local corpus index is stale; run benchmarks/large_corpus.py verify"
        )
    return index


def verify_index_record(
    corpus_root: Path, index_record: dict[str, object]
) -> None:
    """Bind a selected benchmark run to the indexed local file bytes."""
    data_root = corpus_root / "data"
    for key in ("log_files", "label_files"):
        records = index_record.get(key, [])
        if not isinstance(records, list):
            raise CorpusError(f"invalid {key} in local corpus index")
        for file_record in records:
            if not isinstance(file_record, dict):
                raise CorpusError(f"invalid {key} entry in local corpus index")
            path = _indexed_path(data_root, str(file_record["path"]))
            expected_hash = file_record.get("sha256")
            if not isinstance(expected_hash, str):
                raise CorpusError(
                    "local corpus index lacks file hashes; run "
                    "benchmarks/large_corpus.py verify"
                )
            if path.stat().st_size != int(file_record["bytes"]):
                raise CorpusError(
                    f"indexed corpus file changed size: {file_record['path']}"
                )
            actual_hash = hash_file(path, "sha256")
            if actual_hash != expected_hash:
                raise CorpusError(
                    f"indexed corpus file hash changed: {file_record['path']}"
                )


class GroundTruth:
    """Map each input record to stable upstream anomaly units when available."""

    def __init__(self, dataset: dict[str, object], dataset_root: Path) -> None:
        self.kind = "none"
        self.unit = "none"
        self.expected_units: int | None = None
        self.field_index = 0
        self.normal_value = b"-"
        self.anomaly_blocks: set[bytes] = set()
        self.anomaly_applications: set[str] = set()
        self.identifiers: tuple[bytes, ...] = ()

        truth = dataset.get("ground_truth")
        if not isinstance(truth, dict):
            return
        self.kind = str(truth["kind"])
        self.unit = str(truth["unit"])
        if truth.get("expected_anomaly_units") is not None:
            self.expected_units = int(truth["expected_anomaly_units"])
        if self.kind == "inline-field":
            self.field_index = int(truth["field_index"])
            self.normal_value = str(truth["normal_value"]).encode("utf-8")
        elif self.kind == "block-label-csv":
            path = dataset_root / str(truth["path"])
            with path.open("r", encoding="utf-8", newline="") as source:
                for row in csv.DictReader(source):
                    if row.get("Label") == "Anomaly":
                        self.anomaly_blocks.add(row["BlockId"].encode("ascii"))
        elif self.kind == "identifier-list":
            path = dataset_root / str(truth["path"])
            identifiers = []
            for line in path.read_text(encoding="utf-8").splitlines():
                value = line.strip().casefold()
                if UUID.fullmatch(value):
                    identifiers.append(value.encode("ascii"))
            self.identifiers = tuple(identifiers)
        elif self.kind == "application-list":
            path = dataset_root / str(truth["path"])
            current_is_normal = False
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.endswith(":"):
                    current_is_normal = stripped.casefold() == "normal:"
                    continue
                match = APPLICATION_ID.search(stripped)
                if match and not current_is_normal:
                    self.anomaly_applications.add(match.group(0))
        else:
            raise CorpusError(f"unsupported ground-truth kind: {self.kind}")

    def units_for(
        self, relative_path: str, line_number: int, line: bytes
    ) -> set[str]:
        """Return anomaly units represented by one source record."""
        if self.kind == "inline-field":
            fields = line.split()
            if (
                len(fields) > self.field_index
                and fields[self.field_index] != self.normal_value
            ):
                return {f"{relative_path}:{line_number}"}
            return set()
        if self.kind == "block-label-csv":
            return {
                block.decode("ascii")
                for block in BLOCK_ID.findall(line)
                if block in self.anomaly_blocks
            }
        if self.kind == "identifier-list":
            lowered = line.lower()
            return {
                identifier.decode("ascii")
                for identifier in self.identifiers
                if identifier in lowered
            }
        if self.kind == "application-list":
            match = APPLICATION_ID.search(relative_path)
            if match and match.group(0) in self.anomaly_applications:
                return {match.group(0)}
        return set()


@dataclass
class MethodMetrics:
    """Accumulate emitted records, bytes, and represented anomaly units."""

    records: int = 0
    bytes: int = 0
    units: set[str] = field(default_factory=set)

    def add(self, line: bytes, units: set[str]) -> None:
        """Record one full emitted line."""
        self.records += 1
        self.bytes += len(line)
        self.units.update(units)


class CountingWriter:
    """Count qurtail writes while recognizing full retained source records."""

    def __init__(self) -> None:
        self.characters = 0
        self.bytes = 0
        self.newlines = 0
        self.units: set[str] = set()
        self.current_printable = ""
        self.current_units: set[str] = set()

    def set_current(self, printable: str, units: set[str]) -> None:
        """Set the input record synchronously being processed by qurtail."""
        self.current_printable = printable
        self.current_units = units

    def write(self, text: str) -> int:
        """Count output and retain units only when their full source line is visible."""
        self.characters += len(text)
        self.bytes += len(text.encode("utf-8"))
        self.newlines += text.count("\n")
        if text == self.current_printable + "\n":
            self.units.update(self.current_units)
        return len(text)

    def flush(self) -> None:
        """Accept qurtail's streaming flush contract without materializing output."""

    def isatty(self) -> bool:
        """Make measurements match redirected agent output."""
        return False


def _unit_recall(retained: set[str], available: set[str]) -> float | None:
    """Calculate stable anomaly-unit recall when ground truth exists."""
    if not available:
        return None
    return len(retained) / len(available)


def _indexed_path(data_root: Path, relative_path: str) -> Path:
    """Resolve a generated index path without allowing it outside corpus data."""
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise CorpusError(f"unsafe path in local corpus index: {relative_path!r}")
    path = data_root.joinpath(*relative.parts).resolve()
    try:
        path.relative_to(data_root.resolve())
    except ValueError as error:
        raise CorpusError(
            f"unsafe path in local corpus index: {relative_path!r}"
        ) from error
    return path


def _method_result(
    metrics: MethodMetrics, raw: MethodMetrics, available_units: set[str]
) -> dict[str, object]:
    """Render one byte-based baseline result."""
    reduction = 0.0 if raw.bytes == 0 else 1 - metrics.bytes / raw.bytes
    return {
        "output_records": metrics.records,
        "output_bytes": metrics.bytes,
        "byte_reduction": reduction,
        "retained_anomaly_units": len(metrics.units),
        "anomaly_unit_recall": _unit_recall(metrics.units, available_units),
    }


def _release_gates(result: dict[str, object]) -> dict[str, bool]:
    """Evaluate scale and safety gates available from one corpus replay."""
    records = int(result["input_records"])
    qurtail_seconds = float(result["qurtail_seconds"])
    qurtail_metrics = result["methods"]["qurtail"]
    recall = qurtail_metrics["anomaly_unit_recall"]
    gates = {
        "at_least_50000_records_per_second": (
            records / max(qurtail_seconds, 1e-12) >= MIN_RECORDS_PER_SECOND
        ),
        "all_available_anomaly_units_retained": (
            recall is None or float(recall) == 1.0
        ),
    }
    if result["dataset"] == "loghub-hdfs-v1" and result["limit"] is None:
        gates["complete_hdfs_replay_below_four_minutes"] = (
            float(result["elapsed_seconds"]) <= 240.0
        )
    return gates


def evaluate_dataset(
    dataset: dict[str, object],
    index_record: dict[str, object],
    corpus_root: Path,
    *,
    limit: int | None = None,
    run_qurtail: bool = False,
    qurtail_options: dict[str, object] | None = None,
) -> dict[str, object]:
    """Stream one dataset through raw, grep, exact, and optional qurtail views."""
    data_root = corpus_root / "data"
    dataset_root = data_root / str(dataset["id"])
    truth = GroundTruth(dataset, dataset_root)
    raw = MethodMetrics()
    grep = MethodMetrics()
    exact = MethodMetrics()
    available_units: set[str] = set()
    writer = CountingWriter() if run_qurtail else None
    started = time.perf_counter()
    qurtail_seconds = 0.0
    remaining = limit
    source_files: list[str] = []

    for file_record in index_record["log_files"]:
        if remaining == 0:
            break
        tail = (
            _StreamReducer(writer, **(qurtail_options or {}))
            if writer
            else None
        )
        relative_path = str(file_record["path"])
        path = _indexed_path(data_root, relative_path)
        source_files.append(relative_path)
        previous: bytes | None = None
        with path.open("rb") as source:
            for line_number, line in enumerate(source, start=1):
                if remaining == 0:
                    break
                units = truth.units_for(relative_path, line_number, line)
                available_units.update(units)
                raw.add(line, units)
                if _has_generic_signal(line):
                    grep.add(line, units)
                if line != previous:
                    exact.add(line, units)
                previous = line

                if tail is not None and writer is not None:
                    text = line.decode("utf-8", errors="replace")
                    writer.set_current(text.rstrip("\r\n"), units)
                    qurtail_started = time.perf_counter()
                    suppressed = tail.process(text)
                    qurtail_seconds += time.perf_counter() - qurtail_started
                    if suppressed and truth.unit == "line":
                        # An exact count represents repeated line occurrences. Stable
                        # block, application, and instance identifiers need a full
                        # visible record before they count as retained evidence.
                        writer.units.update(units)
                if remaining is not None:
                    remaining -= 1
        if tail is not None:
            qurtail_started = time.perf_counter()
            tail.finish()
            qurtail_seconds += time.perf_counter() - qurtail_started
    elapsed = time.perf_counter() - started
    if (
        limit is None
        and truth.expected_units is not None
        and len(available_units) != truth.expected_units
    ):
        raise CorpusError(
            f"{dataset['id']}: found {len(available_units):,} anomaly units; "
            f"expected {truth.expected_units:,}"
        )

    methods = {
        "raw": _method_result(raw, raw, available_units),
        "grep": _method_result(grep, raw, available_units),
        "adjacent_exact": _method_result(exact, raw, available_units),
    }
    if writer is not None:
        methods["qurtail"] = {
            "output_newlines": writer.newlines,
            "output_characters": writer.characters,
            "output_bytes": writer.bytes,
            "byte_reduction": (
                0.0 if raw.bytes == 0 else 1 - writer.bytes / raw.bytes
            ),
            "retained_anomaly_units": len(writer.units),
            "anomaly_unit_recall": _unit_recall(writer.units, available_units),
        }
    result = {
        "dataset": dataset["id"],
        "source_revision": dataset["source_revision"],
        "source_files": source_files,
        "ground_truth_kind": truth.kind,
        "ground_truth_unit": truth.unit,
        "input_records": raw.records,
        "input_bytes": raw.bytes,
        "available_anomaly_units": len(available_units),
        "expected_anomaly_units": truth.expected_units,
        "limit": limit,
        "elapsed_seconds": elapsed,
        "baseline_scan_seconds": max(0.0, elapsed - qurtail_seconds),
        "qurtail_seconds": qurtail_seconds if writer is not None else None,
        "methods": methods,
    }
    if writer is not None:
        result["release_gates"] = _release_gates(result)
        result["release_gates_passed"] = all(result["release_gates"].values())
    return result


def _print_result(result: dict[str, object]) -> None:
    """Print a compact benchmark summary before the complete JSON result."""
    print(
        f"{result['dataset']}: {int(result['input_records']):,} records, "
        f"{_format_bytes(int(result['input_bytes']))}, "
        f"{int(result['available_anomaly_units']):,} anomaly unit(s), "
        f"{float(result['elapsed_seconds']):.3f}s"
    )
    for name, metrics in result["methods"].items():
        recall = metrics.get("anomaly_unit_recall")
        recall_text = "n/a" if recall is None else f"{float(recall):.1%}"
        reduction = metrics.get("byte_reduction")
        reduction_text = "n/a" if reduction is None else f"{float(reduction):.1%}"
        output = metrics.get("output_records", metrics.get("output_newlines"))
        print(
            f"  {name:<15} output={int(output):>12,}  "
            f"reduction={reduction_text:>7}  anomaly-unit-recall={recall_text}"
        )
    if result["qurtail_seconds"] is not None:
        records = int(result["input_records"])
        baseline_seconds = float(result["baseline_scan_seconds"])
        qurtail_seconds = float(result["qurtail_seconds"])
        print(
            "  timing          "
            f"baseline-scan={baseline_seconds:.3f}s "
            f"({records / max(baseline_seconds, 1e-12):,.0f} records/s), "
            f"qurtail={qurtail_seconds:.3f}s "
            f"({records / max(qurtail_seconds, 1e-12):,.0f} records/s)"
        )
    for name, passed in result.get("release_gates", {}).items():
        print(f"  gate             {name}={'PASS' if passed else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    """Evaluate selected local corpus datasets and optionally write JSON results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", help="dataset ids from the corpus manifest")
    parser.add_argument("--root", help="local corpus root")
    parser.add_argument("--limit", type=int, help="maximum records per dataset")
    parser.add_argument(
        "--qurtail",
        action="store_true",
        help="also measure qurtail after the raw, grep, and exact baselines",
    )
    parser.add_argument(
        "--baselines-only",
        action="store_true",
        help="skip qurtail and measure only the raw, grep, and exact baselines",
    )
    parser.add_argument("--dot-every", type=int, default=10)
    parser.add_argument("--output", type=Path, help="write complete JSON results")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.dot_every < 1:
        parser.error("--dot-every must be positive")
    if args.qurtail and args.baselines_only:
        parser.error("--qurtail and --baselines-only cannot be combined")
    run_qurtail = args.qurtail

    try:
        corpus_root = _corpus_root(args.root)
        manifest = load_manifest()
        datasets = selected_datasets(manifest, args.datasets)
        index = load_index(corpus_root)
        index_by_id = {
            str(record["id"]): record for record in index["datasets"]
        }
        results = []
        corpus_index: dict[str, dict[str, object]] = {}
        qurtail_options = {
            "dot_every": args.dot_every,
        }
        for dataset in datasets:
            dataset_id = str(dataset["id"])
            if dataset_id not in index_by_id:
                raise CorpusError(
                    f"{dataset_id} is missing from the local index; fetch or verify it"
                )
            index_record = index_by_id[dataset_id]
            verify_index_record(corpus_root, index_record)
            corpus_index[dataset_id] = {
                "sha256": _json_sha256(index_record),
                "record": index_record,
            }
            result = evaluate_dataset(
                dataset,
                index_record,
                corpus_root,
                limit=args.limit,
                run_qurtail=run_qurtail,
                qurtail_options=qurtail_options,
            )
            results.append(result)
            _print_result(result)

        payload = {
            "schema_version": 1,
            "run_date": datetime.now(timezone.utc).date().isoformat(),
            "run_environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "qurtail_sha256": hashlib.sha256(
                (ROOT / "qurtail.py").read_bytes()
            ).hexdigest(),
            "evaluator_sha256": {
                "benchmarks/large_corpus.py": hashlib.sha256(
                    (ROOT / "benchmarks" / "large_corpus.py").read_bytes()
                ).hexdigest(),
                "benchmarks/run_large_corpus.py": hashlib.sha256(
                    (ROOT / "benchmarks" / "run_large_corpus.py").read_bytes()
                ).hexdigest(),
            },
            "manifest_sha256": index["manifest_sha256"],
            "corpus_index": corpus_index,
            "configuration": {
                "limit": args.limit,
                "qurtail": run_qurtail,
                "qurtail_options": qurtail_options if run_qurtail else None,
            },
            "results": results,
        }
        serialized = json.dumps(payload, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
            print(f"wrote {args.output}")
        return 0 if all(
            result.get("release_gates_passed", True) for result in results
        ) else 1
    except (CorpusError, OSError, ValueError) as error:
        print(f"large corpus benchmark error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
