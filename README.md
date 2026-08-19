# qurtail can Quell Unwanted Repetition

Long-running logs have a bad habit of saying the same thing thousands of times with a new
timestamp attached. That is tolerable when you're watching a terminal. It gets expensive and
fairly useless when a coding agent has to read the whole thing.

`qurtail` gives you or your agent a smaller view of the stream. It prints the first example of a repeated pattern, shows dots while more copies arrive, and closes the run with an exact count. Warnings, errors, status changes, unfamiliar numbers, and multiline diagnostics stay visible.

```text
> qurtail -F -n 50 app.log
2026-08-08T12:00:00Z INFO refreshed cache request_id=715a
........ [8 similar in 1s]
2026-08-08T12:00:09Z ERROR cache refresh failed: connection refused
..... [5 similar before stop]
```

A silent monitor is hard to distinguish from a stuck one. Qurtail provides the dots by default as a small sign of life, but you can change that with `--dot-every`.

## Install it

Qurtail requires Python 3.11 or newer. It has no runtime dependencies.


To install from PyPI:
```bash
uv tool install qurtail
# or
pipx install qurtail
```

To install from GitHub:
```bash
git clone https://github.com/Mattie/qurtail.git
cd qurtail
uv tool install .
```

Or use `pipx` after cloning:

```bash
pipx install .
```

To run the repository once without installing it:

```bash
uvx --from . qurtail -F -n 50 app.log
```

## Follow along!

```bash
qurtail -F -n 50 app.log
```

`-n` is the number of existing lines to read when the file opens. The default is 10, like `tail`.
Both `-f` and `-F` continue following when the file is truncated, replaced, or rotated.

Leave off the follow flag when you want to compact a file once and exit:

```bash
qurtail app.log
```

For a particularly busy log, print one dot for every ten suppressed records:

```bash
qurtail -F --dot-every 10 app.log
```

Every suppressed record produces one dot by default. Qurtail buffers those dots into short runs
before writing them, then closes the run with the exact repeat count and a newline when the pattern
changes, 30 seconds pass, or monitoring stops. It doesn't redraw old terminal lines with
backspaces or carriage returns, so captured output stays readable too.

## Run a command

If qurtail is launching the noisy command, use `run`:

```bash
qurtail run -- pytest -q
```

Everything after `--` is passed directly to the child command. There is no implicit shell in the
middle interpreting pipes, substitutions, or redirects.

Standard output and standard error are combined and sent through the same conservative reducer.
Qurtail returns the child's success or failure status. If you interrupt qurtail, it interrupts and
reaps the child process before exiting. A failed test run should still look like a failed test run;
saving screen space is no excuse for losing the exit code.

The same form works for container logs:

```bash
qurtail run -- docker logs -f api
```

```bash
qurtail run -- kubectl logs -f deploy/api --timestamps
```

When another command already owns the pipeline, qurtail can read standard input:

```bash
producer 2>&1 | qurtail
```

With no filename and no `run` subcommand, it reads until standard input closes.

Run `qurtail -h` for the complete command reference.

## What counts as repetition?

Qurtail uses bounded per-stream state to index patterns it has already printed. It recognizes a
small, corpus-proven set of values that commonly change without changing the meaning of a log
line:

- timestamps
- UUIDs
- request, trace, and span IDs
- standard JSON log metadata
- stable container and service prefixes

The matcher errs on the side of printing a line. It doesn't use a broad fuzzy-similarity score,
since that is a good way to make an important number disappear. These two lines are different and
both remain visible:

```text
replication lag is 1 second
replication lag is 900 seconds
```

Unknown shapes, ambiguous values, and unfamiliar changing values also print in full. The same goes
for malformed structured records and multiline content qurtail isn't sure how to join.

Warning, error, and fatal transitions stay visible, along with HTTP status changes and changed
structured error payloads. Same-level errors with different details get their own full record.
Tracebacks, stack traces, and other multiline diagnostic blocks stay together. Repeated identical
errors may be summarized after one complete example.

Every suppressed record increments the count for its visible pattern. Suppressed records don't
teach the matcher new patterns, so a hidden record cannot become the hidden example that makes some
later line disappear.

### Optional aggressive matching

Conservative matching remains the default. When changing values still make repetitive output look
unique, `--aggressive` also treats these hexadecimal, path, and long-integer values as noise.

Keep in mind that sometimes ports, years, durations, byte counts, identifiers, and affected paths can all matter. Only use `--aggressive` when those values are noise.

## Keep the raw output if you'll need it

Qurtail is just a viewing helper to reduce noise. You may still want the followed stream captured.

When qurtail runs the child command, use `--raw-log` to keep the raw text:

```bash
qurtail run --raw-log api.raw.log -- docker logs -f api
```

(The raw-log path must be new. To deliberately replace an existing log, add `--overwrite`.)

For a pipe, keep the raw copy before the stream reaches qurtail:

```bash
docker logs -f api 2>&1 | tee api.raw.log | qurtail
```

Without `--raw-log`, qurtail doesn't create a transcript or keep a second copy.

## What qurtail doesn't do

Qurtail compacts a live local stream while it passes through. It doesn't store logs unless you ask.

## Benchmarks and tests

The benchmark suite includes regression fixtures, held-out monitoring episodes, and large-corpus
runs. Installed-command smoke tests run on Linux, macOS, and Windows.

See [`benchmarks/README.md`](benchmarks/README.md) for more. If you have good log data you want to share, please open an issue or pull request. The more diverse the corpus, the better qurtail can be updated to recognize repetition.

## Agent skill

The included `qurtail-fluency` skill makes qurtail the default for verbose tests, builds,
installers, development servers, services, container and Kubernetes workloads, and followed logs.

## Changelog

### 1.0.0

- Added opt-in aggressive matching for long integers, prefixed hexadecimal values, and absolute
  paths.
- Protected existing raw transcripts by default and added an explicit overwrite option.
- Kept corpus-integrity verification byte-exact across Linux, macOS, and Windows.
- Included the complete MIT license in source and built distributions.

## License

[MIT License](LICENSE.md)
