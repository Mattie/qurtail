# SWE test teardown debugging

**Role:** Software engineer

**Task:** Follow a noisy pytest run long enough to retain ordinary test progress and notice a teardown failure that can leave later tests contaminated.

**Format:** pytest live logging ([source](https://docs.pytest.org/en/stable/how-to/logging.html))

## Input

The deterministic stream contains one baseline line, 120 varying recurring lines, one context-relevant failure, and 30 more varying recurring lines. Token counts cover emitted log text only.

The grep expression and qurtail config are both tuned for this scenario. This benchmark measures emitted tokens and retained evidence on a synthetic corpus-derived stream; it does not measure live I/O performance or whether an agent completes the diagnosis.

## Qurtail config

```toml
[qurtail]
mode = "dots"
dot_every = 10
similarity = 0.90
ignore_levels = false
```

## Compared commands

- Naive tail: `tail -f <log>`
- Tail plus grep: `tail -f <log> | grep -Ei 'ERROR|CRITICAL|FAILED|Traceback'`
- Qurtail: `qurtail -c benchmarks/swe-test-debugging.toml -f <log>`

The benchmark runner applies the equivalent finite-stream selection and compression through Python.

## Results

Tokenizer: `tiktoken 0.13.0`, encoding `o200k_base`.

| Method | Output lines | Tokens | Token reduction vs tail | Baseline context | Recurrence visible | Failure retained |
| --- | ---: | ---: | ---: | --- | --- | --- |
| Naive tail | 152 | 1675 | 0.0% | yes | yes | yes |
| Tail plus grep | 1 | 14 | 99.2% | no | no | yes |
| Qurtail | 4 | 28 | 98.3% | yes | yes | yes |

## Outcome

Naive tail retains all evidence and spends the most context on recurring output. Tail plus grep retains the anticipated failure with the fewest tokens and drops ordinary context and recurrence evidence; qurtail retains all three measured signals while reducing emitted tokens.

Reproduce with `python benchmarks/run_benchmarks.py` after installing `benchmarks/requirements.txt`.
