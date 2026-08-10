# qurtail to Quell Unwanted Repetition

Long-running logs have a bad habit of saying the same thing thousands of times with a new
timestamp attached. That is tolerable when you're watching a terminal. It gets expensive and
fairly useless when a coding agent has to read the whole thing.

`qurtail` gives the agent a smaller view of the stream. It prints the first example of a repeated
pattern, shows dots while more copies arrive, and closes the run with an exact count. Warnings,
errors, status changes, unfamiliar numbers, and multiline diagnostics stay visible.

```text
> qurtail -F -n 50 app.log
2026-08-08T12:00:00Z INFO refreshed cache request_id=715a
........ [8 similar in 1s]
2026-08-08T12:00:09Z ERROR cache refresh failed: connection refused
..... [5 similar before stop]
```

A silent monitor is hard to distinguish from a stuck one. Qurtail leaves the dots as a small sign
of life while keeping the output manageable.

## Install it

Qurtail requires Python 3.11 or newer. It has no runtime dependencies.

You can run it once with `uvx`:

```bash
uvx qurtail -F -n 50 app.log
```

Or install it as a command:

```bash
uv tool install qurtail
```

```bash
pipx install qurtail
```

## Follow a file

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

## Keep the raw output when you'll need it

Qurtail is a viewing tool. The followed file, captured raw output, or upstream stream remains the
source of truth.

When qurtail runs the child command, `--raw-log` keeps a raw transcript:

```bash
qurtail run --raw-log api.raw.log -- docker logs -f api
```

For a pipeline, keep the raw copy before the stream reaches qurtail:

```bash
docker logs -f api 2>&1 | tee api.raw.log | qurtail
```

Without `--raw-log`, qurtail doesn't create a transcript or keep a second copy. If your conclusion
depends on one of the values that was summarized, check the original file or the captured raw log.
The compact view tells you what repeated and how often. It cannot recover bytes you chose not to
save.

## What qurtail doesn't do

Qurtail compacts a live local stream while it passes through. It doesn't store logs unless you ask
for raw capture, query history, diagnose failures, manage remote sources, alert people, send
telemetry, call a model, or provide an observability service. The common path requires no project
configuration.

## Benchmarks and tests

The benchmark suite includes regression fixtures, held-out monitoring episodes, and large-corpus
runs. Installed-command smoke tests run on Linux, macOS, and Windows.

See [`benchmarks/README.md`](benchmarks/README.md) for more.

## Agent skill

The included `qurtail-fluency` skill makes qurtail the default for verbose tests, builds,
installers, development servers, services, container and Kubernetes workloads, and followed logs.

## Changelog

TODO

## License

MIT License
