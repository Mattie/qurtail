# Qurtail usefulness for agentic tasks

Reviewed: 2026-09-09. Scope: the local 1.1.0 working tree, including existing uncommitted
telemetry changes. No implementation changes were made during this review.

Historical assessment: automatic interleaved references were subsequently implemented.
See [the behavior](interleaved.md) and [fresh evaluation](../benchmarks/README.md#automatic-interleaved-references).

## Assessment

Qurtail has a useful foundation for agents watching repetitive services: dependency-free execution,
immediate complete-line output, conservative numeric handling, diagnostic preservation, child exit
status propagation, signal cleanup, and optional raw capture. Its strongest current use case is
consecutive repetitions in a live monitoring stream.

Our broader claim that it should be the default for verbose builds, tests, and installers needs
new evidence. The current matcher has limited compression on several recorded development
workloads. Its normalization also hides some changes an agent could need for diagnosis.

The recommended direction is a small, trustworthy stream reducer whose output remains useful
across separate agent reads. Success should mean correct decisions, less total context and polling
work, and inexpensive access to the original evidence.

## What the evidence says

Fresh probes used the production reducer with default settings; byte counts are UTF-8, including
newlines. These synthetic cases demonstrate behavior, not workload prevalence or agent accuracy.

| Fresh probe | Result | Implication |
| --- | --- | --- |
| 10,000 identical `INFO heartbeat` records | 9,999 suppressed; 150,000 input bytes became 10,042 output bytes | Strong reduction, with 9,999 dots still emitted |
| 10,000 alternating service A/B health records | None suppressed; all 230,000 bytes emitted | Adjacent-run matching misses even simple interleaving |
| 100 identical JSON records longer than 1,024 characters | None suppressed | Large structured records bypass matching |
| 100 identical three-line Python tracebacks | All 300 lines emitted | Diagnostic preservation currently prevents block compression |
| Same JSON error, `host` changed from node-a to node-b | Second record suppressed | A newly affected machine disappears from the compact view |
| Same error, embedded certificate expiry changed from 2026 to 2027 | Second record suppressed | A timestamp in the message body can be meaningful evidence |
| Same missing-object error, resource UUID changed | Second record suppressed | Distinct affected resources can collapse together |

These last three are confirmed visibility failures under the default matching policy. We have
not measured whether an agent makes a wrong diagnosis because of them.

Two real CLI probes exposed usability limits:

- A child flushed `ready> ` without a newline, then waited on stdin. Neither the compact view nor
  the raw log exposed those bytes within 500 ms. Once the child received input and emitted a
  newline, both contained the completed output. The reader iterates whole binary lines before
  writing the transcript. This also matters for noninteractive carriage-return progress output;
  interactive commands are already outside the skill's supported scope.
- A file containing an initial error and twenty distinct progress lines produced only its final
  ten lines with `qurtail file.log`. This is intentional tail behavior and has a regression test,
  but the README's instruction to compact a file once can suggest a complete-file scan.

Existing repository evidence adds useful perspective:

- `benchmarks/hdfs-full-results.json` records 11,175,629 input records, 100% retained labeled anomaly
  units, and approximately **0.72% byte reduction**. Its source-binding tests passed. This supports
  performance and the measured recall property; it supplies little evidence of compression value
  for that corpus. The large replay was not rerun for this review.
- `docs/potential_improvements.md` records **0.05–0.46% conservative reduction** on three LogDx
  development cases, with every required signal retained. Those are earlier recorded runs, not
  fresh measurements. The aggressive setting barely improved them.
- The attractive paired-agent and multi-project Codex results are explicitly historical pre-1.0
  evidence. They cannot establish current agent decision quality or adoption.

## Recommended sequence

### TRUST — fix normalization and clarify the viewing contract first

`_stable_json` removes top-level host, hostname, and pid values and replaces ID fields at arbitrary
nesting depths. `_text_signature` can replace UUIDs and timestamps in message bodies, including
JSON string values. These transformations assume a semantic role from a value's shape or name.

Keep source identity significant. Restrict default timestamp removal to recognizable event-time
positions or narrowly defined envelope fields. Preserve resource UUIDs and values inside error
payloads unless we have a well-supported reason to treat that location as metadata. Unexpected
field types should pass through. Add mutation cases for each corrected behavior, alongside the
existing positive repetition cases.

We should accept lower compression where these roles are ambiguous. Raw capture helps recovery,
but an agent cannot know that it needs recovery when a meaningful change was never signaled.

Clarify the last-ten-lines file behavior and provide an explicit complete-file path, such as a
future `--all`. Existing POSIX users can stream the whole file through stdin. Keep tail semantics
stable for existing invocations.

### VIEW — make separate agent reads understandable

Prototype a no-dot summary mode first. Fixed `--dot-every` still makes output grow with the number
of repeated records, and short tool polls can receive dots without their closing count or exemplar.
The current thirty-second summary contains a count and elapsed time but no pattern reference.

An agent-oriented repeat update should identify its pattern, make clear whether its count is a
delta or total, and include enough exemplar context to survive a fresh read. When raw capture is
enabled, point to the original evidence. Define raw references as byte offsets into the captured
combined stream, with explicit handling of file rotation or replacement for followed files.

Separate stream observation from process status: repeated lines prove input activity, while a
quiet child may be healthy, stuck, or waiting. A runner can report observed state such as running,
last output time, and exit status without claiming to know the application's health.

Structured JSONL is a possible rendering of this contract. Build it when an actual consumer needs
it; adding another output format alone does not establish agent usefulness. Prefer explicit
opt-in behavior while the format and costs are being tested.

Raw capture should copy bounded byte chunks before line framing. The display can retain
conservative record rules while the transcript remains available promptly. Define a bounded
partial-record pass-through behavior, then test long lines, carriage returns, incomplete UTF-8,
EOF, and cancellation. A bounded signature table does not bound the input line reader's memory.

### EVAL — measure agent outcomes alongside these changes

Run a current paired evaluation with the same tasks, model/runtime, skill, and raw evidence access.
Include actual tool output limits and polling boundaries. Charge raw rereads and additional tool
calls to the compact arm. Keep task fixtures separate from matcher tuning fixtures.

Cover repetitive monitoring, alternating services, builds, tests, installers, long structured logs,
repeated diagnostics with one changed line, process completion, cancellation, and recovery after
context loss. Compare against raw output, appropriate native quiet flags, simple adjacent dedup,
and a command-aware reducer on commands it supports.

Measure decision correctness, faithful evidence, total tokens including skill/setup and recovery,
tool calls, wall time, detection latency, time to first useful output, output expansion on short
runs, raw capture equality, and resource bounds. Record model and source versions. Use actual
tokenizers for token claims: the local dependency-free test uses a token proxy.

Optional telemetry currently records invocation lifecycle, arguments, and duration. That helps
debug usage; it does not measure savings or decision quality. Per-run input/output byte and record
counts would provide more direct utility evidence without requiring full argument history.

### COVERAGE — prototype additional repetition only after the trust rules hold

Test two bounded experiments independently:

1. Interleaved repetition within explicit sources and short windows. Preserve first observations,
   changed records, resumed states, and temporal evidence. A simple global seen-pattern cache
   would hide useful recovery and ordering information.
2. Exact repeated blocks, starting with tracebacks and repeated test output. Preserve the first
   block and any changed message, path, assertion, or frame. Bound bytes, lines, retained signatures,
   and delay; uncertain boundaries pass through.

The existing block-prototype proposal is sensible. Add alternating service records to the
experiment matrix so we distinguish missing line interleaving from missing multiline matching.
Evaluate long exact records separately: the 1,024-character limit is another independent cause of
poor reduction. Keep all experiments behind measured correctness and latency gates.

### ADOPTION — validate that agents choose it appropriately

Keep the skill short and provide examples for long-running services, one-shot commands, whole-file
reads, raw recovery, completion, and cancellation. Test whether a fresh agent selects qurtail for
useful cases and skips short or unsuitable commands. Installation and skill discovery are distinct
steps and should be documented accurately.

## Fit with current agent tooling

Current tooling increasingly processes results in code before exposing selected information to the
model. Anthropic's programmatic tool calling documents this execution pattern. The implication
for qurtail is a simple interface scripts can consume and summarize, with predictable evidence
recovery. This is a product inference, not an externally validated qurtail result.
Source: [Anthropic engineering](https://www.anthropic.com/engineering/advanced-tool-use).

RTK already documents command-specific filters, agent integrations, and raw-output recall.
Qurtail should include it as a comparator for overlapping workloads. Its implementation breadth
does not establish superiority on qurtail's live streams, and this review did not benchmark RTK.
Source: [RTK repository](https://github.com/rtk-ai/rtk).

MCP's versioned task specification describes polling and deferred retrieval and labels its task
extension experimental. It supports considering integration later. It does not justify building
a qurtail scheduler, daemon, or MCP server before a consumer needs one.
Source: [MCP tasks specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks).

Preserve the dependency-free CLI and conservative local processing. Defer broad fuzzy matching,
LLM summarization, large parser/plugin catalogs, and language rewrites until task measurements
identify a concrete need.

## Verification and remaining uncertainty

- [x] Review scope and agent success criteria defined; current working tree inspected.
- [x] Confirmed behavior separated from proposed improvements and downstream accuracy hypotheses.
- [x] `python3 -m unittest discover -s tests`: **109 tests passed** under Ubuntu/WSL.
- [x] Fresh reducer and real CLI probes checked compression, hidden mutations, partial output, and
  file selection. Source-bound historical evidence was distinguished from fresh runs.
- [x] Current external claims checked against primary sources; independent review checked scope
  and overclaims. No production or skill files were changed.
- [ ] Current paired-agent evaluation, tokenizer-based benchmark rerun, and cross-platform live
  framing checks remain outstanding. These are the next checks before broader usefulness claims.

Reviewed `qurtail.py` SHA-256:
`30fde29a4b1c49be20b5c8fc7b3228eb4607645a1a5a18dd1114d5ad7e98c3b5`.
