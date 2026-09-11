"""Score blinded answers and verify the reader's ordered payload ledger."""

import argparse
import hashlib
import json
from pathlib import Path

import tiktoken


def score(cases_root: Path, receipts_path: Path) -> dict:
    """Count emitted payloads; exclude truncated runs from comparative claims."""
    manifest = json.loads((cases_root / "manifest.json").read_text())
    receipts = json.loads(receipts_path.read_text())
    encoder = tiktoken.get_encoding("o200k_base")
    expected = {(case["id"], arm): (case, spec)
                for case in manifest["cases"] for arm, spec in case["arms"].items()}
    seen, results = set(), []
    for receipt in receipts["receipts"]:
        pair = receipt["case"], receipt["arm"]
        if pair not in expected or pair in seen:
            raise ValueError("unknown or duplicate case/arm receipt")
        seen.add(pair)
        case, spec = expected[pair]
        folder = cases_root / pair[0] / pair[1]
        ledger = [json.loads(line) for line in (folder / "reads.jsonl").read_text().splitlines()]
        if [item["file"] for item in ledger] != receipt["files_read"]:
            raise ValueError("reported reads differ from recorded reads")
        reads = []
        for item in ledger:
            name = item["file"]
            data = (folder / name).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            payload = f"--- {name} ---\n" + data.decode("utf-8")
            if not payload.endswith("\n"):
                payload += "\n"
            encoded = payload.encode()
            if (digest != spec["files"][name] or digest != item["file_sha256"]
                    or hashlib.sha256(encoded).hexdigest() != item["payload_sha256"]
                    or len(encoded) != item["payload_bytes"]):
                raise ValueError("evidence or payload receipt changed")
            reads.append({**item, "emitted_tokens": len(encoder.encode(payload, disallowed_special=()))})
        results.append({**receipt, "view": spec["view"], "reads": reads,
                        "answers_match": receipt["answers"] == case["expected"],
                        "emitted_tokens": sum(item["emitted_tokens"] for item in reads)})
    if seen != set(expected):
        raise ValueError("missing case/arm receipt")
    complete_cases = [case["id"] for case in manifest["cases"] if all(
        item["delivery_complete"] for item in results if item["case"] == case["id"])]
    totals = {view: sum(item["emitted_tokens"] for item in results
                        if item["view"] == view and item["case"] in complete_cases)
              for view in ("raw", "baseline", "current")}
    return {"schema_version": 1, "fixture_manifest": manifest, "protocol": receipts["protocol"],
            "receipts_sha256": hashlib.sha256(receipts_path.read_bytes()).hexdigest(),
            "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "tokenizer": {"package": f"tiktoken {tiktoken.__version__}", "encoding": "o200k_base",
                          "scope": "whole emitted reader payloads including framing and rereads; truncated cases excluded from comparative totals; excludes tool wrappers, launch prompts, reasoning, and answers"},
            "results": results, "complete_cases": complete_cases,
            "complete_case_emitted_tokens": totals}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = score(args.cases, args.receipts)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["complete_case_emitted_tokens"]))
