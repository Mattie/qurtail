"""Compare pinned external logs with the pre-feature and current reducers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import lru_cache, partial
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qurtail import _StreamReducer
from benchmarks.run_interleaved import TranscriptAudit

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LEADING_ISO = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s*")


def normalize_evidence(text: str) -> str:
    """Ignore terminal color, leading event timestamps, and layout in signal lookup."""
    return " ".join(LEADING_ISO.sub("", line).strip()
                    for line in ANSI.sub("", text).splitlines()).strip()


def physical_records(data: bytes) -> list[str]:
    """Split only at LF, matching qurtail framing while preserving lone CR/control bytes."""
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.rstrip("\r") for line in lines]


def load_baseline(path: Path) -> type:
    """Load the retained local pre-feature source, preserving its module identity."""
    spec = importlib.util.spec_from_file_location("_qurtail_external_baseline", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load baseline")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._StreamReducer


def verify_files(root: Path, manifest: dict) -> None:
    """Require every evaluation input to match its recorded SHA-256."""
    for entry in manifest["files"]:
        if hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"changed input: {entry['path']}")


def render_rca_rows(rows: list[dict]) -> tuple[list[str], dict]:
    """Render every table row in stored order without dropping message metadata.

    The three source fields become UTC ISO timestamp, JSON-quoted container name,
    and the original message. Embedded message timestamps and newlines remain.
    This is a documented text-view adaptation, not an original collector stream.
    """
    records = []
    previous_timestamp = None
    backwards = 0
    embedded_newlines = 0
    null_messages = 0
    for row in rows:
        timestamp, container, message = (row[name] for name in ("timestamp", "container_name", "message"))
        if not isinstance(timestamp, int) or not isinstance(container, str) or not (message is None or isinstance(message, str)):
            raise ValueError("unexpected RCAEval field type")
        backwards += previous_timestamp is not None and timestamp < previous_timestamp
        previous_timestamp = timestamp
        stamp = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
        if message is None:
            # Missing text cannot establish a repeated message. Preserve its row
            # and metadata as an explicit uncompressible reference-history fence.
            records.append(f"[RCAEval null message] {stamp} container={json.dumps(container, ensure_ascii=False)}")
            null_messages += 1
            continue
        text = f"{stamp} container={json.dumps(container, ensure_ascii=False)} {message}\n"
        # Match the physical-line boundary consumed by the file/stdio reader.
        lines = text.split("\n")
        records.extend(line.rstrip("\r") for line in lines[:-1])
        embedded_newlines += message.count("\n")
    return records, {"source_rows": len(rows), "physical_records": len(records),
                     "backwards_timestamp_steps": backwards, "embedded_newlines": embedded_newlines,
                     "null_messages": null_messages,
                     "rendering": "UTC ISO timestamp + JSON-quoted container + unchanged message; stored row order; null messages are explicit uncompressible fences"}


def canonical_rca(path: Path) -> tuple[list[str], dict]:
    """Read the pinned three-column Parquet schema and produce its documented text view."""
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    if table.column_names != ["timestamp", "container_name", "message"]:
        raise ValueError(f"unexpected RCAEval schema: {table.column_names}")
    return render_rca_rows(table.to_pylist())


class EvidenceAudit(TranscriptAudit):
    """Track individually visible source records separately from dot/count summaries."""

    def __init__(self, records: list[str], output: Path, count_tokens, *, interleaving: bool = True) -> None:
        super().__init__(records, count_tokens, interleaving=interleaving)
        self.output = output.open("w", encoding="utf-8", newline="\n")
        self.visible = bytearray(len(records))
        self.resolved = []

    def write(self, text: str) -> int:
        """Retain exactly what the reducer writes while independently auditing it."""
        self.output.write(text)
        return super().write(text)

    def _line(self, rendered: str) -> None:
        """Credit exact evidence only when the source record is printed in full."""
        before = self.records
        old_full = self.full_records
        super()._line(rendered)
        if self.full_records != old_full:
            self.visible[before] = 1
            self.resolved.append(self._previous)

    def result(self) -> dict:
        """Finish accounting, keeping adjacent summaries out of exact-evidence claims."""
        return super().finish()


def signal_checks(labels: dict, raw_surface: str, resolved_surface: str,
                  visible: bytearray, record_count: int) -> dict:
    """Report lexical signal retention and exact visibility of valid annotated spans.

    Missing raw signals are annotation/alignment gaps, never credited as retained.
    Alias matches are reported separately from the full primary value.
    """
    signals = []
    for signal in labels.get("required_signals", []):
        primary = normalize_evidence(signal["value"])
        aliases = [normalize_evidence(value) for value in signal.get("aliases", [])]
        signals.append({
            "type": signal["type"], "importance": signal.get("importance"),
            "primary_present_raw": bool(primary) and primary in raw_surface,
            "primary_present_resolved": bool(primary) and primary in resolved_surface,
            "alias_present_raw": any(value and value in raw_surface for value in aliases),
            "alias_present_resolved": any(value and value in resolved_surface for value in aliases),
        })
    span_lines = set()
    invalid = []
    for span in labels.get("evidence_spans", []):
        start, end = span["start_line"], span["end_line"]
        if not 1 <= start <= end <= record_count:
            invalid.append([start, end])
        else:
            span_lines.update(range(start - 1, end))
    return {
        "signals": signals,
        "critical_primary_present_raw": sum(s["importance"] == "critical" and s["primary_present_raw"] for s in signals),
        "critical_primary_lost": sum(s["importance"] == "critical" and s["primary_present_raw"]
                                     and not s["primary_present_resolved"] for s in signals),
        "primary_absent_from_raw": sum(not s["primary_present_raw"] for s in signals),
        "valid_annotated_span_records": len(span_lines),
        "span_records_individually_visible": sum(visible[i] for i in span_lines),
        "invalid_annotated_spans": invalid,
    }


def evaluate_case(records: list[str], output: Path, reducer_type: type,
                  count_tokens, labels: dict | None = None, *, interleaving: bool = True) -> dict:
    """Audit one full replay; retain failures instead of discarding difficult cases."""
    started = time.perf_counter()
    audit = EvidenceAudit(records, output, count_tokens, interleaving=interleaving)
    try:
        reducer = reducer_type(audit, dot_every=10, clock=lambda: 0.0)
        for line in records:
            reducer.process(line)
        reducer.finish()
        report = audit.result()
        report["audit_passed"] = True
        if labels is not None:
            report["evidence"] = signal_checks(
                labels, normalize_evidence("\n".join(records)),
                normalize_evidence("\n".join(audit.resolved)), audit.visible, len(records))
    except ValueError as error:
        report = {"audit_passed": False, "error": str(error), "source_position": audit.records}
    finally:
        audit.output.close()
    report["elapsed_seconds"] = time.perf_counter() - started
    report["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    return report


def main() -> int:
    """Replay the entire prespecified selection and retain source-bound results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import tiktoken
    import pyarrow

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    verify_files(args.root, manifest)
    encoding = tiktoken.get_encoding("o200k_base")
    reducers = {"baseline": load_baseline(args.baseline_source), "current": _StreamReducer,
                "adjacent_only": partial(_StreamReducer, interleaving=False)}
    results = []
    for entry in manifest["files"]:
        source = args.root / entry["path"]
        if source.suffix not in (".log", ".parquet"):
            continue
        case_id = (source.parent.name if entry["family"] != "rootly" else source.stem)
        folder = args.root / "rendered" / entry["family"] / case_id
        folder.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".parquet":
            records, adaptation = canonical_rca(source)
        else:
            records = physical_records(source.read_bytes())
            adaptation = {"rendering": "stored physical-line order; UTF-8 replacement decoding; LF"}
        if records:
            records[0] = records[0].removeprefix("\ufeff")
        canonical = "\n".join(records) + ("\n" if records else "")
        (folder / "raw.log").write_bytes(canonical.encode("utf-8"))
        labels_path = source.parent / "ground_truth.json"
        labels = json.loads(labels_path.read_text()) if labels_path.exists() else None

        @lru_cache(maxsize=8192)
        def count_tokens(text: str) -> int:
            return len(encoding.encode(text, disallowed_special=()))

        views = {}
        for name, reducer in reducers.items():
            views[name] = evaluate_case(records, folder / f"{name}.log", reducer, count_tokens, labels,
                                        interleaving=name == "current")
        result = {"family": entry["family"], "case": case_id, "input_path": entry["path"],
                  "source_sha256": entry["sha256"], "canonical_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                  "adaptation": adaptation, "views": views}
        results.append(result)
        current = views["current"]
        print(f"{entry['family']}/{case_id}: " + (
            f"{current['records']:,} records; {current['byte_reduction']:.2%} bytes; "
            f"{current['token_reduction']:.2%} tokens; {current['suppressed_records']:,} suppressed"
            if current["audit_passed"] else f"AUDIT FAILED: {current['error']}"), flush=True)
    audits_passed = all(view["audit_passed"] for case in results for view in case["views"].values())
    opt_out_matches_baseline = all(
        case["views"]["adjacent_only"]["output_sha256"] == case["views"]["baseline"]["output_sha256"]
        for case in results)
    passed = audits_passed and opt_out_matches_baseline
    report = {
        "schema_version": 1,
        "qurtail_sha256": hashlib.sha256((ROOT / "qurtail.py").read_bytes()).hexdigest(),
        "baseline_sha256": hashlib.sha256(args.baseline_source.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "evaluator_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                             for path in ("benchmarks/validate_external_logs.py", "benchmarks/run_interleaved.py")},
        "tokenizer": {"package": f"tiktoken {tiktoken.__version__}", "encoding": "o200k_base",
                      "scope": "sum of LF-terminated logical records; excludes agent/prompt/tool costs"},
        "pyarrow_version": pyarrow.__version__,
        "configuration": {"dot_every": 10, "clock": "constant zero, no live delays"},
        "results": results, "all_protocol_audits_passed": audits_passed,
        "opt_out_matches_baseline": opt_out_matches_baseline,
        "passed": passed,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
