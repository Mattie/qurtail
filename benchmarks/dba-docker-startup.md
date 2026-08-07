# DBA container database startup diagnosis

**Role:** Database administrator

**Task:** Follow container health traffic while retaining the refused database connection that explains why an application replica cannot start.

**Format:** Docker json-file record ([source](https://docs.docker.com/engine/logging/drivers/json-file/))

## Input

The deterministic stream contains one baseline line, 120 varying recurring lines, one context-relevant failure, and 30 more varying recurring lines. Token counts cover emitted log text only.

The grep expression and qurtail config are both tuned for this scenario. This benchmark measures emitted tokens and retained evidence on a synthetic corpus-derived stream; it does not measure live I/O performance or whether an agent completes the diagnosis.

## Qurtail config

```toml
[qurtail]
mode = "dots"
dot_every = 10
similarity = 0.90
message_field = "log"
```

## Compared commands

- Naive tail: `tail -f <log>`
- Tail plus grep: `tail -f <log> | grep -Ei '"stream":"stderr"|error|fail|panic|refused'`
- Qurtail: `qurtail -c benchmarks/dba-docker-startup.toml -f <log>`

The benchmark runner applies the equivalent finite-stream selection and compression through Python.

## Results

Tokenizer: `tiktoken 0.13.0`, encoding `o200k_base`.

| Method | Output lines | Tokens | Token reduction vs tail | Baseline context | Recurrence visible | Failure retained |
| --- | ---: | ---: | ---: | --- | --- | --- |
| Naive tail | 152 | 5774 | 0.0% | yes | yes | yes |
| Tail plus grep | 1 | 36 | 99.4% | no | no | yes |
| Qurtail | 4 | 77 | 98.7% | yes | yes | yes |

## Outcome

Naive tail retains all evidence and spends the most context on recurring output. Tail plus grep retains the anticipated failure with the fewest tokens and drops ordinary context and recurrence evidence; qurtail retains all three measured signals while reducing emitted tokens.

Reproduce with `python benchmarks/run_benchmarks.py` after installing `benchmarks/requirements.txt`.
