# qurtail

*Quell Unwanted Repetition with `qurtail`*

----

`qurtail` is a small stream compactor built for coding agents. It is the normal way for an agent
to follow a noisy log or run a command that is expected to produce long, repetitive output.

It keeps the first example of each log pattern, recognizes common values that change on every
line, and replaces later repetitions with buffered dot runs and compact closing counts. Warnings,
errors, status changes, unfamiliar numbers, and multiline diagnostics stay visible.

The result is a much smaller stream for the agent to read, with enough context to understand
what repeated and how often.

## Product Boundary

Qurtail is a local streaming view over a file, standard input, or a child command. It does not
store logs unless raw capture is explicitly requested, query history, diagnose failures, manage
remote sources, alert people, send telemetry, call a model, or provide an observability service.
The original file, captured raw output, or upstream stream remains the source of truth.

The common path requires no project configuration.

## Usage and Installation

Run qurtail without installing it permanently:

```bash
uvx qurtail -F -n 50 app.log
```

Or install it as a standalone command:

```bash
uv tool install qurtail
```

```bash
pipx install qurtail
```

Qurtail requires Python 3.11 or newer and has no runtime dependencies.

Follow a file with its last 50 lines of context:

```bash
qurtail -F -n 50 app.log
```

`-f` and `-F` both keep following through truncation, replacement, and log rotation. `-n`
controls how many existing lines are read when the file is opened. The default is 10, matching
`tail`.

When the agent launches a noisy command, use qurtail's command runner:

```bash
qurtail run -- pytest -q
```

Qurtail streams the child command's combined standard output and standard error through the same
conservative reducer. Arguments after `--` are passed to the command without implicit shell
interpretation. Interrupting qurtail interrupts and reaps the child process, and qurtail returns
the child's success or failure status so a failed test, build, or installer cannot look successful
merely because the compacting process exited normally.

Use the runner when qurtail launches a container or cluster log command:

```bash
qurtail run -- docker logs -f api
```

```bash
qurtail run -- kubectl logs -f deploy/api --timestamps
```

Qurtail can still read a live stream from standard input when another process owns the pipeline:

```bash
producer 2>&1 | qurtail
```

Without `-f` or `-F`, a file is read once and qurtail exits. With no file argument or `run`
subcommand, qurtail reads standard input until the upstream stream closes.

Run `qurtail -h` for the complete command reference.

Each suppressed record produces one dot by default. For very busy sources, `--dot-every N`
produces one dot for every N suppressed records:

```bash
qurtail -F --dot-every 10 app.log
```

### What the output looks like

```text
> qurtail -F -n 50 app.log
2026-08-08T12:00:00Z INFO refreshed cache request_id=715a
........ [8 similar in 1s]
2026-08-08T12:00:09Z ERROR cache refresh failed: connection refused
..... [5 similar before stop]
```

The first line from a pattern is always printed in full. Dots are the live activity signal while
similar records continue. Qurtail buffers them into short runs before writing, which reduces agent
output overhead without making an active stream look stalled. When the pattern changes, 30 seconds
pass, or monitoring stops, qurtail closes the run with the exact repeat count and a newline.
Summaries never use backspaces or carriage-return updates.

## How Matching Works

Qurtail uses one conservative streaming signature reducer with bounded per-stream state. It builds
an indexed signature for each visible pattern and recognizes a small set of corpus-proven volatile
values such as:

- timestamps;
- UUIDs;
- request, trace, and span identifiers;
- standard JSON log metadata; and
- stable container or service prefixes.

Unknown changing values remain visible. Qurtail does not use a broad fuzzy-similarity threshold,
so a change such as `replication lag is 1 second` to `replication lag is 900 seconds` is printed in
full. Unknown shapes, ambiguous values, malformed records, and uncertain multiline continuations
also remain visible.

The matcher also keeps these records intact:

- warning, error, and fatal transitions;
- HTTP status changes;
- changed structured error payloads;
- same-level errors with different details; and
- traceback, stack trace, and other multiline diagnostic blocks.

A suppressed record increments the count for its visible pattern. Repeated identical errors may be
summarized after one complete exemplar; changed errors and their complete multiline diagnostics
remain visible. A suppressed record cannot become a hidden example that causes another line to
disappear later.

## Raw Log Recovery

Qurtail is a compact monitoring view. The followed file remains the source of truth whenever the
agent needs the exact suppressed records. For child commands, request a raw transcript explicitly:

```bash
qurtail run --raw-log api.raw.log -- docker logs -f api
```

For stdin-only sources, keep raw output upstream when recovery matters. For example:

```bash
docker logs -f api 2>&1 | tee api.raw.log | qurtail
```

Qurtail does not create its own transcript or retain a second copy of the stream unless
`--raw-log` is supplied. Before drawing a conclusion that depends on a suppressed value, the agent
consults the original file or captured raw output.

## Benchmarks

The benchmark suite includes regression fixtures, held-out monitoring episodes, and large-corpus runs. 

The suite runs installed-command smoke tests on Linux, macOS, and Windows. See more in
`benchmarks/README.md`.

## Skills

The included `qurtail-fluency` agent skill makes qurtail the default for verbose tests, builds,
installers, development servers, services, container and Kubernetes workloads, and followed logs.

## Changelog

TODO

## License

MIT License
