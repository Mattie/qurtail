# Benchmark inventory

## Current 1.1 evidence

- `run_benchmarks.py` defines dependency-free held-out monitoring episodes.
- `run_performance.py` measures throughput, peak additional memory, first-output latency, and an
  informational framing CPU ratio.
- `large_corpus.py`, `run_large_corpus.py`, and `corpus/manifest.json` reproduce the hashed local
  corpus workflow.
- `hdfs-full-results.json` is the complete current-source HDFS replay.
- `interleaved-results.json` compares complete ZooKeeper, Linux, and OpenSSH logs,
  with an independent source-occurrence audit and actual tokenizer counts.
- `interleaved-agent-results.json` scores six fresh-agent runs from the retained
  `interleaved-agent-receipts.json`, including earlier-poll and raw-log reads.
- `interleaved-validation.json` retains the Linux and Windows validation results.

## Interleaved dots and counts

The final-source replay on 2026-09-11 passed the 25% ZooKeeper byte-reduction gate.
Percentages below compare compact output with decoded source records, including LF:

| Complete corpus | Records | Previous byte reduction | Current byte reduction | Current token reduction |
| --- | ---: | ---: | ---: | ---: |
| ZooKeeper | 74,380 | 0.41% | 60.49% | 57.98% |
| Linux | 25,567 | 0.06% | 9.15% | 9.40% |
| OpenSSH | 655,147 | 0.004% | 1.37% | 1.32% |

Every source record was accounted for as a full record or a counted occurrence of an
already printed normalized pattern. Mixed counts hide individual identities and ordering.
Corpus tokens use `tiktoken 0.13.0` / `o200k_base`, summed per complete logical record.
All three corpora shrank in aggregate. A short stream can still grow because of its closing
count: `A, B, A, B` is eight raw bytes and 31 output bytes with the default dot density.

All six agent runs correctly identified the requested peers, backoff, recovery, and final
request ID. Whole-file task/evidence token counts include every ledger-verified read:

| Fixed task | Raw tokens | Compact tokens | Compact change |
| --- | ---: | ---: | ---: |
| Peer failures and latest backoff | 3,856 | 4,201 | 8.9% higher |
| Recovery and final raw request ID | 1,756 | 1,092 | 37.8% lower |
| Later poll only | 1,039 | 1,813 | 74.5% higher |
| Total | 6,651 | 7,106 | 6.84% higher |

These are three paired tasks with one fresh agent per arm, alternating blinded arm labels.
All three compact arms retrieved raw evidence; the raw recovery arm also chose to read
`raw.log` after its polls. No evidence delivery was truncated. The scorer verifies ordered
reader ledgers and fixture hashes. Counts exclude reader headers, provider framing, model
reasoning/output, and launcher instructions. This small evaluation supports correct answers
with raw recovery; it does not establish aggregate agent token savings.

The full 11,175,629-record HDFS replay retained all 16,838 available anomaly units and took
220.2 seconds including baseline scans. Held-out monitoring gates passed in their recorded
modes with zero nonrepetitive token inflation. The recovery episode needs `--no-interleaving`;
its default-mode visibility failure is retained explicitly. All 140 tests passed on Linux;
Windows ran the same suite with five platform skips. Installed-command smoke checks for
both modes and performance gates passed on both platforms.
macOS remains unverified locally; the existing CI matrix includes it.

### Reproduce

Install `benchmarks/requirements.txt` in the project environment. Fetching verifies archives,
extracts them, and binds the index to the extracted bytes:

```bash
python benchmarks/large_corpus.py fetch loghub-zookeeper loghub-linux loghub-openssh loghub-hdfs-v1
python benchmarks/run_interleaved.py --output benchmarks/interleaved-results.json
python benchmarks/run_large_corpus.py loghub-hdfs-v1 --qurtail --output benchmarks/hdfs-full-results.json
python benchmarks/run_benchmarks.py
python benchmarks/run_performance.py
```

The recorded corpus run used a separate verified local root under
`benchmarks/corpus/local/interleaved-review/verified`. Pass `--root` to use it.
The optional `--baseline-source PATH` loads the retained pre-change local module;
its SHA-256 is recorded in the report. That local snapshot includes pre-existing
uncommitted work and is not distributed. Omitting it reproduces the current-source arm.
To reproduce agent scoring with the original local fixtures and reader ledgers:

```bash
python benchmarks/score_interleaved_agents.py --cases benchmarks/corpus/local/interleaved-review/dot-agent-cases --receipts benchmarks/interleaved-agent-receipts.json --output benchmarks/interleaved-agent-results.json
```

For a new trial, use `prepare_interleaved_agent_cases.py DESTINATION` and fresh agents reading
each arm through `read_external_evidence.py`, without its answer key or opposite arm.
Do not overwrite active fixtures or reuse retained answers with new reader ledgers.

External validation against LogDx, Rootly, and RCAEval is documented in
[`docs/external-validation.md`](../docs/external-validation.md), including baseline
comparisons, the opt-out's byte-identical output on all 31 cases, individual-occurrence
visibility losses, and reproduction limits.

## Historical evidence

- `history/reference-design/` retains the rejected labeled-reference experiment's reports.
- `paired-agent-results.json` is tied to its recorded pre-1.0 source and skill hashes.
- `codex-workload-corpus.json` and `codex-workload-comparison.md` preserve the pre-1.0 workload
  replay, including CLI options that no longer exist.
- The workload-specific Markdown and TOML pairs such as `swe-test-debugging.*` and
  `dba-docker-startup.*` are pre-1.0 fuzzy-matcher experiments.

Historical files remain for provenance. They are excluded from current release gates and must not
be used as current CLI instructions. Current commands are documented in the repository README and
the two runner modules above.
