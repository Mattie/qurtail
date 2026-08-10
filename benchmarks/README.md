# Benchmark inventory

## Current 1.0 evidence

- `run_benchmarks.py` defines dependency-free held-out monitoring episodes.
- `run_performance.py` measures throughput, peak additional memory, first-output latency, and an
  informational framing CPU ratio.
- `large_corpus.py`, `run_large_corpus.py`, and `corpus/manifest.json` reproduce the hashed local
  corpus workflow.
- `hdfs-full-results.json` is the complete current-source HDFS replay.

## Historical evidence

- `paired-agent-results.json` is tied to its recorded pre-1.0 source and skill hashes.
- `codex-workload-corpus.json` and `codex-workload-comparison.md` preserve the pre-1.0 workload
  replay, including CLI options that no longer exist.
- The workload-specific Markdown and TOML pairs such as `swe-test-debugging.*` and
  `dba-docker-startup.*` are pre-1.0 fuzzy-matcher experiments.

Historical files remain for provenance. They are excluded from current release gates and must not
be used as current CLI instructions. Current commands are documented in the repository README and
the two runner modules above.
