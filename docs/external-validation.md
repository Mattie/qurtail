# External corpus validation

The shared dots/counts implementation reduces output tokens on all three external
corpus families. `--no-interleaving` produces byte-identical output to the retained
pre-feature reducer on all 31 inputs (1,787,626 physical records).

The default hides individual familiar occurrences, including returns to an earlier
state. Its smaller output does not establish lower total agent cost or preserve an
exact event timeline. See [interleaving behavior](interleaved.md) for the opt-out
and raw-log recovery.

## Complete corpus results

These are weighted totals using `o200k_base` tokens per LF-terminated output
record. The baseline is the retained pre-feature reducer, including local changes
already present before this work. The adjacent-only current view matches that
baseline on every case.

| Corpus | Cases | Records | Raw tokens | Baseline tokens | Default tokens | Reduction vs baseline |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| LogDx v2 | 19 | 255,283 | 12,578,743 | 12,534,616 | 10,788,586 | 13.93% |
| Rootly | 3 | 62,959 | 2,862,487 | 2,831,241 | 2,613,440 | 7.69% |
| RCAEval | 9 | 1,469,384 | 137,503,087 | 135,863,129 | 122,264,185 | 10.01% |

The earlier labeled-reference experiment added 484,346 RCAEval tokens against
baseline. The current implementation removes that protocol and saves 13,598,944
tokens against baseline. The older reports and cost decomposition are preserved
under [historical reference-design evidence](../benchmarks/history/reference-design/README.md)
and are excluded from current validation.

Per-case measurements, source hashes, output hashes, and all three views are in
[`external-validation-results.json`](../benchmarks/external-validation-results.json).

## What survived

All 93 transcript audits passed. The independent audit aligns full records and
repeat counts with the source, requires each suppressed record to match an
already printed normalized pattern, and accounts for every input record. In
adjacent-only mode it also requires consecutive matching. Cache eviction and
protected-diagnostic behavior are covered by focused tests; this offline audit
does not independently reproduce that state. Counts do not reconstruct hidden
identities, timestamps, values, or ordering.

Across LogDx, all **61 critical primary signal strings present in raw input**
remain present in full output records of both the baseline and default. Eighteen
primary signal annotations are absent from the normalized raw logs and receive
no retention credit. Alias matches are recorded separately.

Individual occurrence visibility falls substantially: **1,225 of 3,633 annotated
source-record positions** print in full with the default, compared with **3,627**
under the baseline and opt-out. The remaining positions are summarized as dots
and counts. Retaining a signal somewhere in the output does not demonstrate its
frequency, recurrence, final state, or correct incident diagnosis.

Rootly has no diagnostic answer key in this evaluation. RCAEval fault labels alone
do not establish what an agent can infer from logs without metrics or traces.
The held-out recovery episode explicitly records that default dots hide the
return to a familiar healthy pattern; `--no-interleaving` restores that occurrence.

## Agent evidence

Six fresh agents evaluated three paired synthetic tasks against the current
dots/counts implementation. Every requested field matched the answer key, and
manual review confirmed support in the files read. The scorer checks read
integrity and answer-key equality; it does not assess evidence grounding.
All three compact arms retrieved raw evidence. Total task/evidence reads cost
6,651 tokens for raw views and 7,106 for compact views, **6.84% more**. This small
trial demonstrates usable raw recovery, without establishing general agent-cost
savings. The [benchmark inventory](../benchmarks/README.md) gives per-task results.

The earlier nine-agent external trial tested the rejected reference design. Its
receipts remain in the historical directory and provide no current agent result.
That trial also had unresolved hidden timestamps in two RCAEval answers and
truncated delivery in all three Rootly runs.

## Sources and reproduction

The selection was fixed before compression measurement:

- [LogDx](https://github.com/eyuansu62/LogDx/tree/fc957f0d0d0082019606cc20b8fb545683a03b44):
  all 19 `cases/v2` cases, CC BY 4.0 data.
- [Rootly AI Labs logs dataset](https://github.com/Rootly-AI-Labs/logs-dataset/tree/5d7448debdf22ad27358fdcc62fc36205496e3f8):
  all three log files, Apache 2.0.
- [RCAEval](https://huggingface.co/datasets/phamquiluan/RCAEval/tree/afeacb11bcc94dadfd1c8f483ee4377b2b8b614e):
  repetition 1 of CPU, delay, and socket faults for `re2ob_checkoutservice`,
  `re2ss_carts`, and `re2tt_ts-order-service`, MIT.

The manifest binds 104 retained source, label, and provenance files to immutable
revisions and SHA-256 hashes. Downloads also verify Git blob or LFS hashes.
Third-party logs remain under the ignored local corpus directory.

Text logs are decoded as UTF-8 with replacement and normalized to LF while
preserving stored order. RCAEval Parquet rows become an adapted text view with
UTC timestamp, JSON-quoted container, and original message. Embedded message
newlines become physical records; null messages become explicit boundary records.
The report records null counts and backwards timestamp steps. This formatting
influences compression. The constant replay clock and `dot_every=10` also differ
from some live polling conditions.

```bash
python -m pip install -r benchmarks/external-requirements.txt
python benchmarks/fetch_external_validation.py --root benchmarks/corpus/local/external-validation --manifest benchmarks/external-corpus-manifest.json
python benchmarks/validate_external_logs.py --root benchmarks/corpus/local/external-validation --manifest benchmarks/external-corpus-manifest.json --baseline-source PATH_TO_PRE_FEATURE_QURTAIL --output benchmarks/external-validation-results.json
```

The baseline hash is recorded in the report. Its local snapshot contains
pre-existing uncommitted work and is not distributed, so an external checkout
cannot reproduce that historical baseline without the snapshot. The external
validator requires this baseline module to replay its three views.
Agent fixture generation and scoring tools remain available for new experiments;
fresh agents and their original reader ledgers are required. Do not regenerate
fixtures over an active evaluation.
