# Interleaved repetition

Qurtail uses the same matcher and dots/counts for consecutive and interleaved
repetition. The first example of a pattern prints in full. Later familiar
occurrences contribute to one repeat count even when their patterns alternate.

For input `A B A B A`, the output is:

```text
A
B
... [3 similar before stop]
```

The count is the total number of suppressed records across familiar patterns.
It does not identify which pattern each dot represents. Qurtail introduces no
labels, references, timestamps, delayed groups, or replayed examples.

## Turn interleaving off

If the compact output is confusing, or a return to an earlier pattern matters,
use `--no-interleaving`:

```bash
qurtail -F --no-interleaving app.log
qurtail run --no-interleaving -- COMMAND...
producer 2>&1 | qurtail --no-interleaving
```

This restores the original consecutive-only behavior. A pattern returning after
another pattern prints fully; its following consecutive copies still become dots.
For `healthy, failed, healthy, healthy`, default output is:

```text
healthy
failed
.. [2 similar before stop]
```

With `--no-interleaving` it is:

```text
healthy
failed
healthy
. [1 similar before stop]
```

## Matching and streaming

Both modes use the existing normalization for timestamps, IDs, and structured
metadata. `--aggressive` applies to both. Unfamiliar patterns and significant
values print immediately, after any pending repeat count. Protected diagnostics
retain the original handling. The existing bounded pattern index holds only
patterns learned from full records; suppressed copies do not extend its lifetime.

Dots use the existing buffering and live timer. `--dot-every` controls dot density;
the closing count remains exact. Counts close before a full record, at the summary
interval, and on shutdown. Source records are never buffered for later grouping
or reordered. A full source line retains its original timestamp if it has one.

## Recover exact details

Dots omit the identities, order, timestamps, and normalized values of familiar
occurrences. When those details matter, inspect the source file or capture raw
command output:

```bash
qurtail run --raw-log app.raw.log -- COMMAND...
producer 2>&1 | tee app.raw.log | qurtail
```

The opt-out still summarizes consecutive repetition. Raw output is the source
for an exact timeline or a value omitted by normalization.

See [external validation](external-validation.md) and the
[benchmark inventory](../benchmarks/README.md) for measured savings and limits.
