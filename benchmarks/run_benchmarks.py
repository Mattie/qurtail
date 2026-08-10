"""Evaluate qurtail on held-out, naturally ordered monitoring episodes."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path
import statistics
import sys
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qurtail import _StreamReducer  # noqa: E402


SKILL_PATH = ROOT / "skills" / "qurtail-fluency" / "SKILL.md"
ENCODING_NAME = "o200k_base"
DEPENDENCY_HELP = (
    "Install benchmark dependencies with "
    "'python -m pip install -r benchmarks/requirements.txt'."
)


@dataclass(frozen=True)
class Episode:
    """Describe one monitoring decision and the evidence it must retain."""

    slug: str
    category: str
    task: str
    expected_decision: str
    lines: tuple[str, ...]
    required_blocks: tuple[tuple[str, ...], ...]
    repetitive: bool


def _timestamp(second: int) -> str:
    """Build a deterministic ISO timestamp for a generated source record."""
    minute, second = divmod(second, 60)
    hour, minute = divmod(minute, 60)
    return f"2026-08-08T{12 + hour:02d}:{minute:02d}:{second:02d}Z"


def _cache_episode() -> Episode:
    """Build repetitive request traffic around one operational failure."""
    ordinary = tuple(
        f"{_timestamp(index)} INFO refreshed cache request_id={index:08x}"
        for index in range(600)
    )
    failure = (
        f"{_timestamp(600)} ERROR cache refresh failed: connection refused"
    )
    lines = ordinary + (failure,)
    return Episode(
        slug="cache-refresh-failure",
        category="error",
        task="Monitor cache refreshes and explain why the refresh stopped.",
        expected_decision="Investigate the refused cache connection.",
        lines=lines,
        required_blocks=((failure,),),
        repetitive=True,
    )


def _http_episode() -> Episode:
    """Build repetitive access traffic around an HTTP status transition."""
    ordinary = tuple(
        f'127.0.0.1 [08/Aug/2026:09:{30 + index // 60:02d}:{index % 60:02d} -0500] '
        '"GET /health HTTP/1.1" 200 17'
        for index in range(600)
    )
    failure = (
        '127.0.0.1 [08/Aug/2026:09:40:00 -0500] '
        '"GET /health HTTP/1.1" 503 17'
    )
    lines = ordinary + (failure,)
    return Episode(
        slug="health-status-transition",
        category="status",
        task="Monitor the health endpoint and decide whether it stayed available.",
        expected_decision="Treat the 503 transition as an availability incident.",
        lines=lines,
        required_blocks=((failure,),),
        repetitive=True,
    )


def _json_episode() -> Episode:
    """Build JSON metadata churn around a changed structured error payload."""
    ordinary = tuple(
        json.dumps(
            {
                "time": index,
                "pid": 4100 + index % 3,
                "hostname": f"worker-{index % 2}",
                "level": "info",
                "requestId": f"req-{index:08x}",
                "message": "background sync complete",
            },
            separators=(",", ":"),
        )
        for index in range(600)
    )
    failure = json.dumps(
        {
            "time": 600,
            "pid": 4100,
            "hostname": "worker-0",
            "level": "error",
            "requestId": "req-failure",
            "error": {"code": "EACCES", "message": "permission denied"},
            "message": "background sync failed",
        },
        separators=(",", ":"),
    )
    lines = ordinary + (failure,)
    return Episode(
        slug="structured-permission-failure",
        category="structured-error",
        task="Monitor background synchronization and identify its failure detail.",
        expected_decision="Fix the EACCES permission failure.",
        lines=lines,
        required_blocks=((failure,),),
        repetitive=True,
    )


def _traceback_episode() -> Episode:
    """Build routine worker traffic around one complete traceback block."""
    ordinary = tuple(
        f"{_timestamp(index)} INFO worker heartbeat trace_id={index:032x}"
        for index in range(600)
    )
    block = (
        "Traceback (most recent call last):",
        '  File "worker.py", line 41, in refresh',
        "    cache.write(payload)",
        "OSError: disk full",
    )
    lines = ordinary + block
    return Episode(
        slug="worker-traceback",
        category="multiline",
        task=(
            "Monitor the worker crash, identify its cause, and decide what to do "
            "before restarting it."
        ),
        expected_decision="Free disk space before restarting the worker.",
        lines=lines,
        required_blocks=(block,),
        repetitive=True,
    )


def _resumed_pattern_episode() -> Episode:
    """Build an incident followed by a resumed earlier pattern."""
    before = tuple(
        f"{_timestamp(index)} INFO worker heartbeat request_id={index:08x}"
        for index in range(300)
    )
    failure = (
        f"{_timestamp(300)} ERROR heartbeat transport: connection refused"
    )
    after = tuple(
        f"{_timestamp(index)} INFO worker heartbeat request_id={index:08x}"
        for index in range(301, 401)
    )
    return Episode(
        slug="resumed-pattern-after-incident",
        category="resumed-pattern",
        task=(
            "Monitor worker heartbeats, identify the incident, and decide whether "
            "successful heartbeats resumed."
        ),
        expected_decision=(
            "Investigate the refused connection and note that heartbeats resumed."
        ),
        lines=before + (failure,) + after,
        required_blocks=((failure,), (after[0],)),
        repetitive=True,
    )


def _numeric_episode() -> Episode:
    """Build a nonrepetitive numeric state transition that must fail open."""
    lines = (
        "replication lag is 1 second",
        "replication lag is 12 seconds",
        "replication lag is 900 seconds",
    )
    return Episode(
        slug="replication-lag-change",
        category="numeric-change",
        task="Monitor replication lag and decide whether it is safe.",
        expected_decision="Escalate the 900-second replication lag.",
        lines=lines,
        required_blocks=((lines[-1],),),
        repetitive=False,
    )


def _malformed_episode() -> Episode:
    """Build malformed and ambiguous records that must remain visible."""
    lines = (
        "{malformed request 1",
        "{malformed request 1",
        "  ambiguous continuation",
        "unknown shape value=17",
        "unknown shape value=18",
    )
    return Episode(
        slug="malformed-records",
        category="fail-open",
        task="Monitor an unfamiliar source without losing uncertain evidence.",
        expected_decision="Inspect the raw source before adding a recognizer.",
        lines=lines,
        required_blocks=tuple((line,) for line in lines),
        repetitive=False,
    )


EPISODES = (
    _cache_episode(),
    _http_episode(),
    _json_episode(),
    _traceback_episode(),
    _resumed_pattern_episode(),
    _numeric_episode(),
    _malformed_episode(),
)


class _LogicalClock:
    """Provide deterministic replay time without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _load_tiktoken():
    """Load the optional tokenizer used for release benchmark measurements."""
    try:
        import tiktoken
    except ImportError as error:
        raise SystemExit(DEPENDENCY_HELP) from error
    return tiktoken


def _raw_output(lines: tuple[str, ...]) -> str:
    """Render the unfiltered monitoring view."""
    return "".join(line + "\n" for line in lines)


def _qurtail_output(lines: tuple[str, ...]) -> str:
    """Render one finite episode through the production reducer."""
    output = StringIO()
    reducer = _StreamReducer(output, dot_every=10)
    for line in lines:
        reducer.process(line)
    reducer.finish()
    return output.getvalue()


def _block_visible(output: str, block: tuple[str, ...]) -> bool:
    """Check that every line in a required diagnostic block stayed contiguous."""
    return "\n".join(block) + "\n" in output


def evaluate_logical_clock_replay() -> dict[str, object]:
    """Exercise a periodic summary over logical time without wall-clock delay."""
    output = StringIO()
    clock = _LogicalClock()
    reducer = _StreamReducer(
        output,
        dot_every=1,
        summary_interval=30.0,
        clock=clock,
    )
    reducer.process("INFO worker heartbeat")
    clock.now = 1.0
    reducer.process("INFO worker heartbeat")
    clock.now = 31.0
    reducer.tick()
    reducer.finish()
    rendered = output.getvalue()
    expected = "INFO worker heartbeat\n. [1 similar in 30s]\n"
    return {
        "source_duration_seconds": 31.0,
        "wall_clock_sleep_seconds": 0.0,
        "output": rendered,
        "expected_output": expected,
        "passed": rendered == expected,
    }


def evaluate_episode(
    episode: Episode,
    count_tokens: Callable[[str], int],
) -> dict[str, object]:
    """Measure token cost and required evidence for one monitoring episode."""
    setup = (
        SKILL_PATH.read_text(encoding="utf-8")
        + "\n$ qurtail -F -n 50 app.log\n"
        + episode.task
        + "\n"
    )
    raw = _raw_output(episode.lines)
    compact = _qurtail_output(episode.lines)
    raw_tokens = count_tokens(setup + raw)
    compact_tokens = count_tokens(setup + compact)
    reduction = 0.0 if raw_tokens == 0 else 1 - compact_tokens / raw_tokens
    retained = all(
        _block_visible(compact, block) for block in episode.required_blocks
    )
    return {
        "slug": episode.slug,
        "category": episode.category,
        "task": episode.task,
        "expected_decision": episode.expected_decision,
        "repetitive": episode.repetitive,
        "input_records": len(episode.lines),
        "raw_tokens": raw_tokens,
        "qurtail_tokens": compact_tokens,
        "token_reduction": reduction,
        "required_blocks": len(episode.required_blocks),
        "all_required_blocks_visible": retained,
    }


def evaluate_suite(
    count_tokens: Callable[[str], int],
) -> dict[str, object]:
    """Evaluate release safety and token gates across all held-out episodes."""
    results = [evaluate_episode(episode, count_tokens) for episode in EPISODES]
    repetitive_reductions = [
        float(result["token_reduction"])
        for result in results
        if result["repetitive"]
    ]
    nonrepetitive_inflation = [
        -float(result["token_reduction"])
        for result in results
        if not result["repetitive"]
    ]
    median_reduction = statistics.median(repetitive_reductions)
    maximum_inflation = max(nonrepetitive_inflation, default=0.0)
    all_evidence_visible = all(
        bool(result["all_required_blocks_visible"]) for result in results
    )
    logical_clock_replay = evaluate_logical_clock_replay()
    gates = {
        "all_required_blocks_visible": all_evidence_visible,
        "median_repetitive_token_reduction_at_least_80_percent": (
            median_reduction >= 0.80
        ),
        "nonrepetitive_token_inflation_at_most_2_percent": (
            maximum_inflation <= 0.02
        ),
        "logical_clock_summary_replay": bool(logical_clock_replay["passed"]),
    }
    return {
        "schema_version": 1,
        "episodes": results,
        "summary": {
            "median_repetitive_token_reduction": median_reduction,
            "maximum_nonrepetitive_token_inflation": maximum_inflation,
        },
        "logical_clock_replay": logical_clock_replay,
        "gates": gates,
        "passed": all(gates.values()),
    }


def main(argv: list[str] | None = None) -> int:
    """Run the held-out episode suite and optionally retain its JSON result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="write the complete machine-readable result",
    )
    args = parser.parse_args(argv)
    tiktoken = _load_tiktoken()
    tokenizer = tiktoken.get_encoding(ENCODING_NAME)
    result = evaluate_suite(lambda text: len(tokenizer.encode(text)))
    result["tokenizer"] = {
        "package": f"tiktoken {tiktoken.__version__}",
        "encoding": ENCODING_NAME,
    }
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
