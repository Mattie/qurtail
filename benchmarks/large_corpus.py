"""Download, extract, and verify qurtail's large local log corpus.

The corpus itself stays outside Git. This module keeps its provenance and the
steps needed to reproduce it in the repository without adding runtime
dependencies to qurtail.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
from typing import BinaryIO, Iterable, Iterator
from urllib.request import Request, urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "benchmarks" / "corpus" / "manifest.json"
DEFAULT_CORPUS_ROOT = ROOT / "benchmarks" / "corpus" / "local"
INDEX_NAME = "index.json"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL = 64 * 1024 * 1024
USER_AGENT = "qurtail-large-corpus/1"


class CorpusError(RuntimeError):
    """Report a reproducibility, download, extraction, or validation failure."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, object]:
    """Load and minimally validate the committed corpus manifest."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"cannot load corpus manifest {path}: {error}") from error

    if manifest.get("schema_version") != 1:
        raise CorpusError("unsupported corpus manifest schema")
    datasets = manifest.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise CorpusError("corpus manifest must contain datasets")

    seen: set[str] = set()
    for dataset in datasets:
        if not isinstance(dataset, dict):
            raise CorpusError("every corpus dataset must be an object")
        dataset_id = dataset.get("id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise CorpusError("every corpus dataset needs a non-empty id")
        if dataset_id in seen:
            raise CorpusError(f"duplicate corpus dataset id: {dataset_id}")
        seen.add(dataset_id)
        if not dataset.get("artifacts") or not dataset.get("log_globs"):
            raise CorpusError(f"{dataset_id}: artifacts and log_globs are required")
        minimum = int(dataset.get("minimum_line_count", 0))
        expected = int(dataset.get("expected_line_count", 0))
        if minimum < 1 or expected < minimum:
            raise CorpusError(
                f"{dataset_id}: expected_line_count must meet the positive scale gate"
            )
    return manifest


def selected_datasets(
    manifest: dict[str, object], requested: Iterable[str]
) -> list[dict[str, object]]:
    """Resolve requested dataset ids, defaulting to the complete manifest."""
    datasets = manifest["datasets"]
    assert isinstance(datasets, list)
    by_id = {str(dataset["id"]): dataset for dataset in datasets}
    requested_ids = list(requested)
    if not requested_ids:
        return list(datasets)

    unknown = sorted(set(requested_ids) - by_id.keys())
    if unknown:
        raise CorpusError(f"unknown dataset id(s): {', '.join(unknown)}")
    return [by_id[dataset_id] for dataset_id in requested_ids]


def _format_bytes(size: int) -> str:
    """Render a byte count for progress and status output."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _parse_checksum(checksum: object) -> tuple[str, str] | None:
    """Parse an algorithm:digest checksum from the manifest."""
    if checksum is None:
        return None
    if not isinstance(checksum, str) or ":" not in checksum:
        raise CorpusError(f"invalid checksum: {checksum!r}")
    algorithm, digest = checksum.split(":", 1)
    try:
        hashlib.new(algorithm)
    except ValueError as error:
        raise CorpusError(f"unsupported checksum algorithm: {algorithm}") from error
    if not digest:
        raise CorpusError("checksum digest cannot be empty")
    return algorithm, digest.casefold()


def hash_file(path: Path, algorithm: str) -> str:
    """Return a streaming digest for a potentially large archive."""
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(path: Path, artifact: dict[str, object]) -> str:
    """Verify one downloaded archive and return its SHA-256 provenance hash."""
    if not path.is_file():
        raise CorpusError(f"missing archive: {path}")
    expected_size = int(artifact["size"])
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise CorpusError(
            f"{path.name}: expected {_format_bytes(expected_size)}, "
            f"found {_format_bytes(actual_size)}"
        )

    expected_checksum = _parse_checksum(artifact.get("checksum"))
    if expected_checksum is not None:
        algorithm, expected_digest = expected_checksum
        actual_digest = hash_file(path, algorithm)
        if actual_digest.casefold() != expected_digest:
            raise CorpusError(
                f"{path.name}: {algorithm} mismatch; expected {expected_digest}, "
                f"found {actual_digest}"
            )
    return hash_file(path, "sha256")


def _download(url: str, destination: Path, expected_size: int) -> None:
    """Download one artifact atomically with bounded progress output."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    if partial.exists():
        partial.unlink()

    request = Request(url, headers={"User-Agent": USER_AGENT})
    written = 0
    next_progress = PROGRESS_INTERVAL
    try:
        with urlopen(request, timeout=120) as response, partial.open("wb") as output:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                written += len(chunk)
                if written >= next_progress:
                    print(
                        f"  downloaded {_format_bytes(written)} of "
                        f"{_format_bytes(expected_size)}",
                        file=sys.stderr,
                    )
                    next_progress += PROGRESS_INTERVAL
        if written != expected_size:
            raise CorpusError(
                f"{destination.name}: download size is {_format_bytes(written)}; "
                f"expected {_format_bytes(expected_size)}"
            )
        os.replace(partial, destination)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise


def fetch_artifact(
    artifact: dict[str, object], archive_root: Path, *, force: bool = False
) -> tuple[Path, str]:
    """Ensure one archive is present and verified in the local corpus cache."""
    destination = archive_root / str(artifact["filename"])
    if destination.exists() and not force:
        sha256 = verify_artifact(destination, artifact)
        print(f"archive ready: {destination.name}")
        return destination, sha256
    if destination.exists():
        destination.unlink()

    print(f"downloading {artifact['url']}")
    _download(str(artifact["url"]), destination, int(artifact["size"]))
    sha256 = verify_artifact(destination, artifact)
    print(f"verified archive: {destination.name}")
    return destination, sha256


def _member_destination(root: Path, member_name: str) -> Path:
    """Resolve an archive member while rejecting traversal and absolute paths."""
    normalized = member_name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if member.is_absolute() or ".." in member.parts or not member.parts:
        raise CorpusError(f"unsafe archive member: {member_name!r}")
    destination = root.joinpath(*member.parts)
    try:
        destination.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise CorpusError(f"unsafe archive member: {member_name!r}") from error
    return destination


def _copy_stream(source: BinaryIO, destination: Path) -> None:
    """Copy one extracted file while creating its parent directory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=DOWNLOAD_CHUNK_SIZE)


def _extract_zip(archive: Path, destination: Path) -> None:
    """Extract regular files from a ZIP archive without trusting member paths."""
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = _member_destination(destination, member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            with source.open(member) as member_source:
                _copy_stream(member_source, target)


def _extract_tar(archive: Path, destination: Path) -> None:
    """Extract regular files from a tar archive and ignore links and devices."""
    with tarfile.open(archive, mode="r:gz") as source:
        for member in source:
            target = _member_destination(destination, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            member_source = source.extractfile(member)
            if member_source is None:
                raise CorpusError(f"cannot read archive member: {member.name}")
            with member_source:
                _copy_stream(member_source, target)


def _extract_gzip(archive: Path, destination: Path, output_name: object) -> None:
    """Expand a single-file gzip artifact to its manifest-defined name."""
    if not isinstance(output_name, str) or not output_name:
        raise CorpusError(f"{archive.name}: gzip artifacts require an output name")
    target = _member_destination(destination, output_name)
    with gzip.open(archive, "rb") as source:
        _copy_stream(source, target)


def _extracted_file_records(root: Path) -> list[dict[str, object]]:
    """Hash every extracted regular file so local mutation cannot be re-indexed."""
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.name == ".complete.json":
            continue
        if path.is_symlink():
            raise CorpusError(f"unexpected symlink in extracted dataset: {path}")
        if not path.is_file():
            continue
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hash_file(path, "sha256"),
            }
        )
    return records


def _validate_completion_marker(
    root: Path,
    dataset_id: str,
    expected_artifacts: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Require the marker to bind both archives and current extracted bytes."""
    marker = root / ".complete.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(
            f"{dataset_id}: invalid completion marker; rerun fetch with --force"
        ) from error
    if payload.get("dataset") != dataset_id:
        raise CorpusError(
            f"{dataset_id}: completion marker identifies another dataset; "
            "rerun fetch with --force"
        )
    if expected_artifacts is not None and payload.get("artifacts") != expected_artifacts:
        raise CorpusError(
            f"{dataset_id}: extracted data does not match the verified archives; "
            "rerun fetch with --force"
        )
    recorded_files = payload.get("extracted_files")
    if not isinstance(recorded_files, list):
        raise CorpusError(
            f"{dataset_id}: completion marker lacks extracted-file hashes; "
            "rerun fetch with --force"
        )
    if recorded_files != _extracted_file_records(root):
        raise CorpusError(
            f"{dataset_id}: extracted files changed after verification; "
            "rerun fetch with --force"
        )
    return payload


def extract_dataset(
    dataset: dict[str, object],
    archives: list[tuple[Path, str]],
    data_root: Path,
    *,
    force: bool = False,
) -> Path:
    """Safely extract a dataset through a staging directory."""
    dataset_id = str(dataset["id"])
    destination = data_root / dataset_id
    marker = destination / ".complete.json"
    artifact_records = [
        {"filename": path.name, "sha256": sha256}
        for path, sha256 in archives
    ]
    marker_payload: dict[str, object] = {
        "dataset": dataset_id,
        "artifacts": artifact_records,
    }
    if marker.is_file() and not force:
        _validate_completion_marker(destination, dataset_id, artifact_records)
        print(f"dataset ready: {dataset_id}")
        return destination
    if destination.exists() and not force:
        raise CorpusError(
            f"{destination} exists without a completion marker; rerun with --force"
        )

    data_root.mkdir(parents=True, exist_ok=True)
    staging = data_root / f".{dataset_id}.extracting"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        artifacts = dataset["artifacts"]
        assert isinstance(artifacts, list)
        for artifact, (archive_path, _sha256) in zip(artifacts, archives):
            archive_type = artifact["archive"]
            if archive_type == "zip":
                _extract_zip(archive_path, staging)
            elif archive_type == "tar.gz":
                _extract_tar(archive_path, staging)
            elif archive_type == "gzip":
                _extract_gzip(archive_path, staging, artifact.get("output"))
            else:
                raise CorpusError(
                    f"{dataset_id}: unsupported archive type {archive_type!r}"
                )

        marker_payload["extracted_files"] = _extracted_file_records(staging)
        (staging / ".complete.json").write_text(
            json.dumps(marker_payload, indent=2) + "\n", encoding="utf-8"
        )
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    print(f"extracted dataset: {dataset_id}")
    return destination


def _matching_files(root: Path, patterns: Iterable[str]) -> list[Path]:
    """Return unique regular files selected by manifest glob patterns."""
    matches = {
        path.resolve()
        for pattern in patterns
        for path in root.glob(pattern)
        if path.is_file()
    }
    return sorted(matches)


def count_lines(path: Path) -> int:
    """Count records in a large line-oriented file without decoding it."""
    lines = 0
    last = b""
    with path.open("rb") as source:
        while True:
            chunk = source.read(8 * DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last and last != b"\n":
        lines += 1
    return lines


def _file_record(path: Path, data_root: Path) -> dict[str, object]:
    """Measure one local corpus file for the generated index."""
    return {
        "path": path.relative_to(data_root.resolve()).as_posix(),
        "lines": count_lines(path),
        "bytes": path.stat().st_size,
        "sha256": hash_file(path, "sha256"),
    }


def inspect_dataset(
    dataset: dict[str, object], data_root: Path
) -> dict[str, object]:
    """Measure extracted logs and labels and enforce the dataset's scale gate."""
    dataset_id = str(dataset["id"])
    root = data_root / dataset_id
    if not (root / ".complete.json").is_file():
        raise CorpusError(f"dataset is not extracted: {dataset_id}")
    _validate_completion_marker(root, dataset_id)

    logs = _matching_files(root, dataset["log_globs"])
    if not logs:
        raise CorpusError(f"{dataset_id}: no logs matched {dataset['log_globs']}")
    labels = _matching_files(root, dataset.get("label_globs", []))
    log_records = [_file_record(path, data_root) for path in logs]
    total_lines = sum(int(record["lines"]) for record in log_records)
    minimum_lines = int(dataset["minimum_line_count"])
    if total_lines < minimum_lines:
        raise CorpusError(
            f"{dataset_id}: found {total_lines:,} lines; expected at least "
            f"{minimum_lines:,}"
        )
    expected_lines = int(dataset["expected_line_count"])
    if total_lines != expected_lines:
        raise CorpusError(
            f"{dataset_id}: found {total_lines:,} lines; pinned archive should contain "
            f"{expected_lines:,}"
        )

    ground_truth = dataset.get("ground_truth")
    if isinstance(ground_truth, dict) and "path" in ground_truth:
        truth_path = _member_destination(root, str(ground_truth["path"]))
        if not truth_path.is_file():
            raise CorpusError(
                f"{dataset_id}: missing ground-truth file {ground_truth['path']}"
            )

    return {
        "id": dataset_id,
        "title": dataset["title"],
        "family": dataset["family"],
        "official_line_count": dataset["official_line_count"],
        "expected_line_count": expected_lines,
        "lines": total_lines,
        "bytes": sum(int(record["bytes"]) for record in log_records),
        "log_files": log_records,
        "label_files": [
            _file_record(path, data_root)
            for path in labels
        ],
        "ground_truth": ground_truth,
    }


def build_index(
    manifest: dict[str, object],
    corpus_root: Path,
    datasets: Iterable[dict[str, object]],
) -> dict[str, object]:
    """Inspect selected datasets and merge them into the local benchmark index."""
    data_root = corpus_root / "data"
    index_path = corpus_root / INDEX_NAME
    manifest_sha256 = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    records_by_id: dict[str, dict[str, object]] = {}
    try:
        existing = json.loads(index_path.read_text(encoding="utf-8"))
        if existing.get("manifest_sha256") == manifest_sha256:
            for record in existing.get("datasets", []):
                dataset_id = str(record["id"])
                if (data_root / dataset_id / ".complete.json").is_file():
                    records_by_id[dataset_id] = record
    except (OSError, json.JSONDecodeError, AttributeError, KeyError, TypeError):
        pass

    for dataset in datasets:
        print(f"counting records: {dataset['id']}", file=sys.stderr)
        records_by_id[str(dataset["id"])] = inspect_dataset(dataset, data_root)
    records = [
        records_by_id[str(dataset["id"])]
        for dataset in manifest["datasets"]
        if str(dataset["id"]) in records_by_id
    ]
    index = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": manifest_sha256,
        "datasets": records,
        "totals": {
            "datasets": len(records),
            "log_files": sum(len(record["log_files"]) for record in records),
            "lines": sum(int(record["lines"]) for record in records),
            "bytes": sum(int(record["bytes"]) for record in records),
        },
    }
    corpus_root.mkdir(parents=True, exist_ok=True)
    partial = index_path.with_name(index_path.name + ".part")
    partial.write_text(
        json.dumps(index, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(partial, index_path)
    return index


def fetch_dataset(
    dataset: dict[str, object], corpus_root: Path, *, force: bool = False
) -> None:
    """Download, verify, and safely extract one manifest dataset."""
    artifacts = dataset["artifacts"]
    assert isinstance(artifacts, list)
    archives = [
        fetch_artifact(artifact, corpus_root / "archives", force=force)
        for artifact in artifacts
    ]
    extract_dataset(dataset, archives, corpus_root / "data", force=force)


def _print_index(index: dict[str, object]) -> None:
    """Print a compact, human-readable corpus inventory."""
    for dataset in index["datasets"]:
        print(
            f"{dataset['id']:<20} {int(dataset['lines']):>12,} lines  "
            f"{_format_bytes(int(dataset['bytes'])):>10}  "
            f"{len(dataset['log_files']):>4} file(s)"
        )
    totals = index["totals"]
    print(
        f"{'TOTAL':<20} {int(totals['lines']):>12,} lines  "
        f"{_format_bytes(int(totals['bytes'])):>10}  "
        f"{int(totals['log_files']):>4} file(s)"
    )


def _corpus_root(value: str | None) -> Path:
    """Resolve the CLI, environment, or repository-local corpus location."""
    selected = value or os.environ.get("QURTAIL_CORPUS_ROOT")
    return Path(selected).expanduser().resolve() if selected else DEFAULT_CORPUS_ROOT


def _list_manifest(datasets: Iterable[dict[str, object]]) -> None:
    """Print the committed inventory without requiring local downloads."""
    total = 0
    for dataset in datasets:
        lines = int(dataset["official_line_count"])
        total += lines
        print(
            f"{dataset['id']:<20} {lines:>12,} official lines  "
            f"{dataset['family']}"
        )
    print(f"{'TOTAL':<20} {total:>12,} official lines")


def main(argv: list[str] | None = None) -> int:
    """Run the corpus inventory, fetch, or verification command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        help="local corpus root (default: benchmarks/corpus/local or QURTAIL_CORPUS_ROOT)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list manifest datasets")
    list_parser.add_argument("datasets", nargs="*")

    fetch_parser = subparsers.add_parser(
        "fetch", help="download, verify, extract, and index datasets"
    )
    fetch_parser.add_argument("datasets", nargs="*")
    fetch_parser.add_argument(
        "--force", action="store_true", help="replace selected local archives and data"
    )

    verify_parser = subparsers.add_parser(
        "verify", help="recount extracted logs and rebuild the local index"
    )
    verify_parser.add_argument("datasets", nargs="*")

    args = parser.parse_args(argv)
    try:
        manifest = load_manifest()
        datasets = selected_datasets(manifest, args.datasets)
        if args.command == "list":
            _list_manifest(datasets)
            return 0

        corpus_root = _corpus_root(args.root)
        if args.command == "fetch":
            for dataset in datasets:
                fetch_dataset(dataset, corpus_root, force=args.force)
        index = build_index(manifest, corpus_root, datasets)
        _print_index(index)
        return 0
    except (CorpusError, OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f"large corpus error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
