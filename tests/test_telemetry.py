"""Verify qurtail's local, opt-in command telemetry contract."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import _qurtail_telemetry as telemetry
import qurtail


START_RECORD = re.compile(
    r"^(?P<timestamp>\S+) format=1 event=START "
    r"invocation=(?P<invocation>\S+) tool=qurtail "
    r"version=(?P<version>\S+) pid=(?P<pid>\d+) "
    r"cwd=(?P<cwd>\"(?:\\.|[^\"])*\") "
    r"argv=(?P<argv>\[.*\]) lossy=(?P<lossy>true|false)$"
)
FINISH_RECORD = re.compile(
    r"^(?P<timestamp>\S+) format=1 event=FINISH "
    r"invocation=(?P<invocation>\S+) exit=(?P<exit>-?\d+) "
    r"duration_ms=(?P<duration_ms>\d+)$"
)


class TelemetryTestCase(unittest.TestCase):
    """Provide each telemetry test with an isolated home directory."""

    def setUp(self) -> None:
        self.temporary_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_home.cleanup)
        self.home = Path(self.temporary_home.name)
        home_patch = patch("_qurtail_telemetry._home_directory", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def write_config(self, body: str = 'mode = "full"\n') -> Path:
        """Write a telemetry section beneath the isolated home."""
        config_path = self.home / ".qurtail" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("[telemetry]\n" + body, encoding="utf-8")
        return config_path

    def log_lines(self) -> list[str]:
        """Read the active telemetry log as physical lines."""
        path = self.home / ".qurtail" / "telemetry" / "commands.log"
        return path.read_text(encoding="utf-8").splitlines()


class ConfigurationTests(TelemetryTestCase):
    """Keep configuration bounded, strict, and off by default."""

    def test_missing_section_and_off_mode_create_no_telemetry_directory(self) -> None:
        config_path = self.home / ".qurtail" / "config.toml"
        config_path.parent.mkdir(parents=True)
        for content in ('title = "qurtail"\n', '[telemetry]\nmode = "off"\n'):
            with self.subTest(content=content):
                config_path.write_text(content, encoding="utf-8")
                session = telemetry.start_session(["qurtail"], "1.1.0")
                session.finish(0)
                self.assertFalse((self.home / ".qurtail" / "telemetry").exists())

    def test_full_mode_uses_defaults_and_ignores_future_fields(self) -> None:
        self.write_config('mode = "full"\nfuture = "accepted"\n')

        config = telemetry._load_config()

        assert config is not None
        self.assertEqual(config.max_file_bytes, 5 * 1024 * 1024)
        self.assertEqual(config.max_files, 5)

    def test_invalid_configuration_disables_telemetry(self) -> None:
        cases = (
            'mode = "safe"\n',
            'mode = "full"\nmax_file_bytes = 0\n',
            'mode = "full"\nmax_files = -1\n',
            'mode = "full"\nmax_files = true\n',
            'mode = "full"\nmax_file_bytes = 1.5\n',
            'mode = ',
        )
        for body in cases:
            with self.subTest(body=body):
                self.write_config(body)
                session = telemetry.start_session(["qurtail"], "1.1.0")
                session.finish(0)
                self.assertFalse((self.home / ".qurtail" / "telemetry").exists())

    def test_exact_read_limit_is_accepted_and_larger_config_is_rejected(self) -> None:
        prefix = b'[telemetry]\nmode = "full"\n#'
        config_path = self.write_config()
        exact = prefix + (b"x" * (telemetry.CONFIG_READ_LIMIT - len(prefix)))
        config_path.write_bytes(exact)

        session = telemetry.start_session(["qurtail"], "1.1.0")
        session.finish(0)
        self.assertEqual(len(self.log_lines()), 2)

        telemetry_directory = self.home / ".qurtail" / "telemetry"
        for child in telemetry_directory.iterdir():
            child.unlink()
        telemetry_directory.rmdir()
        config_path.write_bytes(exact + b"x")

        session = telemetry.start_session(["qurtail"], "1.1.0")
        session.finish(0)
        self.assertFalse(telemetry_directory.exists())

    def test_home_and_start_write_failures_are_silent(self) -> None:
        with patch(
            "_qurtail_telemetry._home_directory",
            side_effect=RuntimeError("no home"),
        ):
            telemetry.start_session(["qurtail"], "1.1.0").finish(0)

        self.write_config()
        with patch(
            "_qurtail_telemetry._append_event",
            side_effect=OSError("read only"),
        ) as append:
            telemetry.start_session(["qurtail"], "1.1.0").finish(0)
        self.assertEqual(append.call_count, 1)


class RecordAndLifecycleTests(TelemetryTestCase):
    """Verify complete records and normal CLI completion behavior."""

    def test_start_and_finish_records_are_complete_paired_and_exactly_once(self) -> None:
        self.write_config()
        arguments = ["qurtail", "run", "--", "child", "secret value"]

        session = telemetry.start_session(arguments, "1.1.0")
        session.finish(7)
        session.finish(8)

        lines = self.log_lines()
        self.assertEqual(len(lines), 2)
        start = START_RECORD.fullmatch(lines[0])
        finish = FINISH_RECORD.fullmatch(lines[1])
        assert start is not None and finish is not None
        self.assertEqual(start["invocation"], finish["invocation"])
        self.assertEqual(start["version"], "1.1.0")
        self.assertEqual(int(start["pid"]), os.getpid())
        self.assertEqual(json.loads(start["cwd"]), os.getcwd())
        self.assertEqual(json.loads(start["argv"]), arguments)
        self.assertEqual(start["lossy"], "false")
        self.assertEqual(finish["exit"], "7")
        self.assertGreaterEqual(int(finish["duration_ms"]), 0)
        self.assertRegex(start["timestamp"], r"Z$")
        self.assertRegex(finish["timestamp"], r"Z$")

    def test_control_characters_and_surrogates_stay_on_one_utf8_line(self) -> None:
        self.write_config()
        with patch("_qurtail_telemetry.os.getcwd", return_value="bad\udcff cwd"):
            session = telemetry.start_session(
                ["qurtail", "line\nbreak", "bad\udcff argument"],
                "1.1.0",
            )
        session.finish(0)

        lines = self.log_lines()
        self.assertEqual(len(lines), 2)
        start = START_RECORD.fullmatch(lines[0])
        assert start is not None
        self.assertEqual(start["lossy"], "true")
        self.assertEqual(json.loads(start["cwd"]), "bad\ufffd cwd")
        self.assertEqual(
            json.loads(start["argv"]),
            ["qurtail", "line\nbreak", "bad\ufffd argument"],
        )

    def test_main_records_explicit_arguments_and_normal_system_exits(self) -> None:
        self.write_config()
        output = StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(output):
                qurtail.main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "qurtail 1.1.0\n")
        start = START_RECORD.fullmatch(self.log_lines()[0])
        finish = FINISH_RECORD.fullmatch(self.log_lines()[1])
        assert start is not None and finish is not None
        self.assertEqual(json.loads(start["argv"]), ["qurtail", "--version"])
        self.assertEqual(finish["exit"], "0")

    def test_argument_errors_keep_status_and_stderr_while_finishing(self) -> None:
        self.write_config()
        errors = StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stderr(errors):
                qurtail.main(["--dot-every", "0"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--dot-every must be at least 1", errors.getvalue())
        finish = FINISH_RECORD.fullmatch(self.log_lines()[1])
        assert finish is not None
        self.assertEqual(finish["exit"], "2")

    def test_successful_command_and_child_failure_record_their_return_statuses(self) -> None:
        self.write_config()
        with (
            patch.object(sys, "stdin", StringIO("record\n")),
            patch.object(sys, "stdout", StringIO()),
        ):
            self.assertEqual(qurtail.main([]), 0)

        child_output = StringIO()
        with patch.object(sys, "stdout", child_output):
            self.assertEqual(
                qurtail.main(
                    [
                        "run",
                        "--",
                        sys.executable,
                        "-c",
                        "raise SystemExit(7)",
                    ]
                ),
                7,
            )

        finishes = [
            match
            for line in self.log_lines()
            if (match := FINISH_RECORD.fullmatch(line)) is not None
        ]
        self.assertEqual([match["exit"] for match in finishes], ["0", "7"])

    def test_unexpected_exception_leaves_an_unmatched_start(self) -> None:
        self.write_config()
        with patch("qurtail._main", side_effect=RuntimeError("panic")):
            with self.assertRaisesRegex(RuntimeError, "panic"):
                qurtail.main(["input.log"])

        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertIsNotNone(START_RECORD.fullmatch(lines[0]))

    def test_finish_write_failure_does_not_change_the_command_result(self) -> None:
        self.write_config()
        real_append = telemetry._append_event
        calls = 0

        def fail_finish(config: object, record: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("finish unavailable")
            real_append(config, record)  # type: ignore[arg-type]

        with patch("_qurtail_telemetry._append_event", side_effect=fail_finish):
            with (
                patch.object(sys, "stdin", StringIO("record\n")),
                patch.object(sys, "stdout", StringIO()),
            ):
                status = qurtail.main([])

        self.assertEqual(status, 0)
        self.assertEqual(len(self.log_lines()), 1)


class StorageTests(TelemetryTestCase):
    """Verify rotation, retention, locking, and platform permissions."""

    def config(self, *, max_file_bytes: int, max_files: int) -> telemetry._TelemetryConfig:
        """Create a direct storage configuration for focused tests."""
        return telemetry._TelemetryConfig(
            self.home / ".qurtail" / "telemetry",
            max_file_bytes,
            max_files,
        )

    def test_rotation_happens_before_the_event_after_the_threshold(self) -> None:
        config = self.config(max_file_bytes=5, max_files=3)

        telemetry._append_event(config, "first-event\n")
        self.assertFalse((config.directory / "commands.log.1").exists())
        telemetry._append_event(config, "second-event\n")

        self.assertEqual(
            (config.directory / "commands.log.1").read_text(encoding="utf-8"),
            "first-event\n",
        )
        self.assertEqual(
            (config.directory / "commands.log").read_text(encoding="utf-8"),
            "second-event\n",
        )

    def test_one_file_retention_discards_the_old_active_log(self) -> None:
        config = self.config(max_file_bytes=1, max_files=1)

        telemetry._append_event(config, "old\n")
        telemetry._append_event(config, "new\n")

        self.assertEqual(
            (config.directory / "commands.log").read_text(encoding="utf-8"),
            "new\n",
        )
        self.assertEqual(list(config.directory.glob("commands.log.*")), [])

    def test_lowered_retention_is_applied_without_rotation(self) -> None:
        config = self.config(max_file_bytes=10_000, max_files=3)
        config.directory.mkdir(parents=True)
        for number in range(1, 7):
            (config.directory / f"commands.log.{number}").write_text(
                str(number), encoding="utf-8"
            )

        telemetry._append_event(config, "active\n")

        self.assertTrue((config.directory / "commands.log.1").exists())
        self.assertTrue((config.directory / "commands.log.2").exists())
        self.assertFalse((config.directory / "commands.log.3").exists())
        self.assertFalse((config.directory / "commands.log.6").exists())

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_posix_directory_and_files_are_private(self) -> None:
        config = self.config(max_file_bytes=10_000, max_files=3)

        telemetry._append_event(config, "event\n")

        self.assertEqual(config.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (config.directory / "commands.log").stat().st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            (config.directory / "commands.lock").stat().st_mode & 0o777,
            0o600,
        )

    def test_concurrent_processes_write_only_complete_paired_records(self) -> None:
        self.write_config()
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["USERPROFILE"] = str(self.home)
        command = [sys.executable, str(Path(qurtail.__file__).resolve()), "--version"]

        processes = [
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            for _ in range(8)
        ]
        outcomes = [process.communicate(timeout=10) for process in processes]

        for process, (output, errors) in zip(processes, outcomes):
            self.assertEqual(process.returncode, 0, errors)
            self.assertEqual(output, "qurtail 1.1.0\n")
        lines = self.log_lines()
        self.assertEqual(len(lines), 16)
        start_matches = [
            match
            for line in lines
            if (match := START_RECORD.fullmatch(line)) is not None
        ]
        starts = {
            match["invocation"]
            for match in start_matches
        }
        finishes = {
            match["invocation"]
            for line in lines
            if (match := FINISH_RECORD.fullmatch(line)) is not None
        }
        self.assertEqual(starts, finishes)
        self.assertEqual(len(starts), 8)
        self.assertTrue(
            all(
                json.loads(match["argv"])
                == [str(Path(qurtail.__file__).resolve()), "--version"]
                for match in start_matches
            )
        )

    def test_held_lock_is_bounded_and_silently_drops_the_invocation(self) -> None:
        self.write_config()
        directory = self.home / ".qurtail" / "telemetry"
        directory.mkdir(parents=True)
        lock_path = directory / "commands.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        telemetry._try_lock(descriptor)
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["USERPROFILE"] = str(self.home)
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, str(Path(qurtail.__file__).resolve()), "--version"],
                capture_output=True,
                text=True,
                env=environment,
                timeout=5,
                check=False,
            )
        finally:
            telemetry._unlock(descriptor)
            os.close(descriptor)

        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "qurtail 1.1.0\n")
        self.assertGreaterEqual(elapsed, 0.20)
        self.assertLess(elapsed, 2.0)
        self.assertFalse((directory / "commands.log").exists())


if __name__ == "__main__":
    unittest.main()
