# Large log corpus

This corpus is the main evidence set for qurtail's compression, signal-retention, and throughput
claims. It contains naturally ordered logs with tens of thousands to millions of records. The
downloaded files stay local; Git stores the manifest and the code needed to reproduce them.

The current manifest describes more than 20 million records across Apache, Linux, ZooKeeper,
Hadoop, OpenStack, OpenSSH, Blue Gene/L, HDFS, and the NASA Kennedy Space Center HTTP traces.
Six datasets exceed 100,000 records and three exceed one million.

| Dataset | Records | Logs | Upstream anomaly units |
| --- | ---: | ---: | ---: |
| Apache | 56,482 | 1 | — |
| Linux | 25,567 | 1 | — |
| ZooKeeper | 74,380 | 1 | — |
| Hadoop | 394,310 | 978 | 44 applications |
| OpenStack | 207,820 | 3 | 4 VM instances |
| OpenSSH | 655,147 | 1 | — |
| Blue Gene/L | 4,747,963 | 1 | 348,460 records |
| HDFS v1 | 11,175,629 | 1 | 16,838 blocks |
| NASA HTTP | 3,461,613 | 2 | — |
| **Total** | **20,798,911** | **989** | |

Some upstream pages report one fewer record when a final unterminated record is counted only by
newline characters. The manifest keeps both the upstream count and the exact record count produced
by line-oriented readers.

## Build the corpus

Python 3.10 or newer is sufficient. The downloader has no third-party dependencies.

```bash
python benchmarks/large_corpus.py list
python benchmarks/large_corpus.py fetch
```

The default local location is `benchmarks/corpus/local`. Set `QURTAIL_CORPUS_ROOT` or pass
`--root PATH` to use another disk:

```bash
python benchmarks/large_corpus.py --root /mnt/corpora/qurtail fetch
```

Fetch one or more datasets by stable identifier:

```bash
python benchmarks/large_corpus.py fetch loghub-apache loghub-bgl
```

Downloads are checked against the size and checksum in `manifest.json`. Archives are extracted
through path-traversal-safe readers. The command then counts every record and writes an ignored
`index.json` containing the exact local paths, bytes, and line counts used by benchmarks.

Recount the extracted data and rebuild the index without using the network:

```bash
python benchmarks/large_corpus.py verify
```

`--force` replaces only the selected archives and extracted dataset directories beneath the
chosen corpus root.

## Run proof baselines

The streaming runner compares an unfiltered view, a generic error/warning grep, and adjacent
exact-line deduplication without loading a dataset into memory:

```bash
python benchmarks/run_large_corpus.py loghub-apache loghub-bgl
```

Four datasets include upstream anomaly annotations. For those datasets the report measures
recall over stable anomaly units (BGL records, HDFS blocks, Hadoop applications, or OpenStack
instance identifiers), so a tiny output cannot look successful after discarding whole failures.

Qurtail measurement is opt-in so baseline-only scans stay inexpensive:

```bash
python benchmarks/run_large_corpus.py loghub-hdfs-v1 --limit 1000 --qurtail
```

Use `--output benchmarks/corpus/local/results.json` to retain complete machine-readable results.
The runner checks the manifest hash and each selected file's SHA-256 from `index.json`, preserving
the connection between every result and the exact source bytes.

## Benchmark contract

Large-corpus experiments should consume the files listed in the generated index rather than
discovering arbitrary files on disk. The included runner records:

- the manifest SHA-256 from the index;
- the selected dataset and file paths;
- the number of input records and bytes;
- whether the full dataset or a declared prefix was used;
- qurtail configuration when it is enabled;
- output records and bytes;
- separated baseline-scan and qurtail time; and
- retained annotated anomaly units where labels exist.

Published token-cost claims should additionally count each output with the tokenizer for the model
being evaluated. Published throughput claims should record the machine and peak memory. Byte counts
remain the dependency-free comparison available from every local run.

Completed runs must preserve upstream order. Repeating or concatenating completed logs does not
qualify as a large-corpus result. Small prefixes remain useful for smoke tests and profiler runs,
provided the report labels them as prefixes.

Static files support fast replay for correctness and throughput. A separate logical-clock replay
can use source timestamps to evaluate periodic summaries without waiting hours or days in real
time.

## Usage and redistribution

Loghub uses a research/academic-use license that requires repository attribution and citation.
The NASA archive permits redistribution while asking that analysis remain limited to general
traffic patterns; its records preserve originating hosts and requests. Our project therefore
keeps all downloaded data outside Git and distributable packages. The manifest links the exact
license or usage page for every dataset.

Review source terms again before publishing derived data or a public benchmark bundle.
