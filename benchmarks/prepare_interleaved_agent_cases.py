"""Prepare fixed raw/compact agent tasks, including polling and raw recovery.

The fixtures are independently written. Evaluate the arms with fresh agents and
retain their answers and file-read receipts; this script does not call any model.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qurtail import _StreamReducer  # noqa: E402
from benchmarks.run_interleaved import TranscriptAudit  # noqa: E402


GUIDANCE = """Investigate only the provided log evidence. Read the initial poll files named below.
If evidence is missing, you may read poll-1.txt or raw.log in this same directory.
Do not inspect other directories, other agents, implementation code, or the internet.

Qurtail prints full examples and replaces familiar occurrences with dots/counts.
A count may combine several previously printed patterns. It does not identify which
patterns repeated, their order, or their timestamps. Existing normalization can also
hide request IDs. Read raw.log when those missing details are needed for an answer.

Return JSON with answers (an object containing the requested fields), evidence (a short
explanation with timestamps or quoted values), files_read (every filename read, in order,
including this task.md), and tool_calls (the number of tool calls used to read evidence).
Do not infer missing values or report a successful recovery without evidence.
"""


def cases() -> list[dict]:
    """Define the scenarios and answer keys before either view is evaluated."""
    peers = []
    for attempt in range(40):
        peers.extend([
            "WARN cannot connect to peer 2 at election address /10.10.34.12:3888",
            "WARN cannot connect to peer 3 at election address /10.10.34.13:3888",
            f"INFO notification timeout: {min(400 * 2 ** (attempt // 4), 60000)}",
        ])
    a = "INFO worker-a cache refresh completed successfully request_id=steady"
    b = "INFO worker-b cache refresh completed successfully request_id=steady"
    recovery = [a, b] * 12 + [
        "ERROR worker-a cache refresh failed: connection refused",
        a,
        *(a.replace("request_id=steady", f"request_id=retry-{n}") for n in range(1, 4)),
        b,
    ]
    healthy2 = "INFO connection to peer 2 is healthy and accepting requests"
    healthy3 = "INFO connection to peer 3 is healthy and accepting requests"
    failed3 = "ERROR connection to peer 3 failed with connection refused"
    gap = [healthy2, healthy3] * 4 + [failed3, healthy3] * 4 + [healthy2, failed3, healthy3] * 16
    return [
        {"id": "peers-and-backoff", "payloads": peers, "split": 60,
         "initial": ["poll-1.txt", "poll-2.txt"],
         "question": "Return failed_peers (sorted integer list) and final_timeout_ms (integer). Identify the peers that cannot connect and the latest reported notification timeout.",
         "expected": {"failed_peers": [2, 3], "final_timeout_ms": 60000}},
        {"id": "recovery-and-raw-id", "payloads": recovery, "split": 24,
         "initial": ["poll-1.txt", "poll-2.txt"],
         "question": "Return recovered (boolean), first_recovery_timestamp (ISO string), and final_worker_a_request_id (string). Did worker-a complete a successful refresh after connection refused? What is the first successful timestamp after the failure and the last request ID actually logged for worker-a?",
         "expected": {"recovered": True, "first_recovery_timestamp": "2026-09-10T12:00:25Z", "final_worker_a_request_id": "retry-3"}},
        {"id": "later-poll-without-anchors", "payloads": gap, "split": 31,
         "initial": ["poll-2.txt"],
         "question": "Return failing_peer (integer) and recovered_at_end (boolean). During this later poll, which peer has connection-refused failures, and does its last observed state show recovery?",
         "expected": {"failing_peer": 3, "recovered_at_end": True}},
    ]


def prepare(destination: Path) -> dict:
    """Write identical tasks with paired evidence arms and a separate manifest."""
    manifest = {"schema_version": 1, "qurtail_sha256": hashlib.sha256((ROOT / "qurtail.py").read_bytes()).hexdigest(),
                "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "cases": []}
    base = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    for index, case in enumerate(cases()):
        lines = [f"{(base + timedelta(seconds=i)).isoformat().replace('+00:00', 'Z')} {payload}\n"
                 for i, payload in enumerate(case["payloads"])]
        output = StringIO()
        reducer = _StreamReducer(output, clock=lambda: 0.0)
        for line in lines[:case["split"]]:
            reducer.process(line)
        first = output.getvalue()
        for line in lines[case["split"]:]:
            reducer.process(line)
        reducer.finish()
        compact = output.getvalue()
        audit = TranscriptAudit(lines)
        audit.write(compact)
        audit.finish()
        views = {"raw": ["".join(lines[:case["split"]]), "".join(lines[case["split"]:])],
                 "compact": [first, compact[len(first):]]}
        task = GUIDANCE + "\nInitial poll files: " + ", ".join(case["initial"]) + "\n\n" + case["question"] + "\n"
        arms = {}
        for arm, view in zip(("arm-a", "arm-b"), ("raw", "compact") if index % 2 == 0 else ("compact", "raw")):
            folder = destination / case["id"] / arm
            folder.mkdir(parents=True, exist_ok=True)
            contents = {"task.md": task, "poll-1.txt": views[view][0], "poll-2.txt": views[view][1], "raw.log": "".join(lines)}
            for name, text in contents.items():
                (folder / name).write_bytes(text.encode("utf-8"))
            arms[arm] = {"view": view, "files": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in contents.items()}}
        manifest["cases"].append({"id": case["id"], "records": len(lines), "split_after_source_record": case["split"],
                                  "initial_files": case["initial"], "expected": case["expected"], "arms": arms})
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = prepare(args.destination)
    print(f"Prepared {len(result['cases'])} paired tasks in {args.destination}")
