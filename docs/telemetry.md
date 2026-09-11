# Local command telemetry

Qurtail can keep an opt-in local history of the command line used to invoke it and how a normal run finished. This is meant to help debug command usage. It has no network transport, and it is not an audit log.

## Configuration

Add a telemetry section to `~/.qurtail/config.toml`:

```toml
[telemetry]
mode = "full"
max_file_bytes = 5242880
max_files = 5
```

If the telemetry section is absent, or if `mode = "off"`, logging is disabled. Qurtail reads at most 1 MiB from the config before it parses command arguments. `max_file_bytes` defaults to 5 MiB. `max_files` defaults to five and counts the active log along with its numbered archives. Both limits must be positive integers.

Malformed TOML, an unsupported mode, invalid limits, an unresolved home directory, or a filesystem failure disables telemetry for that invocation. Qurtail doesn't print a warning when this happens. Other sections and fields remain available for future configuration.

The telemetry directory is `~/.qurtail/telemetry/`. On Windows, `~` is the current user's profile directory.

## What full mode records

> **WARNING:** Full mode records every command-line argument, including everything after `qurtail run --`. Those arguments can contain passwords, tokens, private URLs, and local paths. Turn full mode on only when that debugging history is worth the exposure (here, local describes transport, not sensitivity).

Telemetry does not record environment variables, standard input, command output, or the raw log stream. The logs remain local unless another program copies or uploads them.

## Lifecycle records

The active log is `commands.log`. Count-based archives are `commands.log.1`, `commands.log.2`, and so on. `commands.lock` coordinates rotation and complete event writes between qurtail processes.

Each configured invocation attempts one `START` before argument parsing. Every normal exit attempts one matching `FINISH`:

```text
2026-08-29T18:22:01.123Z format=1 event=START invocation=... tool=qurtail version=1.1.0 pid=1234 cwd="D:\\work" argv=["qurtail","app.log"] lossy=false
2026-08-29T18:22:01.135Z format=1 event=FINISH invocation=... exit=0 duration_ms=12
```

Help, version, argument errors, command errors, and successful runs all finish normally. An unexpected exception, kill, power loss, lock timeout, or failed finish write can leave an unmatched start or a missing event. `duration_ms` uses a monotonic clock. Timestamps are UTC.

Paths and arguments use JSON string escaping inside the plain-text record, which keeps control characters on one physical line. `lossy=true` marks an operating-system string that could not be represented as Unicode. The file is line-oriented text, not JSONL.

## Rotation and failure behavior

Telemetry is best effort. It never changes qurtail's stdout, stderr, exit status, or primary work. A writer waits no longer than 250 ms for another qurtail process to release `commands.lock`; if the deadline expires, that event is skipped.

Rotation happens before an event when the active file is already at or above `max_file_bytes`. Each event is appended as one complete buffer, so an individual record is never divided between files. A large record can take the active file beyond the target until the next event. Every event also removes archives outside the current `max_files` setting.

On Unix, qurtail restricts the telemetry directory to mode `0700` and its files to `0600`. Windows uses the access-control list inherited from the user's profile.

To stop future collection, remove the telemetry section or set `mode = "off"`. Existing log files remain until you remove them.
