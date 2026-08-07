"""Generate reproducible qurtail benchmark reports from the development log corpus."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path
import re
import sys
from typing import Callable

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

try:
    import tiktoken
except ImportError as error:
    raise SystemExit(
        "Install benchmark dependencies with "
        "'python -m pip install -r benchmarks/requirements.txt'."
    ) from error


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qurtail import compress  # noqa: E402


CORPUS_PATH = ROOT / "tests" / "fixtures" / "log_corpus.json"
BENCHMARKS_PATH = ROOT / "benchmarks"
ENCODING_NAME = "o200k_base"
REPEATS_BEFORE_FAILURE = 120
REPEATS_AFTER_FAILURE = 30


@dataclass(frozen=True)
class Scenario:
    """Describe an agent task, source format, filter, and qurtail configuration."""

    slug: str
    title: str
    role: str
    task: str
    case_name: str
    grep_pattern: str
    variation_pattern: str
    variation_template: str
    options: dict[str, object]


SCENARIOS = (
    Scenario(
        slug="swe-test-debugging",
        title="SWE test teardown debugging",
        role="Software engineer",
        task=(
            "Follow a noisy pytest run long enough to retain ordinary test progress and "
            "notice a teardown failure that can leave later tests contaminated."
        ),
        case_name="pytest-live-log",
        grep_pattern=r"ERROR|CRITICAL|FAILED|Traceback",
        variation_pattern=r"case \d+",
        variation_template="case {number}",
        options={
            "mode": "dots",
            "dot_every": 10,
            "similarity": 0.90,
            "ignore_levels": False,
        },
    ),
    Scenario(
        slug="it-kubernetes-troubleshooting",
        title="IT Kubernetes rollout troubleshooting",
        role="IT operator",
        task=(
            "Follow deployment reconciliation traffic while preserving the crash-loop event "
            "needed to diagnose a rollout that never becomes ready."
        ),
        case_name="kubernetes-cri",
        grep_pattern=r"stderr|error|fail|crash|panic",
        variation_pattern=r"generation=\d+",
        variation_template="generation={number}",
        options={
            "mode": "dots",
            "dot_every": 10,
            "similarity": 0.90,
            "ignore_timestamps": True,
            "ignore_prefixes": ("stdout F", "stderr F"),
        },
    ),
    Scenario(
        slug="dba-docker-startup",
        title="DBA container database startup diagnosis",
        role="Database administrator",
        task=(
            "Follow container health traffic while retaining the refused database connection "
            "that explains why an application replica cannot start."
        ),
        case_name="docker-json-file",
        grep_pattern=r'"stream":"stderr"|error|fail|panic|refused',
        variation_pattern=r"replica=\d+",
        variation_template="replica={number}",
        options={
            "mode": "dots",
            "dot_every": 10,
            "similarity": 0.90,
            "message_field": "log",
        },
    ),
)


def _load_cases() -> dict[str, dict[str, object]]:
    """Load corpus cases by stable case name."""
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    return {case["name"]: case for case in corpus["cases"]}


def _vary_recurring(line: str, scenario: Scenario, number: int) -> str:
    """Change one stable fixture identifier to create similar, non-identical noise."""
    replacement = scenario.variation_template.format(number=number)
    varied, replacements = re.subn(
        scenario.variation_pattern, replacement, line, count=1
    )
    if replacements != 1:
        raise ValueError(
            f"{scenario.slug}: variation pattern did not match exactly once"
        )
    return varied


def _stream(
    case: dict[str, object], scenario: Scenario
) -> tuple[list[str], str, str]:
    """Build a deterministic noisy stream around the case's exceptional line."""
    baseline, recurring, failure = case["lines"]
    recurring_lines = [
        _vary_recurring(recurring, scenario, number)
        for number in range(
            42, 42 + REPEATS_BEFORE_FAILURE + REPEATS_AFTER_FAILURE
        )
    ]
    lines = (
        [baseline]
        + recurring_lines[:REPEATS_BEFORE_FAILURE]
        + [failure]
        + recurring_lines[REPEATS_BEFORE_FAILURE:]
    )
    return lines, baseline, failure


def _tail(lines: list[str]) -> str:
    """Return every line, matching a naive tail consumer."""
    return "".join(line + "\n" for line in lines)


def _grep(lines: list[str], pattern: str) -> str:
    """Return lines accepted by a conventional case-insensitive grep filter."""
    expression = re.compile(pattern, re.IGNORECASE)
    return "".join(line + "\n" for line in lines if expression.search(line))


def _qurtail(lines: list[str], options: dict[str, object]) -> str:
    """Compress the stream through qurtail's public finite-stream interface."""
    output = StringIO()
    compress((line + "\n" for line in lines), output, **options)
    return output.getvalue()


def _has_marker_line(output: str) -> bool:
    """Return whether output contains a line made only from qurtail dot markers."""
    return any(line and set(line) == {"."} for line in output.splitlines())


def _format_config(options: dict[str, object]) -> str:
    """Render options as standard TOML accepted by qurtail."""
    lines = ["[qurtail]"]
    for name, value in options.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (tuple, list)):
            rendered = json.dumps(", ".join(str(item) for item in value))
        elif isinstance(value, float):
            rendered = f"{value:.2f}"
        elif isinstance(value, str):
            rendered = json.dumps(value)
        else:
            rendered = str(value)
        lines.append(f"{name} = {rendered}")
    config = "\n".join(lines)
    tomllib.loads(config)
    return config


def _yes_no(value: bool) -> str:
    """Format a benchmark outcome as a compact Markdown value."""
    return "yes" if value else "no"


def _render_report(
    scenario: Scenario,
    case: dict[str, object],
    count_tokens: Callable[[str], int],
) -> str:
    """Render one benchmark scenario and its measured outcomes."""
    lines, baseline, failure = _stream(case, scenario)
    outputs = {
        "Naive tail": _tail(lines),
        "Tail plus grep": _grep(lines, scenario.grep_pattern),
        "Qurtail": _qurtail(lines, scenario.options),
    }
    if baseline in outputs["Tail plus grep"] or failure not in outputs["Tail plus grep"]:
        raise ValueError(f"{scenario.slug}: grep outcome no longer matches the scenario")
    if not (
        baseline in outputs["Qurtail"]
        and failure in outputs["Qurtail"]
        and _has_marker_line(outputs["Qurtail"])
    ):
        raise ValueError(f"{scenario.slug}: qurtail did not retain all measured signals")
    tail_tokens = count_tokens(outputs["Naive tail"])
    rows = []
    for method, output in outputs.items():
        tokens = count_tokens(output)
        reduction = 100 * (tail_tokens - tokens) / tail_tokens
        recurrence_visible = (
            method == "Naive tail"
            or method == "Qurtail"
            and _has_marker_line(output)
        )
        rows.append(
            "| "
            + " | ".join(
                (
                    method,
                    str(output.count("\n")),
                    str(tokens),
                    f"{reduction:.1f}%",
                    _yes_no(baseline in output),
                    _yes_no(recurrence_visible),
                    _yes_no(failure in output),
                )
            )
            + " |"
        )

    config = _format_config(scenario.options)
    return f"""# {scenario.title}

**Role:** {scenario.role}

**Task:** {scenario.task}

**Format:** {case['format']} ([source]({case['source']}))

## Input

The deterministic stream contains one baseline line, {REPEATS_BEFORE_FAILURE} varying recurring lines, one context-relevant failure, and {REPEATS_AFTER_FAILURE} more varying recurring lines. Token counts cover emitted log text only.

The grep expression and qurtail config are both tuned for this scenario. This benchmark measures emitted tokens and retained evidence on a synthetic corpus-derived stream; it does not measure live I/O performance or whether an agent completes the diagnosis.

## Qurtail config

```toml
{config}
```

## Compared commands

- Naive tail: `tail -f <log>`
- Tail plus grep: `tail -f <log> | grep -Ei '{scenario.grep_pattern}'`
- Qurtail: `qurtail -c benchmarks/{scenario.slug}.toml -f <log>`

The benchmark runner applies the equivalent finite-stream selection and compression through Python.

## Results

Tokenizer: `tiktoken {tiktoken.__version__}`, encoding `{ENCODING_NAME}`.

| Method | Output lines | Tokens | Token reduction vs tail | Baseline context | Recurrence visible | Failure retained |
| --- | ---: | ---: | ---: | --- | --- | --- |
{chr(10).join(rows)}

## Outcome

Naive tail retains all evidence and spends the most context on recurring output. Tail plus grep retains the anticipated failure with the fewest tokens and drops ordinary context and recurrence evidence; qurtail retains all three measured signals while reducing emitted tokens.

Reproduce with `python benchmarks/run_benchmarks.py` after installing `benchmarks/requirements.txt`.
"""


def _artifacts() -> dict[Path, str]:
    """Generate every report and reusable override from the current implementation."""
    cases = _load_cases()
    tokenizer = tiktoken.get_encoding(ENCODING_NAME)

    def count_tokens(text: str) -> int:
        return len(tokenizer.encode(text))

    artifacts = {}
    for scenario in SCENARIOS:
        artifacts[BENCHMARKS_PATH / f"{scenario.slug}.md"] = _render_report(
            scenario, cases[scenario.case_name], count_tokens
        )
        artifacts[BENCHMARKS_PATH / f"{scenario.slug}.toml"] = (
            _format_config(scenario.options) + "\n"
        )
    return artifacts


def main(argv: list[str] | None = None) -> int:
    """Write reports, or verify that committed reports match current measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail when a benchmark report is stale"
    )
    args = parser.parse_args(argv)
    artifacts = _artifacts()

    if args.check:
        stale = [
            path
            for path, content in artifacts.items()
            if not path.exists() or path.read_text(encoding="utf-8") != content
        ]
        if stale:
            for path in stale:
                print(f"stale benchmark report: {path.relative_to(ROOT)}", file=sys.stderr)
            return 1
        print(f"{len(SCENARIOS)} benchmark reports and configs are current")
        return 0

    for path, content in artifacts.items():
        with path.open("w", encoding="utf-8", newline="\n") as destination:
            destination.write(content)
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
