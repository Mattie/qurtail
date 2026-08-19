# Potential improvements

Last reviewed: 2026-08-19

This page records ideas worth revisiting and the reason each one is still waiting.

## Repeated multiline blocks

Qurtail works line by line. That suits heartbeats, polling messages, and records that keep the same
shape while a few values change. It misses a different kind of repetition: a whole block that keeps
coming back.

Stack traces make this easy to see. The same trace can appear forty times, with every frame
separated by the other frames in the trace. Test runners repeat wrappers and summaries in much the
same way. The individual lines rarely arrive next to an identical copy, so qurtail prints them all.

A small exact-block prototype could tell us whether this is worth building. The first block would
print in full. Later copies could collapse to a count. A block with one changed frame, message, or
assertion would print in full again. If we cannot keep that rule easy to explain, the idea is
probably a bad fit for qurtail.

### What the LogDx development cases showed

We ran the three canonical `v2/dev` cases from LogDx at commit
[`fc957f0d0d0082019606cc20b8fb545683a03b44`](https://github.com/eyuansu62/LogDx/tree/fc957f0d0d0082019606cc20b8fb545683a03b44).
Each replay read the complete file. Signal checks used the case's required values, aliases, and file
fallback, following LogDx's static scoring rules. The qurtail working tree was based on commit
`51d3d1375e479a55a073db5249b1bfd669c1bdbd` and contained uncommitted changes. The measured
`qurtail.py` had SHA-256
`1b165afe3ea936fb67aaec89b4ebaee095b0f7a672e1659026359e5ce3507e12`.

| Case | Lines | Bytes | Conservative reduction | Aggressive reduction | Required signals retained |
| --- | ---: | ---: | ---: | ---: | ---: |
| [Moby BuildKit](https://github.com/eyuansu62/LogDx/tree/fc957f0d0d0082019606cc20b8fb545683a03b44/cases/v2/dev/moby-buildx-bake-v2-001) | 3,979 | 447,387 | 0.23% | 0.48% | 6/6 |
| [pip and pytest](https://github.com/eyuansu62/LogDx/tree/fc957f0d0d0082019606cc20b8fb545683a03b44/cases/v2/dev/pip-pytest-network-github-v2-001) | 6,669 | 897,296 | 0.05% | 0.05% | 7/7 |
| [pnpm and Jest](https://github.com/eyuansu62/LogDx/tree/fc957f0d0d0082019606cc20b8fb545683a03b44/cases/v2/dev/pnpm-jest-config-v2-001) | 3,126 | 304,326 | 0.46% | 0.82% | 5/5 |

These logs are a lousy target for tuning the current line matcher. Qurtail kept every required
signal and passed through almost everything else too. Aggressive mode barely moved the numbers.

Moby and pnpm still expose the block-repetition gap. Once the leading GitHub timestamp is removed,
Moby has 1,941 duplicate payload instances and pnpm has 1,040. Many belong to stack traces or
test-runner sections that repeat as blocks. The pip log is different. It has 189 duplicate
instances across 6,669 lines, so there is little compression to find there.

LogDx marks evidence that must survive. It does not say which other lines are safe to throw away.
That is a serious limit for tuning. We could produce an impressively short result that passes every
label while leaving a human or agent without enough context to understand the failure.

These processed captures do not reproduce a live pipe faithfully. GitHub job and step prefixes
were removed. At least one case stores terminal escapes as literal caret notation. The files have
no write or flush boundaries, and the original stdout/stderr split is gone. They are poor LIVE I/O
fixtures.

The LogDx case data is covered by
[CC-BY-4.0](https://github.com/eyuansu62/LogDx/blob/fc957f0d0d0082019606cc20b8fb545683a03b44/LICENSE-DATA).
Any local benchmark that uses it needs the required attribution and a pinned copy or verified
download. Qurtail's committed test fixtures should remain independently written.

### The question worth testing

Can qurtail compress an exact repeated multiline block without delaying live output enough to be
annoying or hiding a block that changed in one important place?

An offline deduper would dodge the hard part. Qurtail has to show useful output while the process is
still running. Any block matcher that waits too long has failed, even if its final output looks
great.

### Start with our own streams

The first cases should be small enough to understand by eye:

- A complete stack trace repeated several times.
- A second trace with one changed exception message or frame.
- Repeated Jest-style result blocks with different test names and assertion values.
- A long block that exceeds the allowed buffer and must pass through unchanged.
- A partial block followed by EOF or an interrupted child process.

Feed those streams through stdin and `qurtail run`. Control the child with a handshake so the test
can prove that output arrives before exit. Keep an exact raw transcript and compare it byte for
byte.

For the first pass, match exact block content after qurtail's existing conservative timestamp and
ID normalization. Fuzzy matching would make the result hard to trust. A changed exception,
severity, assertion value, path, or frame must make the block visible.

The limits matter as much as the matching:

- Cap the number of buffered bytes and lines.
- Cap how long an unfinished block may delay output.
- Pass the buffered text through unchanged when the boundary is unclear.
- Keep only a bounded number of block signatures.
- Leave raw capture completely independent of suppression.

Start in benchmark code. A CLI option can wait until the behavior has proved useful.

### External pressure check

Once the owned cases behave, replay the Moby and pnpm LogDx logs through the prototype. Every
critical signal must survive. Someone should also read the output, because the labels only cover
evidence the dataset already knows about.

Measure:

- Bytes, lines, and tokens emitted.
- Required and critical signals retained.
- Time to first output and the longest block-induced delay.
- Peak memory used by block state.
- Whether a one-line block mutation remains visible.
- Raw transcript equality.

The experiment has to earn the buffering it adds. Less than 10% reduction on both Moby and pnpm is
probably too small to bother with. Stop if it loses a critical signal, hides a one-line mutation,
lets memory grow without a firm bound, or makes live output feel late.

### Questions that remain

- Which boundaries are dependable across tracebacks, Java stacks, and test-runner sections?
- Should a repeated block be exact after conservative normalization, or exact at the byte level?
- How should dots and summaries describe repeated blocks without looking like repeated lines?
- Should the first block after a long gap print again as context?
- Is block compression still qurtail's job if it needs tool-specific parsers to work well?

Tool-specific parsing is the cutoff. Qurtail's rules are small enough that a user can predict what
it will hide. A block matcher has to stay that understandable. If it needs separate knowledge of
pytest, Jest, and Java stacks, it belongs in a different tool.
