"""Tests for qurtail's documented stream compression behavior."""

from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from qurtail import compress


CORPUS_PATH = Path(__file__).parent / "fixtures" / "log_corpus.json"


class TerminalBuffer(StringIO):
    """String buffer that behaves like an interactive terminal for color tests."""

    def isatty(self) -> bool:
        return True


class CompressTests(unittest.TestCase):
    """Exercise line similarity, marker output, and recent-history limits."""

    def test_readme_style_output(self) -> None:
        output = StringIO()
        lines = ["starting worker 17\n"]
        lines.extend(f"starting worker {number}\n" for number in range(18, 23))
        lines.append("connection reset by peer\n")
        lines.extend("connection reset by peer\n" for _ in range(30))
        lines.append("finished batch 42\n")

        compress(lines, output)

        self.assertEqual(
            output.getvalue(),
            "starting worker 17\n"
            ".....\n"
            "connection reset by peer\n"
            "..............................\n"
            "finished batch 42\n",
        )

    def test_meaningfully_different_lines_are_printed_in_full(self) -> None:
        output = StringIO()

        compress(["service started\n", "database disconnected\n"], output)

        self.assertEqual(output.getvalue(), "service started\ndatabase disconnected\n")

    def test_history_only_covers_recent_lines(self) -> None:
        output = StringIO()

        compress(
            ["red apple\n", "blue sky\n", "green grass\n", "red apple\n"],
            output,
            history_size=2,
        )

        self.assertEqual(
            output.getvalue(),
            "red apple\nblue sky\ngreen grass\nred apple\n",
        )

    def test_marker_can_be_changed(self) -> None:
        output = StringIO()

        compress(["same\n", "same\n", "same\n"], output, marker="~")

        self.assertEqual(output.getvalue(), "same\n~~\n")

    def test_dot_every_groups_suppressed_lines(self) -> None:
        output = StringIO()

        compress(["same\n"] * 8, output, dot_every=3)

        self.assertEqual(output.getvalue(), "same\n..\n")

    def test_partial_dot_group_does_not_add_blank_output(self) -> None:
        output = StringIO()

        compress(["same\n", "same\n", "changed\n"], output, dot_every=3)

        self.assertEqual(output.getvalue(), "same\nchanged\n")

    def test_spinner_rotates_in_one_terminal_cell(self) -> None:
        output = StringIO()

        compress(
            ["same\n", "same\n", "same\n", "same\n", "same\n"],
            output,
            mode="spinner",
        )

        self.assertEqual(output.getvalue(), "same\n|\b/\b-\b\\\n")

    def test_common_timestamp_and_level_formats_can_be_ignored(self) -> None:
        output = StringIO()
        lines = [
            "2026-08-06T12:34:56Z [INFO] worker ready\n",
            "Aug  6 12:35:01 WARNING: worker ready\n",
            "[12:35:02] ERROR worker ready\n",
            "[06/Aug/2026:12:35:03 -0500] DEBUG worker ready\n",
        ]

        compress(lines, output, ignore_timestamps=True, ignore_levels=True)

        self.assertEqual(output.getvalue(), lines[0] + "...\n")

    def test_severity_change_is_shown_before_repeated_errors_are_suppressed(
        self,
    ) -> None:
        output = StringIO()
        lines = [
            "INFO worker heartbeat request=41\n",
            "ERROR worker heartbeat request=41\n",
            "ERROR worker heartbeat request=41\n",
        ]

        compress(lines, output)

        self.assertEqual(output.getvalue(), lines[0] + lines[1] + ".\n")

    def test_json_severity_change_survives_message_field_selection(self) -> None:
        output = StringIO()
        lines = [
            '{"level":30,"msg":"request complete"}\n',
            '{"level":50,"msg":"request complete"}\n',
            '{"level":50,"msg":"request complete"}\n',
        ]

        compress(lines, output, message_field="msg")

        self.assertEqual(output.getvalue(), lines[0] + lines[1] + ".\n")

    def test_json_metadata_key_does_not_hide_a_severity_change(self) -> None:
        output = StringIO()
        lines = [
            '{"level":30,"error":null,"msg":"request complete"}\n',
            '{"level":50,"error":null,"msg":"request complete"}\n',
            '{"level":50,"error":null,"msg":"request complete"}\n',
        ]

        compress(lines, output, message_field="msg")

        self.assertEqual(output.getvalue(), lines[0] + lines[1] + ".\n")

    def test_json_error_payload_survives_message_field_selection(self) -> None:
        output = StringIO()
        lines = [
            '{"level":30,"error":null,"msg":"request complete"}\n',
            '{"level":30,"error":"connection refused","msg":"request complete"}\n',
            '{"level":30,"error":"connection refused","msg":"request complete"}\n',
        ]

        compress(lines, output, message_field="msg")

        self.assertEqual(output.getvalue(), lines[0] + lines[1] + ".\n")

    def test_json_http_status_outranks_normal_level(self) -> None:
        output = StringIO()
        lines = [
            '{"level":30,"status":200,"msg":"request complete"}\n',
            '{"level":30,"status":200,"msg":"request complete"}\n',
            '{"level":30,"status":404,"msg":"request complete"}\n',
            '{"level":30,"status":404,"msg":"request complete"}\n',
            '{"level":30,"status":500,"msg":"request complete"}\n',
            '{"level":30,"status":500,"msg":"request complete"}\n',
        ]

        compress(lines, output, message_field="msg")

        self.assertEqual(
            output.getvalue(),
            lines[0] + ".\n" + lines[2] + ".\n" + lines[4] + ".\n",
        )

    def test_nested_pino_response_status_is_preserved(self) -> None:
        output = StringIO()
        lines = [
            '{"level":30,"res":{"statusCode":200},"msg":"request complete"}\n',
            '{"level":30,"res":{"statusCode":500},"msg":"request complete"}\n',
            '{"level":30,"res":{"statusCode":500},"msg":"request complete"}\n',
        ]

        compress(lines, output, message_field="msg")

        self.assertEqual(output.getvalue(), lines[0] + lines[1] + ".\n")

    def test_http_error_status_is_shown_then_repeated_errors_are_suppressed(
        self,
    ) -> None:
        output = StringIO()
        lines = [
            '127.0.0.1 - - "GET /health HTTP/1.1" 200 17\n',
            '127.0.0.1 - - "GET /health HTTP/1.1" 500 17\n',
            '127.0.0.1 - - "GET /health HTTP/1.1" 500 17\n',
        ]

        compress(lines, output, similarity=0.95)

        self.assertEqual(output.getvalue(), lines[0] + lines[1] + ".\n")

    def test_http_status_inside_a_json_message_is_preserved(self) -> None:
        output = StringIO()
        lines = [
            '{"log":"GET /health HTTP/1.1\\\" 200 17"}\n',
            '{"log":"GET /health HTTP/1.1\\\" 500 17"}\n',
            '{"log":"GET /health HTTP/1.1\\\" 500 17"}\n',
        ]

        compress(lines, output, message_field="log", similarity=0.95)

        self.assertEqual(output.getvalue(), lines[0] + lines[1] + ".\n")

    def test_regex_filters_run_before_similarity_tracking(self) -> None:
        output = StringIO()
        lines = [
            "info ready\n",
            "error disk 100\n",
            "debug error trace\n",
            "error disk 101\n",
        ]

        compress(
            lines,
            output,
            include_regex="error",
            exclude_regex="^debug",
        )

        self.assertEqual(output.getvalue(), "error disk 100\n.\n")

    def test_marker_and_spinner_colors_are_terminal_only(self) -> None:
        dots = TerminalBuffer()
        spinner = TerminalBuffer()
        redirected = StringIO()

        compress(["same\n", "same\n"], dots, dot_color="yellow")
        compress(
            ["same\n", "same\n"],
            spinner,
            mode="spinner",
            spinner_color="green",
        )
        compress(["same\n", "same\n"], redirected, dot_color="yellow")

        self.assertEqual(dots.getvalue(), "same\n\x1b[33m.\x1b[0m\n")
        self.assertEqual(spinner.getvalue(), "same\n\x1b[32m|\x1b[0m\n")
        self.assertEqual(redirected.getvalue(), "same\n.\n")

    def test_counts_mode_updates_count_and_elapsed_time(self) -> None:
        output = StringIO()
        timestamps = iter((10.0, 18.9))

        compress(
            ["same\n", "same\n", "same\n"],
            output,
            mode="counts",
            clock=lambda: next(timestamps),
        )

        self.assertEqual(
            output.getvalue(),
            "same\n[1 similar line, 0s]\r[2 similar lines, 8s]\n",
        )

    def test_json_fields_can_be_ignored_or_selected(self) -> None:
        ignored_output = StringIO()
        selected_output = StringIO()
        lines = [
            '{"timestamp":"12:00","request_id":"a","msg":"ready"}\n',
            '{"request_id":"b","msg":"ready","timestamp":"12:01"}\n',
        ]

        compress(
            lines,
            ignored_output,
            ignore_fields=("timestamp", "request_id"),
        )
        compress(lines, selected_output, message_field="msg")

        self.assertEqual(ignored_output.getvalue(), lines[0] + ".\n")
        self.assertEqual(selected_output.getvalue(), lines[0] + ".\n")

    def test_nested_json_message_field_can_be_selected(self) -> None:
        output = StringIO()
        lines = [
            '{"timeUnixNano":"1","body":{"stringValue":"ready"}}\n',
            '{"timeUnixNano":"2","body":{"stringValue":"ready"}}\n',
        ]

        compress(lines, output, message_field="body.stringValue")

        self.assertEqual(output.getvalue(), lines[0] + ".\n")

    def test_exact_json_key_takes_priority_over_dotted_lookup(self) -> None:
        output = StringIO()
        lines = [
            '{"body.stringValue":"ready","body":{"stringValue":"one"}}\n',
            '{"body.stringValue":"ready","body":{"stringValue":"two"}}\n',
        ]

        compress(lines, output, message_field="body.stringValue")

        self.assertEqual(output.getvalue(), lines[0] + ".\n")

    def test_comparison_lines_remain_persistent_known_noise(self) -> None:
        output = StringIO()

        compress(
            ["known noise 124\n", "meaningful event\n"],
            output,
            comparison_lines=["known noise 123\n"],
        )

        self.assertEqual(output.getvalue(), ".\nmeaningful event\n")

    def test_comparison_lines_do_not_hide_a_severity_transition(self) -> None:
        output = StringIO()

        compress(
            ["ERROR known noise 124\n", "ERROR known noise 124\n"],
            output,
            comparison_lines=["INFO known noise 123\n"],
        )

        self.assertEqual(output.getvalue(), "ERROR known noise 124\n.\n")

    def test_rotate_sample_prints_every_nth_repeat_in_full(self) -> None:
        output = StringIO()

        compress(["heartbeat\n"] * 6, output, rotate_sample=3)

        self.assertEqual(output.getvalue(), "heartbeat\n..\nheartbeat\n..\n")

    def test_rotate_sample_can_use_a_seconds_interval(self) -> None:
        output = StringIO()
        timestamps = iter((0.0, 4.0, 10.0))

        compress(
            ["heartbeat\n"] * 4,
            output,
            rotate_sample="10s",
            clock=lambda: next(timestamps),
        )

        self.assertEqual(output.getvalue(), "heartbeat\n..\nheartbeat\n")

    def test_rotate_sample_timer_survives_silent_dot_groups(self) -> None:
        output = StringIO()
        timestamps = iter((0.0, 4.0, 10.0))

        compress(
            ["heartbeat\n"] * 4,
            output,
            dot_every=100,
            rotate_sample="10s",
            clock=lambda: next(timestamps),
        )

        self.assertEqual(output.getvalue(), "heartbeat\nheartbeat\n")


class CorpusTests(unittest.TestCase):
    """Verify logs commonly inspected during development against sanitized fixtures."""

    def test_corpus_declares_intended_fixture_coverage(self) -> None:
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        cases = corpus["cases"]

        self.assertIn("Sanitized", corpus["methodology"])
        self.assertTrue(corpus["sources"])
        self.assertEqual(
            {platform for case in cases for platform in case["platforms"]},
            {"linux", "windows", "macos"},
        )
        self.assertTrue(
            {"application", "test", "web", "container", "os"}
            <= {case["workload"] for case in cases}
        )
        self.assertGreaterEqual(len({case["format"] for case in cases}), 10)

    def test_development_log_corpus_compresses_noise_and_preserves_exceptions(
        self,
    ) -> None:
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        cases = corpus["cases"]

        for case in cases:
            with self.subTest(case=case["name"]):
                output = StringIO()
                lines = case["lines"]

                self.assertIn(case["source"], corpus["sources"])
                self.assertEqual(len(lines), 3)
                compress(
                    (line + "\n" for line in lines),
                    output,
                    **case["options"],
                )

                self.assertEqual(
                    output.getvalue(),
                    lines[0] + "\n.\n" + lines[2] + "\n",
                )


class CommandTests(unittest.TestCase):
    """Check that the command can process a file from end to end."""

    def test_file_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "input.log")
            path.write_text("ready 1\nready 2\nfailed\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "qurtail",
                    "--config",
                    str(Path(directory, "missing.rc")),
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ready 1\n.\nfailed\n")

    def test_file_input_starts_with_the_last_ten_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "input.log")
            path.write_text("\n".join("abcdefghijkl") + "\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "qurtail",
                    "--config",
                    str(Path(directory, "missing.rc")),
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "c\nd\ne\nf\ng\nh\ni\nj\nk\nl\n")

    def test_rc_enables_spinner_and_cli_can_override_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".qurtailrc").write_text(
                "[qurtail]\n"
                "mode = spinner\n"
                "spinner = ab\n"
                "spinner_color = green\n"
                "dot_color = yellow\n"
                "ignore_timestamps = yes\n"
                "ignore_levels = true\n"
                "ignore_prefixes = api:, worker:\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            path.write_text(
                "2026-08-06T12:34:56Z INFO api: task done\n"
                "Aug  6 12:35:01 ERROR worker: task done\n"
                "[12:35:02] WARN api: changed\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            configured = subprocess.run(
                [sys.executable, "-m", "qurtail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            overridden = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "qurtail",
                    "--mode",
                    "dots",
                    "--marker",
                    "~",
                    str(path),
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(configured.returncode, 0, configured.stderr)
        self.assertEqual(
            configured.stdout,
            "2026-08-06T12:34:56Z INFO api: task done\na\n"
            "[12:35:02] WARN api: changed\n",
        )
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        self.assertEqual(
            overridden.stdout,
            "2026-08-06T12:34:56Z INFO api: task done\n~\n"
            "[12:35:02] WARN api: changed\n",
        )

    def test_rc_can_set_similarity_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".qurtailrc").write_text(
                "[qurtail]\nsimilarity = 1.0\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            path.write_text("worker 1234\nworker 1235\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "qurtail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "worker 1234\nworker 1235\n")

    def test_rc_can_configure_json_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".qurtailrc").write_text(
                "[qurtail]\nignore_fields = timestamp, request_id\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            first = '{"timestamp":"12:00","request_id":"a","msg":"ready"}\n'
            path.write_text(
                first + '{"request_id":"b","msg":"ready","timestamp":"12:01"}\n',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "qurtail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, first + ".\n")

    def test_rc_can_enable_counts_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".qurtailrc").write_text(
                "[qurtail]\nmode = counts\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            path.write_text("same\nsame\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "qurtail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "same\n[1 similar line, 0s]\n")

    def test_rc_can_reduce_dot_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".qurtailrc").write_text(
                "[qurtail]\nmode = dots\ndot_every = 2\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            path.write_text("same\nsame\nsame\nsame\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "qurtail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "same\n.\n")

    def test_rc_can_rotate_every_nth_suppressed_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".qurtailrc").write_text(
                "[qurtail]\nrotate_sample = 2\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            path.write_text("same\nsame\nsame\nsame\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "qurtail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "same\n.\nsame\n.\n")

    def test_rc_comparison_file_is_relative_to_the_rc_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".qurtailrc").write_text(
                "[qurtail]\ncomparison_file = known.log\n",
                encoding="utf-8",
            )
            Path(home, "known.log").write_text("known noise 123\n", encoding="utf-8")
            path = Path(home, "input.log")
            path.write_text("known noise 124\nmeaningful event\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "qurtail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ".\nmeaningful event\n")

    def test_missing_comparison_file_reports_a_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory, "missing.log")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "qurtail",
                    "--config",
                    str(Path(directory, "missing.rc")),
                    "--comparison-file",
                    str(missing),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn(str(missing), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rc_and_cli_regex_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".qurtailrc").write_text(
                "[qurtail]\ninclude_regex = error\nexclude_regex = debug\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            path.write_text(
                "info ready\n"
                "error disk 100\n"
                "debug error trace\n"
                "error disk 101\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            configured = subprocess.run(
                [sys.executable, "-m", "qurtail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            overridden = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "qurtail",
                    "--include-regex",
                    "info",
                    "--exclude-regex",
                    "nomatch",
                    str(path),
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(configured.returncode, 0, configured.stderr)
        self.assertEqual(configured.stdout, "error disk 100\n.\n")
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        self.assertEqual(overridden.stdout, "info ready\n")

    def test_cli_accepts_documented_ignore_prefixes_option(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "input.log")
            path.write_text(
                "api: task done\nworker: task done\nchanged\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "qurtail",
                    "--config",
                    str(Path(directory, "missing.rc")),
                    "--ignore-prefixes",
                    "api:, worker:",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "api: task done\n.\nchanged\n")

    def test_invalid_regex_reports_a_cli_error(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "qurtail",
                "--config",
                "missing.rc",
                "--include-regex",
                "[",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid filter regex", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_rotate_sample_reports_a_cli_error(self) -> None:
        for value in ("0", "nans"):
            with self.subTest(value=value):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "qurtail",
                        "--config",
                        "missing.rc",
                        "--rotate-sample",
                        value,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("rotate_sample must be a positive", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_dot_every_reports_a_cli_error(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "qurtail",
                "--config",
                "missing.rc",
                "--dot-every",
                "0",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--dot-every must be at least 1", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(
        sys.platform != "win32" and shutil.which("bash"),
        "requires Bash on a POSIX system",
    )
    def test_cli_runs_cleanly_from_bash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(__file__).resolve().parents[1]
            environment = Path(directory, "venv")
            setup = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "venv",
                    "--system-site-packages",
                    str(environment),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)

            install = subprocess.run(
                [
                    str(environment / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--no-build-isolation",
                    "--no-deps",
                    "-e",
                    str(project),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            shell_environment = os.environ.copy()
            shell_environment["PATH"] = os.pathsep.join(
                [str(environment / "bin"), shell_environment["PATH"]]
            )
            shell_environment["HOME"] = directory
            shell_environment["USERPROFILE"] = directory
            pipeline_result = subprocess.run(
                [
                    shutil.which("bash"),
                    "-c",
                    'printf "ready 1\\nready 2\\nfailed\\n" | qurtail',
                ],
                env=shell_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(pipeline_result.returncode, 0, pipeline_result.stderr)
            self.assertEqual(pipeline_result.stdout, "ready 1\n.\nfailed\n")

            path = Path(directory, "my.log")
            path.write_text("\n".join("abcdefghijkl") + "\n", encoding="utf-8")
            help_result = subprocess.run(
                [shutil.which("bash"), "-c", "qurtail -h"],
                env=shell_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("usage: qurtail", help_result.stdout)
            self.assertIn("--follow", help_result.stdout)
            self.assertIn("--config", help_result.stdout)
            self.assertIn("--mode", help_result.stdout)
            self.assertIn("--ignore-timestamps", help_result.stdout)
            self.assertIn("--ignore-prefix", help_result.stdout)
            self.assertIn("--ignore-prefixes", help_result.stdout)
            self.assertIn("--include-regex", help_result.stdout)
            self.assertIn("--exclude-regex", help_result.stdout)
            self.assertIn("--spinner-color", help_result.stdout)
            self.assertIn("--dot-color", help_result.stdout)
            self.assertIn("--dot-every", help_result.stdout)
            self.assertIn("--ignore-field", help_result.stdout)
            self.assertIn("--message-field", help_result.stdout)
            self.assertIn("--comparison-file", help_result.stdout)
            self.assertIn("--rotate-sample", help_result.stdout)

            process = subprocess.Popen(
                [
                    shutil.which("bash"),
                    "-c",
                    'exec qurtail -f "$1"',
                    "qurtail-test",
                    str(path),
                ],
                env=shell_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            time.sleep(0.6)
            rotated = Path(directory, "my.log.1")
            path.replace(rotated)
            time.sleep(0.4)
            path.write_text(
                "rotation alpha\nreplacement complete\n",
                encoding="utf-8",
            )
            time.sleep(0.6)
            path.write_text("after truncate\n", encoding="utf-8")
            time.sleep(0.6)

            process.terminate()
            stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "c\nd\ne\nf\ng\nh\ni\nj\nk\nl\n"
            "rotation alpha\nreplacement complete\nafter truncate\n",
        )

    def test_follow_flag_reads_the_file_and_waits_for_more(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "my.log")
            path.write_text("ready 1\nready 2\nfailed\n", encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "qurtail",
                    "--config",
                    str(Path(directory, "missing.rc")),
                    "-f",
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            with self.assertRaises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.5)

            process.terminate()
            stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(stderr, "")
        self.assertEqual(stdout, "ready 1\n.\nfailed\n")


if __name__ == "__main__":
    unittest.main()
