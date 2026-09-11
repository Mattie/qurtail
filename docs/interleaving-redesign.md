# Interleaving design reset

Status: superseded proposal. The implemented design uses the existing matcher and
one shared dot/count run, with `--no-interleaving` restoring adjacent-only output.
It does not batch or reorder source records. See [the current behavior](interleaved.md).
The proposal below is retained to explain the design review, not as implementation guidance.

## Product contract

Qurtail reduces repeated log noise for humans and agents. Its display vocabulary
stays full source records, dots, and the existing exact-count summaries. Raw logs
provide details omitted by that view. Automatic interleaving support must use
the same normalization and conservative protections as adjacent repetition.

## Remove from the reference design

- Labels, repeat IDs, and the per-occurrence reference output protocol.
- Individual timestamps on compressed repeats and timestamp reconstruction.
- The separate literal matcher that bypasses established normalization and
  `--aggressive` behavior.
- Anchor allocation, lazy labeling, expiration, and re-labeling overhead.
- The character-length reference profitability check.
- Tests and release claims that treat exact event reconstruction as the goal.

The original adjacent reducer, command interfaces, optional raw capture,
diagnostic protections, signal handling, and child exit behavior remain the
baseline to preserve. Telemetry was outside this original interleaving proposal;
the combined 1.1.0 release also includes opt-in local telemetry.

## Proposed mechanism

Keep the original adjacent path unchanged. When a familiar pattern reappears
nonadjacently, collect a small batch of familiar records using the existing
signature and bounded pattern index. Retain a count, the latest actual source
record, and its arrival position for each pending pattern. Hidden records do not
create new matching patterns.

Flush all pending records before printing any unfamiliar, changed, or protected
record. Also flush on the existing one-second live-output deadline, at bounded
capacity, and on EOF or cancellation. First occurrences and unfamiliar changes
therefore continue to print immediately. Integrate this deadline with the
existing timer machinery; introduce no new mode or user setting.

At a flush, order groups by their last arrival. Print each group's latest actual
source record. For a group containing N records, that line represents one record;
the existing dots/count summary accounts for the other N−1 records. A singleton
prints normally. Honor `--dot-every`. Never synthesize a timestamp or reprint an
old initial exemplar as if it were a new occurrence.

For example, after the first peer messages have already appeared, a short batch
containing four copies from peer 2 and three from peer 3 can render as:

```text
WARN cannot connect to peer 2
... [3 similar in 1s]
WARN cannot connect to peer 3
.. [2 similar in 1s]
INFO notification timeout: 1600
```

Here the timeout is an unfamiliar change: pending repeats flush before it.
The example assumes the last peer-2 occurrence preceded the last peer-3 one.

## Explicit behavioral limits

The output syntax is unchanged, but batched counts cover earlier copies of the
displayed latest record. Original adjacent counts cover later copies of their
displayed first record. Grouping discards order among familiar repeats inside
the short batch, and a familiar transition can wait up to one second.

Flushing before every unfamiliar record prevents an older buffered healthy
message from appearing after a newly observed failure. Ordering groups by last
arrival preserves the final observed pattern in each batch. This does not claim
to reconstruct every intermediate state transition. No service, severity, or
health classifier is proposed.

This conservative boundary reduces potential savings. In an exploratory scan
of the 74,380-record ZooKeeper file using existing signatures and a 4,096-pattern
index, windows ending at an unfamiliar signature contained 48,646 familiar
records, with 9,423 records beyond one sample per pending pattern. That is only
feasibility evidence: the scan omitted elapsed-time and diagnostic boundaries
and did not measure the proposed implementation's bytes or tokens.

## Alternatives rejected during review

Shorter reference markers still require a new reading protocol and retain
per-occurrence output. Global suppression of every previously seen pattern can
hide a return to an earlier state. Exact tandem-cycle compression alone misses
much of the motivating workload, where changing timeout lines separate the same
peer warnings. Batching across unfamiliar lines risks printing stale context
after a meaningful new event; this proposal flushes before those lines.

## Validation before acceptance

- Preserve original adjacent-only output byte for byte, including live dots,
  periodic summaries, EOF, interruption, and `--dot-every`.
- Exercise irregular interleaving with changing timeouts, peer identities,
  normalized request IDs, JSON metadata, and `--aggressive`.
- Verify every source record is either printed or included in exactly one
  count. Do not require omitted per-record timestamps to be reconstructible.
- Check both new and previously seen failure/recovery patterns, final state,
  protected diagnostics, and pending healthy records before a new failure.
- Verify singleton groups, capacity flushes, quiet-stream deadlines, source
  order at unfamiliar boundaries, and bounded memory.
- Replay the external and original corpora; measure tokens, byte expansion,
  output latency, and raw-retrieval cost. Do not inherit the reference design's
  performance claims or its compression target without new evidence.

An independent product review found this a coherent conservative candidate.
Its delayed familiar records and grouped chronology require validation before
implementation results can be described as a successful replacement.
