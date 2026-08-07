---
name: qurtail-fluency
description: Use qurtail to compact noisy development logs and long-running command output for coding agents while keeping distinct failures visible. Use when Codex, Claude Code, ChatGPT, or another agent needs to follow application, test, web-server, container, operating-system, JSONL, or OpenTelemetry output with lower token use.
---

# Qurtail Fluency

Keep enough output to notice failures while preventing recurring development logs from consuming context.

## Choose the stream

- Follow a file with `qurtail -f app.log`.
- Wrap a test, build, or server command with `command 2>&1 | qurtail`.
- Follow a container with `docker logs -f api 2>&1 | qurtail`.
- Follow a Kubernetes workload with `kubectl logs -f deploy/api --timestamps 2>&1 | qurtail`.

Prefer `mode = dots` for output another agent will read. Use `dot_every` to reduce marker volume.

## Start conservative

Use a configuration like this:

```toml
[qurtail]
mode = dots
dot_every = 10
similarity = 0.90
ignore_timestamps = yes
ignore_levels = no
rotate_sample = 50
```

Keep `rotate_sample` so recurring traffic is periodically shown in full. Lower `similarity` only after checking that distinct failures remain visible.

## Match common formats

For timestamped text from Python, pytest, Log4j, .NET, journalctl, or macOS unified logging, start with `ignore_timestamps = yes`. Leave `ignore_levels = no` when a severity change should force a full line, and add `ignore_prefixes` for stable wrappers such as logger names or CRI stream markers.

Qurtail keeps transitions between normal, warning, and error severity classes visible; repeated lines within the new class can still collapse. Set `ignore_levels = yes` only when changing levels are known noise and failures have distinct message text.

For newline-delimited JSON, compare the stable message body:

```toml
[qurtail]
message_field = msg
```

Use `msg` for Pino, `Message` for .NET JSON or projected Windows events, and `log` for saved Docker json-file records. For a flattened OpenTelemetry record, use `body` when it is a string or a dotted path such as `body.stringValue` when it carries a typed value.

Canonical OTLP file output wraps log records in `resourceLogs` and `scopeLogs`, so flatten each JSONL envelope before comparison:

```bash
jq -c '.resourceLogs[].scopeLogs[].logRecords[]' otel.jsonl |
  qurtail --message-field body.stringValue
```

If no single message field is reliable, list volatile top-level fields with `ignore_fields`.

For copied Kubernetes CRI records, remove the stream wrapper:

```toml
[qurtail]
ignore_timestamps = yes
ignore_prefixes = stdout F, stderr F
```

Use a high threshold such as `similarity = 0.95` for fixed-width web access logs, then verify that error responses remain visible.

## Read operating-system logs

- Linux: `journalctl -fu my.service --no-pager | qurtail --ignore-timestamps`
- macOS: `log stream --style compact 2>&1 | qurtail --ignore-timestamps`
- Windows: project each event to one JSON line and use `--message-field Message`.

```powershell
Get-WinEvent -LogName Application -MaxEvents 100 |
  ForEach-Object {
    $_ | Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message |
      ConvertTo-Json -Compress
  } | qurtail --message-field Message
```

## Preserve useful evidence

- Put representative recurring lines in `comparison_file`; keep errors and exceptional output out of it.
- Keep redirected output in dots mode; spinner backspaces are meant for an interactive terminal.
- Inspect unsuppressed samples before tightening similarity or increasing `dot_every`.
- Report the exact qurtail command and rc settings when sharing filtered diagnostics.
