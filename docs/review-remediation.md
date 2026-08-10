# Pre-commit review remediation

Date: 2026-08-09

This document records how the qurtail 1.0 working-tree review findings were resolved and which
checks support each resolution.

## Runtime correctness

- File following now keeps bounded checkpoints from the beginning and recent consumed content,
  including a final-window sample for zero-context following and prefix-aware incomplete final
  records. At end-of-file, qurtail compares those samples with the live inode before reading again.
  Same-inode copy-truncate rewrites that change retained checkpoints rewind to the beginning instead
  of skipping the rewritten prefix.
- POSIX interruption now drains the child pipe concurrently with shutdown. Signal escalation
  targets the complete isolated process group through `SIGINT`, `SIGTERM`, and `SIGKILL` grace
  stages.
- `SIGTERM` received by qurtail is converted into orderly child cleanup and returns status 143.
  `SIGINT` continues to return status 130 when the child exits successfully during cleanup.
- Real-process regressions cover a 512 KiB signal-handler write and verify that `SIGTERM` leaves no
  child process behind. The existing Windows control-break test remains the portable contract
  check for Windows.

## Benchmark and provenance integrity

- Large-corpus runs credit suppressed line occurrences when an exact count represents them. A
  stable block, application, or instance identifier counts as retained only after its full source
  record is visible.
- Extracted dataset completion markers bind both the verified archives and SHA-256 hashes for every
  extracted file. Corpus indexes record SHA-256 for every selected log and label file, and the
  runner verifies those hashes before evaluation. Rebuilding an index cannot bless locally mutated
  extracted data.
- Large-corpus reports include hashes of both evaluator modules and a canonical, fully embedded
  selected-index record. Source or corpus provenance changes therefore invalidate the committed
  evidence check.
- `run_large_corpus.py` now performs baselines by default. Qurtail measurement requires
  `--qurtail`; conflicting baseline-only flags are rejected.
- The resumed-pattern episode requires the first successful post-incident heartbeat as evidence.
- The unsupported three-times-framing CPU threshold was removed from the release contract. CPU
  ratio remains reported for comparison. Throughput, memory, and first-output latency remain
  enforced gates, and their test now requires the aggregate result and every gate to pass.

## Evidence and distribution scope

- `.agents/skills/qurtail-fluency` is the repository-discovery copy of the skill and is included in
  the commit. The README now states that PyPI, `uv tool`, and `pipx` install the CLI only.
- The 2026-08-08 paired-agent report is labeled historical, tied to its original hashed inputs, and
  excluded from current release gates. The stale current-source verification step was removed.
- The older multi-project Codex workload corpus and report are also labeled historical pre-1.0
  evidence. Removed commands remain visible as provenance and cannot be mistaken for current CLI
  instructions.
- Unsupported normal-discovery adoption and paired-agent release claims were removed from the
  README. A new evaluation can restore those claims after it runs against the final skill and
  current qurtail build.
- `README_old.md` remains a workstation backup and is ignored by Git.

## Verification completed

- `python -m unittest discover -s tests -v`: 80 tests passed.
- Isolated wheel build and `tests/installed_smoke.py`: passed.
- Reference performance command: passed all enforced gates.
- Complete HDFS replay: 11,175,629 records in 174.016 seconds, 109,882 qurtail records per second,
  100% retained HDFS anomaly-block recall, and every release gate passed.
- `git diff --check`: passed.

GitHub Actions still supplies the final Python 3.11 checks on Ubuntu, macOS, and Windows after the
commit is pushed.
