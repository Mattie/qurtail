"""Capture sanitized multi-project Codex verification workloads and compare output."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS_PATH = ROOT / "benchmarks"
CORPUS_PATH = BENCHMARKS_PATH / "codex-workload-corpus.json"
REPORT_PATH = BENCHMARKS_PATH / "codex-workload-comparison.md"
ENCODING_NAME = "o200k_base"
REPEATS = 2
DEPENDENCY_HELP = (
    "Install benchmark dependencies with "
    "'python -m pip install -r benchmarks/requirements.txt'."
)


@dataclass(frozen=True)
class Workload:
    """Describe one command selected from a local Codex task record."""

    name: str
    title: str
    project: str
    technology: str
    purpose: str
    root_env: str | None
    launcher: str
    arguments: tuple[str, ...]
    original_agent_command: str
    source_observation: str
    success_signal: str

    def root(self) -> Path:
        """Resolve the local project root without recording it in the corpus."""
        if self.root_env is None:
            return ROOT
        value = os.environ.get(self.root_env)
        if not value:
            raise RuntimeError(f"set {self.root_env} to recapture {self.name}")
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"{self.root_env} does not point to a directory")
        return root

    def command(self) -> list[str]:
        """Return a platform-aware executable command for the workload."""
        if self.launcher == "python":
            return [sys.executable, *self.arguments]
        if self.launcher == "npm" and os.name == "nt":
            return [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/s",
                "/c",
                "npm.cmd",
                *self.arguments,
            ]
        executable = shutil.which(self.launcher)
        if executable is None:
            raise RuntimeError(f"{self.launcher} is required to recapture {self.name}")
        return [executable, *self.arguments]


WORKLOADS = (
    Workload(
        name="compression-regression",
        title="Compression regression suite",
        project="qurtail",
        technology="Python / unittest",
        purpose="Check qurtail's matching, diagnostics, summaries, and CLI behavior.",
        root_env=None,
        launcher="python",
        arguments=(
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_qurtail.py",
            "-v",
        ),
        original_agent_command=(
            "python -m unittest discover -s tests -p test_qurtail.py -v"
        ),
        source_observation="The source qurtail task recorded this suite passing.",
        success_signal="OK",
    ),
    Workload(
        name="canvas-frontend-regression",
        title="Canvas frontend regression",
        project="canvas-frontend",
        technology="React / PixiJS / Vitest",
        purpose="Check a Canvas interface and its article-view integration.",
        root_env="QURTAIL_CORPUS_CANVAS_ROOT",
        launcher="npm",
        arguments=("test", "--", "src/__tests__/CanvasApp.test.tsx"),
        original_agent_command="npm test -- src/__tests__/CanvasApp.test.tsx",
        source_observation=(
            "The source task ran this focused command before reporting its full frontend "
            "suite green."
        ),
        success_signal="passed",
    ),
    Workload(
        name="python-cli-smoke",
        title="Python CLI smoke test",
        project="python-cli",
        technology="Python / Poetry / Pytest",
        purpose="Check an installed console entry point in an isolated directory.",
        root_env="QURTAIL_CORPUS_PYTHON_CLI_ROOT",
        launcher="poetry",
        arguments=(
            "run",
            "pytest",
            "tests/test_init_files.py::test_installed_console_script_init_runs",
            "-q",
        ),
        original_agent_command=(
            "poetry run pytest "
            "tests/test_init_files.py::test_installed_console_script_init_runs -q"
        ),
        source_observation=(
            "The source task reported both CLI entry-path smoke tests passing."
        ),
        success_signal="passed",
    ),
    Workload(
        name="visual-renderer-regression",
        title="Visual renderer regression",
        project="visual-renderer",
        technology="TypeScript / Three.js / Vitest",
        purpose="Check deterministic camera-image-effect sampling and defaults.",
        root_env="QURTAIL_CORPUS_RENDERER_ROOT",
        launcher="npm",
        arguments=(
            "test",
            "--",
            "tests/steer/engine/cameraImageEffects.test.ts",
        ),
        original_agent_command=(
            "npm test -- tests/steer/engine/cameraImageEffects.test.ts"
        ),
        source_observation=(
            "The source task included this target in a 722-test passing suite."
        ),
        success_signal="passed",
    ),
)


def _environment(workload: Workload) -> dict[str, str]:
    """Build a stable child environment without passing common secret values."""
    environment = os.environ.copy()
    for name in tuple(environment):
        upper_name = name.upper()
        if upper_name.startswith("QURTAIL_CORPUS_") or any(
            marker in upper_name
            for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        ):
            environment.pop(name)
    environment.update(
        {
            "CI": "1",
            "FORCE_COLOR": "0",
            "NO_COLOR": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    if workload.root_env is None:
        environment["PYTHONPATH"] = str(ROOT / "tests")
    else:
        environment.pop("PYTHONPATH", None)
    return environment


def _redactions(resolved: list[tuple[Workload, Path]]) -> list[tuple[str, str]]:
    """Build machine-local path replacements for every captured project."""
    replacements = [
        (str(root), f"<repo:{workload.project}>")
        for workload, root in resolved
    ]
    replacements.extend(
        (
            (str(Path(sys.executable)), "python"),
            (str(Path.home()), "<home>"),
        )
    )
    return sorted(replacements, key=lambda item: len(item[0]), reverse=True)


def _sanitize(output: str, redactions: list[tuple[str, str]]) -> str:
    """Remove machine-local paths and nondeterministic timing from captured output."""
    sanitized = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    for local_path, label in redactions:
        for variant in (local_path, local_path.replace("\\", "/")):
            sanitized = re.sub(re.escape(variant), label, sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(
        r"(?m)^> [^\s@]+@[^\s]+ test$",
        "> <package>@<version> test",
        sanitized,
    )
    sanitized = re.sub(r"(?m)^(\s*Start at\s+).+$", r"\1<time>", sanitized)
    sanitized = re.sub(
        r"Ran (\d+) tests? in [0-9.]+s",
        r"Ran \1 tests in <elapsed>s",
        sanitized,
    )
    sanitized = re.sub(
        r"(?<![\w.])\d+(?:\.\d+)?(?:ms|s)\b",
        "<elapsed>",
        sanitized,
    )
    return sanitized.strip()


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    redactions: list[tuple[str, str]],
    input_text: str | None = None,
) -> tuple[int, str]:
    """Run one local command and return its exit status and sanitized combined output."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode, _sanitize(result.stdout, redactions)


def _raw_text(runs: list[dict[str, object]]) -> str:
    """Join actual command output in the same order qurtail receives it."""
    return "\n".join(str(run["output"]) for run in runs) + "\n"


def _capture() -> dict[str, object]:
    """Execute each workload and replay its sanitized output through the qurtail CLI."""
    resolved = [(workload, workload.root()) for workload in WORKLOADS]
    redactions = _redactions(resolved)
    cases = []
    for workload, root in resolved:
        runs = []
        for role in ("agent_reference", "rerun"):
            exit_code, output = _run(
                workload.command(),
                cwd=root,
                environment=_environment(workload),
                redactions=redactions,
            )
            if exit_code != 0:
                raise RuntimeError(
                    f"{workload.name}: workload command failed with exit code {exit_code}"
                )
            runs.append({"role": role, "exit_code": exit_code, "output": output})

        replay_command = [
            sys.executable,
            "-m",
            "qurtail",
            "--dot-every",
            "10",
        ]
        replay_exit_code, replay_output = _run(
            replay_command,
            cwd=ROOT,
            environment=_environment(WORKLOADS[0]),
            redactions=redactions,
            input_text=_raw_text(runs),
        )
        if replay_exit_code != 0:
            raise RuntimeError(
                f"{workload.name}: qurtail replay failed with exit code {replay_exit_code}"
            )
        if workload.success_signal.casefold() not in replay_output.casefold():
            raise RuntimeError(
                f"{workload.name}: qurtail replay lost the completion signal"
            )

        cases.append(
            {
                "name": workload.name,
                "title": workload.title,
                "project": workload.project,
                "technology": workload.technology,
                "purpose": workload.purpose,
                "command_source": "Codex task record",
                "original_agent_command": workload.original_agent_command,
                "source_observation": workload.source_observation,
                "success_signal": workload.success_signal,
                "repeats": REPEATS,
                "runs": runs,
                "qurtail": {
                    "command": "python -m qurtail --dot-every 10",
                    "exit_code": replay_exit_code,
                    "output": replay_output,
                },
            }
        )

    return {
        "status": "current-capture",
        "release_evidence": False,
        "methodology": (
            "Commands selected from Codex task records in multiple local Git "
            "projects were run twice by this Codex task. The exact command output received "
            "by the task was sanitized, combined, and replayed through the actual qurtail CLI."
        ),
        "provenance": (
            "The task records supplied command provenance and high-level success observations. "
            "Token measurements come from the fresh agent-reference and rerun executions."
        ),
        "sanitization": [
            "Replace every project root, executable path, and user home with portable labels.",
            "Strip ANSI control sequences and replace elapsed times and wall-clock starts.",
            "Replace package banner names with a generic label.",
            "Exclude prompts, transcript prose, task IDs, environment values, and external logs.",
        ],
        "scope": (
            "These captures measure fresh reruns of source commands from separate projects. "
            "They do not claim byte-for-byte identity with archived task output. Live file-follow "
            "performance and agent task completion are outside scope."
        ),
        "cases": cases,
    }


def _load_tiktoken():
    """Load the optional tokenizer used for the generated comparison report."""
    try:
        import tiktoken
    except ImportError as error:
        raise SystemExit(DEPENDENCY_HELP) from error
    return tiktoken


def _render_report(corpus: dict[str, object]) -> str:
    """Render measured raw and qurtail output from the captured workload corpus."""
    tiktoken = _load_tiktoken()
    tokenizer = tiktoken.get_encoding(ENCODING_NAME)
    rows = []
    commands = []
    for case in corpus["cases"]:
        raw_output = _raw_text(case["runs"])
        qurtail_output = case["qurtail"]["output"] + "\n"
        raw_tokens = len(tokenizer.encode(raw_output))
        qurtail_tokens = len(tokenizer.encode(qurtail_output))
        reduction = 100 * (raw_tokens - qurtail_tokens) / raw_tokens
        rows.append(
            f"| `{case['project']}` | {case['technology']} | "
            f"{raw_output.count(chr(10))} | {raw_tokens} | "
            f"{qurtail_output.count(chr(10))} | {qurtail_tokens} | "
            f"{reduction:.1f}% | `{case['success_signal']}` |"
        )
        commands.append(
            f"- **{case['title']}** (`{case['project']}`, {case['technology']}):\n"
            f"  Original agent command: `{case['original_agent_command']}`  \n"
            f"  {case['purpose']} {case['source_observation']}"
        )

    return f"""# Codex live workload replay

This corpus samples {len(corpus['cases'])} verification workloads selected from Codex task records in separate local Git projects. The same source commands ran {REPEATS} times in this task, and the exact output Codex received was sanitized and replayed through the real qurtail CLI.

Project names are replaced with functional aliases. No prompts, transcript prose, task IDs, environment values, repository paths, or external machine logs are included.

## Original agent command lines

Each command line below is copied from its Codex task record. It produced the output the original agent monitored during that task.

{chr(10).join(commands)}

## Comparison

Tokenizer: `tiktoken {tiktoken.__version__}`, encoding `{ENCODING_NAME}`.

| Project alias | Technology | Raw lines | Raw tokens | Qurtail lines | Qurtail tokens | Token reduction | Completion retained |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
{chr(10).join(rows)}

Qurtail replay command: `python -m qurtail --dot-every 10`.

## Limits

The task records establish which commands earlier agents used and their reported outcomes. These token measurements are fresh reruns of those source commands, so they do not claim byte-for-byte identity with archived task output.

The captured material is completed command output replayed after each run. Live file-follow performance, unattended agent completion, diagnosis quality, and broad tool superiority are outside this comparison.

The replay uses qurtail's built-in conservative signatures after path and timing normalization. These rows measure repeat-command compression; held-out monitoring episodes provide the matcher safety evidence.

Re-capture requires the three private project-root environment variables named in `capture_codex_workloads.py`; their values are never written to the artifacts. Then run `python benchmarks/capture_codex_workloads.py` after installing `benchmarks/requirements.txt`.
"""


def _artifacts() -> dict[Path, str]:
    """Capture live workloads and return their serialized corpus and report."""
    corpus = _capture()
    return {
        CORPUS_PATH: json.dumps(corpus, indent=2, ensure_ascii=True) + "\n",
        REPORT_PATH: _render_report(corpus),
    }


def main(argv: list[str] | None = None) -> int:
    """Write live capture artifacts or verify them against a fresh command replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when committed live workload captures are stale",
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
                print(
                    f"stale live workload artifact: {path.relative_to(ROOT)}",
                    file=sys.stderr,
                )
            return 1
        print(f"{len(WORKLOADS)} live Codex workload captures are current")
        return 0

    for path, content in artifacts.items():
        with path.open("w", encoding="utf-8", newline="\n") as destination:
            destination.write(content)
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
