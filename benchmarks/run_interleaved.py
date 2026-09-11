"""Audit dot/count output against complete, verified, naturally ordered logs.

Token counts sum complete logical records, including newlines, with o200k_base.
This makes framing explicit without materializing multi-million-token transcripts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qurtail import _StreamReducer, _signature  # noqa: E402
from benchmarks.large_corpus import load_manifest  # noqa: E402
from benchmarks.run_large_corpus import load_index, verify_index_record  # noqa: E402


SUMMARY = re.compile(r"^\.* ?\[(\d+) similar (?:in \d+s|before stop)\]$")
DATASETS = ("loghub-zookeeper", "loghub-linux", "loghub-openssh")


class TranscriptAudit:
    """Consume actual output and account for each original source occurrence.

    Counts may cover only known, fully printed patterns. In adjacent-only mode,
    each counted record must also match the immediately preceding source pattern.
    This checks occurrence accounting, not recovery of omitted field values.
    """

    def __init__(self, source: Iterable[str], count_tokens: Callable[[str], int] | None = None,
                 *, interleaving: bool = True) -> None:
        self._source = iter(source)
        self._peeked: str | None = None
        self._buffer = ""
        self._previous: str | None = None
        self._known: set[object] = set()
        self._interleaving = interleaving
        self._count_tokens = count_tokens or (lambda text: 0)
        self.records = 0
        self.full_records = 0
        self.suppressed_records = 0
        self.raw_bytes = 0
        self.output_bytes = 0
        self.raw_tokens = 0
        self.output_tokens = 0

    def _peek(self) -> str | None:
        """Look ahead one source record without advancing its position."""
        if self._peeked is None:
            line = next(self._source, None)
            if line is not None:
                self._peeked = line.rstrip("\r\n")
                if self.records == 0:
                    self._peeked = self._peeked.removeprefix("\ufeff")
        return self._peeked

    def _take(self) -> str:
        """Consume one original occurrence and charge its decoded text cost."""
        line = self._peek()
        if line is None:
            raise ValueError("output represents more records than the source")
        self._peeked = None
        self.records += 1
        self.raw_bytes += len((line + "\n").encode("utf-8"))
        self.raw_tokens += self._count_tokens(line + "\n")
        return line

    def _line(self, rendered: str) -> None:
        """Check one completed output line, giving literal source text priority."""
        self.output_tokens += self._count_tokens(rendered + "\n")
        if rendered == self._peek():
            self._previous = self._take()
            self.full_records += 1
            signature = _signature(rendered)
            if signature is not None:
                self._known.add(signature)
            return

        summary = SUMMARY.fullmatch(rendered)
        if summary:
            count = int(summary[1])
            if not count or self._previous is None:
                raise ValueError("adjacent count has no exemplar or occurrences")
            for _ in range(count):
                source = self._take()
                signature = _signature(source)
                if signature is None or signature not in self._known:
                    raise ValueError("count represents an unknown pattern")
                if not self._interleaving and signature != _signature(self._previous):
                    raise ValueError("adjacent count represents a changed pattern")
                self._previous = source
            self.suppressed_records += count
            return

        raise ValueError(f"unaccounted output at source record {self.records + 1}")

    def write(self, text: str) -> int:
        """Accept arbitrary producer writes, including incomplete dot batches."""
        self.output_bytes += len(text.encode("utf-8"))
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._line(line)
        return len(text)

    def flush(self) -> None:
        """Support the reducer's streaming writer interface."""

    def finish(self) -> dict[str, int | float | bool]:
        """Require a complete transcript and return measured occurrence costs."""
        if self._buffer or self._peek() is not None:
            raise ValueError("output ended before all source records were represented")
        if self.full_records + self.suppressed_records != self.records:
            raise ValueError("source occurrence accounting disagrees")
        return {
            "records": self.records,
            "full_records": self.full_records,
            "suppressed_records": self.suppressed_records,
            "decoded_input_bytes": self.raw_bytes,
            "output_bytes": self.output_bytes,
            "byte_reduction": 1 - self.output_bytes / self.raw_bytes if self.raw_bytes else 0.0,
            "input_tokens": self.raw_tokens,
            "output_tokens": self.output_tokens,
            "token_reduction": 1 - self.output_tokens / self.raw_tokens if self.raw_tokens else 0.0,
            "all_occurrences_accounted_for": True,
        }


def replay(path: Path, reducer_type: type, count_tokens: Callable[[str], int]) -> dict[str, object]:
    """Replay one natural file with an independent source cursor auditing output."""
    started = time.perf_counter()
    with path.open("rb") as source, path.open("rb") as originals:
        audit = TranscriptAudit((line.decode("utf-8", errors="replace") for line in originals), count_tokens)
        reducer = reducer_type(audit, dot_every=10, clock=lambda: 0.0)
        for line in source:
            reducer.process(line.decode("utf-8", errors="replace"))
        reducer.finish()
        result = audit.finish()
    return {**result, "elapsed_seconds": time.perf_counter() - started}


def main(argv: list[str] | None = None) -> int:
    """Measure current output and an optional locally retained pre-change source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "benchmarks/corpus/local")
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    import tiktoken

    encoding = tiktoken.get_encoding("o200k_base")
    count_tokens = lambda text: len(encoding.encode(text, disallowed_special=()))
    reducers = {"current": _StreamReducer}
    baseline_hash = None
    if args.baseline_source:
        spec = importlib.util.spec_from_file_location("_qurtail_baseline", args.baseline_source)
        if spec is None or spec.loader is None:
            parser.error("cannot load baseline source")
        baseline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = baseline
        spec.loader.exec_module(baseline)
        reducers["baseline"] = baseline._StreamReducer
        baseline_hash = hashlib.sha256(args.baseline_source.read_bytes()).hexdigest()

    index = load_index(args.root)
    manifest = load_manifest()
    results = []
    for dataset_id in DATASETS:
        record = next(item for item in index["datasets"] if item["id"] == dataset_id)
        verify_index_record(args.root, record)
        files = []
        for file in record["log_files"]:
            path = args.root / "data" / file["path"]
            measurements = {name: replay(path, reducer, count_tokens) for name, reducer in reducers.items()}
            files.append({"path": file["path"], "sha256": file["sha256"], "views": measurements})
            print(f"{dataset_id}: {measurements['current']['byte_reduction']:.1%} bytes, "
                  f"{measurements['current']['token_reduction']:.1%} tokens saved; occurrences verified", flush=True)
        results.append({"dataset": dataset_id, "source_revision": next(d["source_revision"] for d in manifest["datasets"] if d["id"] == dataset_id), "files": files})

    zookeeper = results[0]["files"][0]["views"]["current"]
    gates = {"zookeeper_decoded_byte_reduction_at_least_25_percent": zookeeper["byte_reduction"] >= 0.25,
             "all_occurrences_accounted_for": all(f["views"]["current"]["all_occurrences_accounted_for"] for r in results for f in r["files"])}
    report = {
        "schema_version": 1,
        "qurtail_sha256": hashlib.sha256((ROOT / "qurtail.py").read_bytes()).hexdigest(),
        "evaluator_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in ("benchmarks/run_interleaved.py", "benchmarks/run_large_corpus.py", "benchmarks/large_corpus.py")},
        "manifest_sha256": index["manifest_sha256"],
        "baseline_sha256": baseline_hash,
        "tokenizer": {"package": f"tiktoken {tiktoken.__version__}", "encoding": "o200k_base", "scope": "sum of complete logical records including LF; no platform or prompt framing"},
        "configuration": {"dot_every": 10, "clock": "constant zero; finite offline replay"},
        "results": results,
        "gates": gates,
        "passed": all(gates.values()),
    }
    serialized = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
