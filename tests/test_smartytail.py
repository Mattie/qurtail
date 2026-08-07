"""Tests for smartytail's documented stream compression behavior."""

from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from smartytail import compress


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

    def test_comparison_lines_remain_persistent_known_noise(self) -> None:
        output = StringIO()

        compress(
            ["known noise 124\n", "meaningful event\n"],
            output,
            comparison_lines=["known noise 123\n"],
        )

        self.assertEqual(output.getvalue(), ".\nmeaningful event\n")


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
                    "smartytail",
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
                    "smartytail",
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
            Path(home, ".smartytailrc").write_text(
                "[smartytail]\n"
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
                [sys.executable, "-m", "smartytail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            overridden = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "smartytail",
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
            Path(home, ".smartytailrc").write_text(
                "[smartytail]\nsimilarity = 1.0\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            path.write_text("worker 1234\nworker 1235\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "smartytail", str(path)],
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
            Path(home, ".smartytailrc").write_text(
                "[smartytail]\nignore_fields = timestamp, request_id\n",
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
                [sys.executable, "-m", "smartytail", str(path)],
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
            Path(home, ".smartytailrc").write_text(
                "[smartytail]\nmode = counts\n",
                encoding="utf-8",
            )
            path = Path(home, "input.log")
            path.write_text("same\nsame\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "smartytail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "same\n[1 similar line, 0s]\n")

    def test_rc_comparison_file_is_relative_to_the_rc_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Path(home, ".smartytailrc").write_text(
                "[smartytail]\ncomparison_file = known.log\n",
                encoding="utf-8",
            )
            Path(home, "known.log").write_text("known noise 123\n", encoding="utf-8")
            path = Path(home, "input.log")
            path.write_text("known noise 124\nmeaningful event\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["USERPROFILE"] = str(home)

            result = subprocess.run(
                [sys.executable, "-m", "smartytail", str(path)],
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
                    "smartytail",
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
            Path(home, ".smartytailrc").write_text(
                "[smartytail]\ninclude_regex = error\nexclude_regex = debug\n",
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
                [sys.executable, "-m", "smartytail", str(path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            overridden = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "smartytail",
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
                    "smartytail",
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
                "smartytail",
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
                    'printf "ready 1\\nready 2\\nfailed\\n" | smartytail',
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
                [shutil.which("bash"), "-c", "smartytail -h"],
                env=shell_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("usage: smartytail", help_result.stdout)
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
            self.assertIn("--ignore-field", help_result.stdout)
            self.assertIn("--message-field", help_result.stdout)
            self.assertIn("--comparison-file", help_result.stdout)

            process = subprocess.Popen(
                [
                    shutil.which("bash"),
                    "-c",
                    'exec smartytail -f "$1"',
                    "smartytail-test",
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
                    "smartytail",
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
