"""Read a bounded agent evidence file and retain an exact payload receipt."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ALLOWED = {"task.md", "poll-1.txt", "poll-2.txt", "raw.log"}


def read_evidence(folder: Path, names: list[str]) -> str:
    """Record each complete returned file, including repeated reads and framing."""
    payloads = []
    for name in names:
        if name not in ALLOWED:
            raise ValueError("only task.md, poll-1.txt, poll-2.txt, and raw.log are available")
        data = (folder / name).read_bytes()
        payload = f"--- {name} ---\n" + data.decode("utf-8")
        if not payload.endswith("\n"):
            payload += "\n"
        receipt = {"file": name, "file_sha256": hashlib.sha256(data).hexdigest(),
                   "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                   "payload_bytes": len(payload.encode())}
        with (folder / "reads.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(receipt) + "\n")
        payloads.append(payload)
    return "".join(payloads)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    sys.stdout.write(read_evidence(args.folder, args.files))
