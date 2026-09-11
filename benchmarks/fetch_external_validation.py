"""Fetch a fixed, licensed validation set without running third-party code."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request

LOGDX = ("eyuansu62/LogDx", "fc957f0d0d0082019606cc20b8fb545683a03b44")
ROOTLY = ("Rootly-AI-Labs/logs-dataset", "5d7448debdf22ad27358fdcc62fc36205496e3f8")
RCA = ("phamquiluan/RCAEval", "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e")
RCA_SERVICES = ("re2ob_checkoutservice", "re2ss_carts", "re2tt_ts-order-service")
RCA_FAULTS = ("cpu", "delay", "socket")


def read_url(url: str) -> bytes:
    """Read public pinned data with a finite timeout and no credentials."""
    request = urllib.request.Request(url, headers={"User-Agent": "qurtail-validation"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def read_json(url: str) -> object:
    """Decode public repository metadata."""
    return json.loads(read_url(url))


def github_entries(repo: str, revision: str, family: str) -> list[dict]:
    """Select raw logs, labels, and license records from an immutable Git tree."""
    tree = read_json(f"https://api.github.com/repos/{repo}/git/trees/{revision}?recursive=1")
    if tree.get("truncated"):
        raise ValueError("repository tree was truncated")
    entries = []
    for item in tree["tree"]:
        path = item["path"]
        if family == "logdx":
            selected = path.startswith("cases/v2/") and path.endswith(
                ("/raw.log", "/ground_truth.json", "/case.json", "/tags.json"))
            selected |= path in ("LICENSE", "LICENSE-DATA", "README.md")
        else:
            selected = path in ("LICENSE", "README.md", "apache/apache_access.log",
                               "apache/apache_error.log", "openssh/openssh.log")
        if selected:
            entries.append({"family": family, "path": f"{family}/{path}",
                            "url": f"https://raw.githubusercontent.com/{repo}/{revision}/{path}",
                            "revision": revision, "size": item["size"],
                            "git_blob_sha1": item["sha"]})
    return entries


def rca_entries() -> list[dict]:
    """Select one repetition of three named faults in three named systems."""
    repo, revision = RCA
    entries = []
    for service in RCA_SERVICES:
        for fault in RCA_FAULTS:
            case = f"{service}_{fault}_1"
            items = read_json(f"https://huggingface.co/api/datasets/{repo}/tree/{revision}/{case}?expand=true")
            for item in items:
                if item["path"].endswith(("/logs.parquet", "/inject_time.txt")):
                    entry = {"family": "rcaeval", "path": "rcaeval/" + item["path"],
                             "url": f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{item['path']}",
                             "revision": revision, "size": item["size"]}
                    if item.get("lfs"):
                        entry["sha256"] = item["lfs"]["oid"]
                    else:
                        entry["git_blob_sha1"] = item["oid"]
                    entries.append(entry)
            if not any(e["path"] == f"rcaeval/{case}/logs.parquet" for e in entries):
                raise ValueError(f"selected case has no logs: {case}")
    # The MIT grant explicitly covers the datasets in this pinned README.
    source_revision = "bb48c5aa9a24f1d5fcc716bdd479ea2d63145c90"
    for name in ("LICENSE", "README.md"):
        entries.append({"family": "rcaeval", "path": f"rcaeval/{name}",
                        "url": f"https://raw.githubusercontent.com/phamquiluan/RCAEval/{source_revision}/{name}",
                        "revision": source_revision})
    return entries


def fetch_one(root: Path, entry: dict) -> dict:
    """Verify upstream content hashes before retaining bytes in the local corpus."""
    path = root / entry["path"]
    data = path.read_bytes() if path.exists() else read_url(entry["url"])
    digest = hashlib.sha256(data).hexdigest()
    if "size" in entry and len(data) != entry["size"]:
        raise ValueError(f"size mismatch: {entry['path']}")
    if "sha256" in entry and digest != entry["sha256"]:
        raise ValueError(f"SHA-256 mismatch: {entry['path']}")
    if "git_blob_sha1" in entry:
        git_digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if git_digest != entry["git_blob_sha1"]:
            raise ValueError(f"Git blob mismatch: {entry['path']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"verified {entry['path']} ({len(data):,} bytes)", flush=True)
    return {**entry, "size": len(data), "sha256": digest}


def main() -> None:
    """Record selection before measurement and write a reproducible input manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    entries = (github_entries(*LOGDX, "logdx") + github_entries(*ROOTLY, "rootly")
               + rca_entries())
    with ThreadPoolExecutor(max_workers=4) as pool:
        files = list(pool.map(lambda entry: fetch_one(args.root, entry), entries))
    report = {
        "schema_version": 1,
        "selection": {
            "logdx": "All 19 cases under cases/v2, selected before compression measurement.",
            "rootly": "All three raw log files, preserving stored physical-line order.",
            "rcaeval": {"services": RCA_SERVICES, "faults": RCA_FAULTS, "repetition": 1,
                        "note": "Nine complete log tables; no selection by compression outcome."},
        },
        "licenses": {"logdx": "CC-BY-4.0: retain attribution and identify modifications",
                     "rootly": "Apache-2.0: retain license/notices and identify modifications",
                     "rcaeval": "MIT: retain copyright and license"},
        "files": files,
    }
    args.manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
