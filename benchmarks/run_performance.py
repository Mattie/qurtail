"""Measure qurtail's synthetic throughput, CPU, memory, and first-output gates."""

from __future__ import annotations

import argparse
from io import StringIO
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qurtail import DEFAULT_DOT_EVERY, _StreamReducer


MIN_RECORDS_PER_SECOND = 50_000
MAX_ADDITIONAL_MEMORY = 64 * 1024 * 1024
MAX_FIRST_RECORD_SECONDS = 0.100
DEFAULT_TIMING_RECORDS = 500_000
DEFAULT_TIMING_TRIALS = 5
MAX_MEMORY_RECORDS = 100_000


class CountingWriter:
    """Count output without retaining it and record the first write time."""

    def __init__(self) -> None:
        self.characters = 0
        self.first_write: float | None = None

    def write(self, text: str) -> int:
        if self.first_write is None:
            self.first_write = time.perf_counter()
        self.characters += len(text)
        return len(text)

    def flush(self) -> None:
        """Accept the streaming writer contract."""


def _record(index: int, width: int) -> str:
    """Build one fixed-width record with only documented volatile changes."""
    prefix = (
        f"2026-08-08T12:{(index // 60) % 60:02d}:{index % 60:02d}Z "
        f"INFO refreshed cache request_id={index:032x} "
    )
    return (prefix + "x" * max(0, width - len(prefix)))[:width] + "\n"


def _unique_record(index: int, width: int) -> str:
    """Build a record whose unfamiliar number must create a visible pattern."""
    prefix = f"unique sequence={index} "
    return (prefix + "x" * max(0, width - len(prefix)))[:width] + "\n"


def _framing_scan(source_text: str) -> tuple[float, float]:
    """Measure wall time and CPU time for framing and raw forwarding."""
    writer = CountingWriter()
    source = StringIO(source_text)
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    for record in source:
        printable = record.rstrip("\r\n")
        writer.write(printable + "\n")
        writer.flush()
    return (
        time.perf_counter() - wall_started,
        time.process_time() - cpu_started,
    )


def _qurtail_scan(
    source_text: str,
    *,
    measure_memory: bool,
) -> tuple[float, float, int, float]:
    """Measure wall time, CPU time, memory, and latency for the reducer."""
    writer = CountingWriter()
    reducer = _StreamReducer(writer, dot_every=DEFAULT_DOT_EVERY)
    source = StringIO(source_text)
    if measure_memory:
        tracemalloc.start()
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    for record in source:
        reducer.process(record)
    reducer.finish()
    cpu_elapsed = time.process_time() - cpu_started
    wall_elapsed = time.perf_counter() - wall_started
    first_latency = (
        wall_elapsed
        if writer.first_write is None
        else writer.first_write - wall_started
    )
    peak = 0
    if measure_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return wall_elapsed, cpu_elapsed, peak, first_latency


def _memory_scan(record_count: int, width: int) -> int:
    """Measure bounded state while every unfamiliar record stays visible."""
    writer = CountingWriter()
    reducer = _StreamReducer(writer, dot_every=10)
    tracemalloc.start()
    for index in range(record_count):
        reducer.process(_unique_record(index, width))
    reducer.finish()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def measure(
    record_count: int = DEFAULT_TIMING_RECORDS,
    width: int = 256,
    trials: int = DEFAULT_TIMING_TRIALS,
) -> dict[str, object]:
    """Return median release-gate timings and one bounded-memory replay."""
    if trials < 1:
        raise ValueError("trials must be positive")
    source_text = "".join(_record(index, width) for index in range(record_count))
    timing_samples = []
    for trial in range(trials):
        if trial % 2 == 0:
            framing = _framing_scan(source_text)
            qurtail = _qurtail_scan(source_text, measure_memory=False)
        else:
            qurtail = _qurtail_scan(source_text, measure_memory=False)
            framing = _framing_scan(source_text)
        framing_wall, framing_cpu = framing
        qurtail_wall, qurtail_cpu, _, first_latency = qurtail
        timing_samples.append(
            {
                "framing_seconds": framing_wall,
                "framing_cpu_seconds": framing_cpu,
                "qurtail_seconds": qurtail_wall,
                "qurtail_cpu_seconds": qurtail_cpu,
                "framing_cpu_ratio": qurtail_cpu / max(framing_cpu, 1e-12),
                "first_record_seconds": first_latency,
            }
        )
    framing_seconds = statistics.median(
        sample["framing_seconds"] for sample in timing_samples
    )
    framing_cpu_seconds = statistics.median(
        sample["framing_cpu_seconds"] for sample in timing_samples
    )
    qurtail_seconds = statistics.median(
        sample["qurtail_seconds"] for sample in timing_samples
    )
    qurtail_cpu_seconds = statistics.median(
        sample["qurtail_cpu_seconds"] for sample in timing_samples
    )
    first_latency = max(
        sample["first_record_seconds"] for sample in timing_samples
    )
    memory_record_count = min(record_count, MAX_MEMORY_RECORDS)
    peak_memory = _memory_scan(memory_record_count, width)
    throughput = record_count / max(qurtail_seconds, 1e-12)
    total_framing_cpu = sum(
        sample["framing_cpu_seconds"] for sample in timing_samples
    )
    total_qurtail_cpu = sum(
        sample["qurtail_cpu_seconds"] for sample in timing_samples
    )
    cpu_ratio = total_qurtail_cpu / max(total_framing_cpu, 1e-12)
    gates = {
        "at_least_50000_records_per_second": (
            throughput >= MIN_RECORDS_PER_SECOND
        ),
        "peak_additional_memory_below_64_mib": (
            peak_memory < MAX_ADDITIONAL_MEMORY
        ),
        "first_record_below_100_ms": (
            first_latency < MAX_FIRST_RECORD_SECONDS
        ),
    }
    return {
        "schema_version": 1,
        "record_count": record_count,
        "memory_record_count": memory_record_count,
        "record_width": width,
        "dot_every": DEFAULT_DOT_EVERY,
        "timing_trials": trials,
        "timing_samples": timing_samples,
        "framing_seconds": framing_seconds,
        "framing_cpu_seconds": framing_cpu_seconds,
        "qurtail_seconds": qurtail_seconds,
        "qurtail_cpu_seconds": qurtail_cpu_seconds,
        "total_framing_cpu_seconds": total_framing_cpu,
        "total_qurtail_cpu_seconds": total_qurtail_cpu,
        "records_per_second": throughput,
        "framing_cpu_ratio": cpu_ratio,
        "peak_additional_bytes": peak_memory,
        "first_record_seconds": first_latency,
        "gates": gates,
        "passed": all(gates.values()),
    }


def main(argv: list[str] | None = None) -> int:
    """Run the synthetic performance gate and print or retain JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--records", type=int, default=DEFAULT_TIMING_RECORDS
    )
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument(
        "--trials", type=int, default=DEFAULT_TIMING_TRIALS
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.records < 1:
        parser.error("--records must be positive")
    if args.width < 1:
        parser.error("--width must be positive")
    if args.trials < 1:
        parser.error("--trials must be positive")
    result = measure(args.records, args.width, args.trials)
    serialized = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(serialized, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
