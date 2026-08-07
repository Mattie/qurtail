# SWE OpenTelemetry sync debugging

**Role:** Software engineer

**Task:** Follow flattened OpenTelemetry records while preserving the error severity transition in a repetitive background synchronization task.

**Format:** OpenTelemetry flattened log record JSON ([source](https://opentelemetry.io/docs/specs/otel/protocol/file-exporter/))

## Input

The deterministic stream contains one baseline line, 120 varying recurring lines, one context-relevant failure, and 30 more varying recurring lines. Token counts cover emitted log text only.

The grep expression and qurtail config are both tuned for this scenario. This benchmark measures emitted tokens and retained evidence on a synthetic corpus-derived stream; it does not measure live I/O performance or whether an agent completes the diagnosis.

## Qurtail config

```toml
[qurtail]
mode = "dots"
dot_every = 10
similarity = 0.90
message_field = "body.stringValue"
```

## Compared commands

- Naive tail: `tail -f <log>`
- Tail plus grep: `tail -f <log> | grep -Ei '"severityText":"(Error|Fatal)"|"severityNumber":(1[7-9]|2[0-4])|error|fatal'`
- Qurtail: `qurtail -c benchmarks/swe-opentelemetry-sync-debugging.toml -f <log>`

The benchmark runner applies the equivalent finite-stream selection and compression through Python.

## Results

Tokenizer: `tiktoken 0.13.0`, encoding `o200k_base`.

| Method | Output lines | Tokens | Token reduction vs tail | Baseline context | Recurrence visible | Failure retained |
| --- | ---: | ---: | ---: | --- | --- | --- |
| Naive tail | 152 | 6992 | 0.0% | yes | yes | yes |
| Tail plus grep | 1 | 46 | 99.3% | no | no | yes |
| Qurtail | 4 | 95 | 98.6% | yes | yes | yes |

## Outcome

Naive tail retains all evidence and spends the most context on recurring output. Tail plus grep retains the anticipated failure with the fewest tokens and drops ordinary context and recurrence evidence; qurtail retains all three measured signals while reducing emitted tokens.

Reproduce with `python benchmarks/run_benchmarks.py` after installing `benchmarks/requirements.txt`.
