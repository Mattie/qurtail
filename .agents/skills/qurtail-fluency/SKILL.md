---
name: qurtail-fluency
description: Use qurtail for compact monitoring of noisy commands, files, containers, and Kubernetes with raw-log recovery.
---

# Qurtail Fluency

Use qurtail's built-in matching. Create no configuration.

## Choose the path

- File: `qurtail -F -n 50 app.log`
- Command: `qurtail run -- COMMAND...`
- Docker: `qurtail run -- docker logs -f api`
- Kubernetes: `qurtail run -- kubectl logs -f deploy/api --timestamps`
- Existing pipeline: `command 2>&1 | qurtail`
- Very busy stream: add `--dot-every 10`.

Skip interactive or TTY-dependent commands, byte-exact tasks, and clearly short commands.

## Interpret

The first full line is the exemplar. Dots show repetitions; the closing summary gives their exact count. Treat full warnings, errors, status changes, unfamiliar numbers, and multiline diagnostics as new evidence.

## Recover raw records

For files, inspect the original. For commands, capture combined raw output:

`qurtail run --raw-log app.raw.log -- COMMAND...`

When another process owns the pipeline, copy upstream:

`command 2>&1 | tee app.raw.log | qurtail`

Check raw output before conclusions that require a suppressed value.
