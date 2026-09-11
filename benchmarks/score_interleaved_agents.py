"""Score retained agent answers and tokenize every reported task/evidence read."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import tiktoken


def score(cases_root: Path, receipts_path: Path) -> dict:
    """Require paired receipts and verified files, then count whole-file reads."""
    manifest = json.loads((cases_root / "manifest.json").read_text(encoding="utf-8"))
    receipt_data = json.loads(receipts_path.read_text(encoding="utf-8"))
    encoding = tiktoken.get_encoding("o200k_base")
    cases = {case["id"]: case for case in manifest["cases"]}
    expected_pairs = {(case["id"], arm) for case in cases.values() for arm in case["arms"]}
    seen = set()
    results = []
    for receipt in receipt_data["receipts"]:
        pair = (receipt["case"], receipt["arm"])
        if pair not in expected_pairs or pair in seen:
            raise ValueError("unknown or duplicate case/arm receipt")
        if receipt.get("output_truncated") is not False:
            raise ValueError("receipt must confirm evidence delivery was not truncated")
        seen.add(pair)
        case = cases[pair[0]]
        arm = case["arms"][pair[1]]
        ledger_path = cases_root / pair[0] / pair[1] / "reads.jsonl"
        ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        if [item["file"] for item in ledger] != receipt["files_read"]:
            raise ValueError("reported reads differ from the evidence-reader ledger")
        reads = []
        for item in ledger:
            name = item["file"]
            expected_hash = arm["files"][name]
            data = (cases_root / pair[0] / pair[1] / name).read_bytes()
            payload = f"--- {name} ---\n" + data.decode("utf-8")
            if not payload.endswith("\n"):
                payload += "\n"
            encoded = payload.encode("utf-8")
            if (hashlib.sha256(data).hexdigest() != expected_hash
                    or item["file_sha256"] != expected_hash
                    or item["payload_sha256"] != hashlib.sha256(encoded).hexdigest()
                    or item["payload_bytes"] != len(encoded)):
                raise ValueError("evidence or payload receipt changed")
            reads.append({"file": name, "sha256": expected_hash,
                          "tokens": len(encoding.encode(data.decode("utf-8"), disallowed_special=()))})
        results.append({**receipt, "view": arm["view"], "reads": reads,
                        "task_and_evidence_tokens": sum(read["tokens"] for read in reads),
                        "passed": receipt["answers"] == case["expected"]})
    if seen != expected_pairs:
        raise ValueError("missing case/arm receipt")
    totals = {view: sum(result["task_and_evidence_tokens"] for result in results
                        if result["view"] == view) for view in ("raw", "compact")}
    return {
        "schema_version": 1,
        "protocol": receipt_data["protocol"],
        "fixture_manifest": manifest,
        "receipts_sha256": hashlib.sha256(receipts_path.read_bytes()).hexdigest(),
        "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "reader_sha256": hashlib.sha256(Path(__file__).with_name("read_external_evidence.py").read_bytes()).hexdigest(),
        "tokenizer": {"package": f"tiktoken {tiktoken.__version__}", "encoding": "o200k_base",
                      "scope": "sum of each whole task/evidence file read, including rereads"},
        "results": results,
        "total_task_and_evidence_tokens": totals,
        "aggregate_token_reduction": 1 - totals["compact"] / totals["raw"],
        "passed": all(result["passed"] for result in results),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = score(args.cases, args.receipts)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "tokens": report["total_task_and_evidence_tokens"],
                      "reduction": report["aggregate_token_reduction"]}))
    raise SystemExit(0 if report["passed"] else 1)
