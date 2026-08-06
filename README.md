# smartytail

`smartytail` is a very simple version of `tail` for noisy logs.

It watches a stream of lines and compares each new line to the recent lines it has already seen.
If a new line is very similar to an earlier one, it suppresses that full line and prints a short
marker such as `.` instead, without adding a newline. That keeps repeated chatter visible without
letting it flood the terminal.

When the line is meaningfully different, `smartytail` prints the full line normally.

Example:

```text
starting worker 17
.....
connection reset by peer
..............................
finished batch 42
```

The goal is to preserve signal while compressing repetition.
