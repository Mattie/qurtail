"""Smoke-test the installed qurtail console command through standard input."""

from __future__ import annotations

import importlib.util
from importlib.metadata import distribution
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    """Require the installed command to compact one duplicate record."""
    if importlib.util.find_spec("smartytail") is not None:
        raise SystemExit("removed smartytail module is still present in the install")
    if any("smartytail" in str(path) for path in distribution("qurtail").files or ()):
        raise SystemExit("removed smartytail files are still present in the wheel")

    result = subprocess.run(
        ["qurtail"],
        input="heartbeat\nheartbeat\n",
        text=True,
        capture_output=True,
        check=True,
    )
    expected = "heartbeat\n. [1 similar before stop]\n"
    if result.stdout != expected:
        raise SystemExit(
            f"unexpected qurtail output: {result.stdout!r}; expected {expected!r}"
        )

    script = (
        "import os, sys; "
        "os.write(1, b'heartbeat\\n'); "
        "os.write(2, b'heartbeat\\n'); "
        "sys.exit(7)"
    )
    with tempfile.TemporaryDirectory() as temporary:
        raw_log = Path(temporary) / "child.raw.log"
        runner = subprocess.run(
            [
                "qurtail",
                "run",
                "--raw-log",
                str(raw_log),
                "--",
                sys.executable,
                "-c",
                script,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if runner.returncode != 7:
            raise SystemExit(f"runner returned {runner.returncode}; expected 7")
        if runner.stdout != "heartbeat\n. [1 similar before stop]\n":
            raise SystemExit(f"unexpected runner output: {runner.stdout!r}")
        if raw_log.read_bytes() != b"heartbeat\nheartbeat\n":
            raise SystemExit("runner raw log did not preserve combined child output")

    print("installed qurtail stdin and runner smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
