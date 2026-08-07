# DBA Python database lock diagnosis

**Role:** Database administrator

**Task:** Follow repetitive Python worker progress while retaining the teardown failure that reports a locked database.

**Format:** Python logging text ([source](https://docs.python.org/3/howto/logging.html))

## Input

The deterministic stream contains one baseline line, 120 varying recurring lines, one context-relevant failure, and 30 more varying recurring lines. Token counts cover emitted log text only.

The grep expression and qurtail config are both tuned for this scenario. This benchmark measures emitted tokens and retained evidence on a synthetic corpus-derived stream; it does not measure live I/O performance or whether an agent completes the diagnosis.

## Qurtail config

```toml
[qurtail]
mode = "dots"
dot_every = 10
similarity = 0.90
ignore_timestamps = true
```

## Compared commands

- Naive tail: `tail -f <log>`
- Tail plus grep: `tail -f <log> | grep -Ei 'ERROR|CRITICAL|database is locked|deadlock|timeout'`
- Qurtail: `qurtail -c benchmarks/dba-python-lock-diagnosis.toml -f <log>`

The benchmark runner applies the equivalent finite-stream selection and compression through Python.

## Results

Tokenizer: `tiktoken 0.13.0`, encoding `o200k_base`.

| Method | Output lines | Tokens | Token reduction vs tail | Baseline context | Recurrence visible | Failure retained |
| --- | ---: | ---: | ---: | --- | --- | --- |
| Naive tail | 152 | 3194 | 0.0% | yes | yes | yes |
| Tail plus grep | 1 | 23 | 99.3% | no | no | yes |
| Qurtail | 4 | 47 | 98.5% | yes | yes | yes |

## Outcome

Naive tail retains all evidence and spends the most context on recurring output. Tail plus grep retains the anticipated failure with the fewest tokens and drops ordinary context and recurrence evidence; qurtail retains all three measured signals while reducing emitted tokens.

Reproduce with `python benchmarks/run_benchmarks.py` after installing `benchmarks/requirements.txt`.
