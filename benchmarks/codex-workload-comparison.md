# Codex live workload replay

This corpus samples 4 verification workloads selected from Codex task records in separate local Git projects. The same source commands ran 2 times in this task, and the exact output Codex received was sanitized and replayed through the real qurtail CLI.

Project names are replaced with functional aliases. No prompts, transcript prose, task IDs, environment values, repository paths, or external machine logs are included.

## Original agent command lines

Each command line below is copied from its Codex task record. It produced the output the original agent monitored during that task.

- **Compression regression suite** (`qurtail`, Python / unittest):
  Original agent command: `PYTHONPATH=tests python -m unittest test_qurtail.CompressTests -v`  
  Check qurtail's comparison, filtering, severity, and display behavior. The source qurtail task recorded this suite passing.
- **Canvas frontend regression** (`canvas-frontend`, React / PixiJS / Vitest):
  Original agent command: `npm test -- src/__tests__/CanvasApp.test.tsx`  
  Check a Canvas interface and its article-view integration. The source task ran this focused command before reporting its full frontend suite green.
- **Python CLI smoke test** (`python-cli`, Python / Poetry / Pytest):
  Original agent command: `poetry run pytest tests/test_init_files.py::test_installed_console_script_init_runs -q`  
  Check an installed console entry point in an isolated directory. The source task reported both CLI entry-path smoke tests passing.
- **Visual renderer regression** (`visual-renderer`, TypeScript / Three.js / Vitest):
  Original agent command: `npm test -- tests/steer/engine/cameraImageEffects.test.ts`  
  Check deterministic camera-image-effect sampling and defaults. The source task included this target in a 722-test passing suite.

## Comparison

Tokenizer: `tiktoken 0.13.0`, encoding `o200k_base`.

| Project alias | Technology | Raw lines | Raw tokens | Qurtail lines | Qurtail tokens | Token reduction | Completion retained |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `qurtail` | Python / unittest | 64 | 1710 | 33 | 858 | 49.8% | `OK` |
| `canvas-frontend` | React / PixiJS / Vitest | 44 | 906 | 23 | 458 | 49.4% | `passed` |
| `python-cli` | Python / Poetry / Pytest | 8 | 172 | 5 | 87 | 49.4% | `passed` |
| `visual-renderer` | TypeScript / Three.js / Vitest | 22 | 186 | 11 | 97 | 47.8% | `passed` |

Qurtail replay command: `python -m qurtail -c benchmarks/codex-workload-replay.toml`.

## Limits

The task records establish which commands earlier agents used and their reported outcomes. These token measurements are fresh reruns of those source commands, so they do not claim byte-for-byte identity with archived task output.

The captured material is completed command output replayed after each run. Live file-follow performance, unattended agent completion, diagnosis quality, and broad tool superiority are outside this comparison.

The replay config uses `similarity = 1.0` after path and timing normalization. These rows measure repeat-command compression rather than fuzzy-match quality.

Re-capture requires the three private project-root environment variables named in `capture_codex_workloads.py`; their values are never written to the artifacts. Then run `python benchmarks/capture_codex_workloads.py` after installing `benchmarks/requirements.txt`.
