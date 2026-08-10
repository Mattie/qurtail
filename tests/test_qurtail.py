"""Verify qurtail's conservative matching, streaming output, and CLI contract."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import qurtail
from qurtail import _StreamReducer, _signature, main


class FakeClock:
    """Provide explicit logical time to streaming tests."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class SignalingBuffer(StringIO):
    """Signal when a live timer writes a closing summary."""

    def __init__(self) -> None:
        super().__init__()
        self.summary_written = threading.Event()

    def write(self, text: str) -> int:
        result = super().write(text)
        if "similar in" in text:
            self.summary_written.set()
        return result


class StreamReducerTests(unittest.TestCase):
    """Keep compression exact, bounded, fail-open, and line oriented."""

    def run_stream(
        self, lines: list[str], **options: object
    ) -> tuple[str, _StreamReducer]:
        output = StringIO()
        reducer = _StreamReducer(output, **options)
        for line in lines:
            reducer.process(line)
        reducer.finish()
        return output.getvalue(), reducer

    def test_first_example_is_visible_and_known_volatile_values_repeat(self) -> None:
        first = (
            "2026-08-08T12:00:00Z INFO refreshed cache "
            "request_id=715a"
        )
        repeat = (
            "2026-08-08T12:00:01Z INFO refreshed cache "
            "request_id=82bf"
        )

        output, _ = self.run_stream([first, repeat])

        self.assertEqual(
            output,
            first + "\n. [1 similar before stop]\n",
        )

    def test_input_byte_order_mark_is_removed_from_the_first_record(self) -> None:
        output, _ = self.run_stream(["\ufeffheartbeat", "heartbeat"])

        self.assertEqual(output, "heartbeat\n. [1 similar before stop]\n")

    def test_uuid_values_are_recognized_without_hiding_other_changes(self) -> None:
        first = "INFO completed request 21bb6a55-7354-4b15-9d8d-42ff1e84b89d"
        repeat = "INFO completed request 564e5727-9465-44bb-8679-0c1465036a3b"
        changed = "INFO failed request 4a1e5ce4-38dd-46ae-9f47-dd76c7d033b3"

        output, _ = self.run_stream([first, repeat, changed])

        self.assertEqual(
            output,
            first + "\n. [1 similar in 0s]\n" + changed + "\n",
        )

    def test_unknown_numeric_changes_remain_visible(self) -> None:
        lines = [
            "replication lag is 1 second",
            "replication lag is 900 seconds",
            "worker 17 finished batch 41",
            "worker 18 finished batch 42",
        ]

        output, _ = self.run_stream(lines)

        self.assertEqual(output, "".join(line + "\n" for line in lines))

    def test_container_prefixes_and_timestamps_keep_the_payload_pattern(self) -> None:
        first = (
            "2026-08-08T12:00:00Z stdout F api | refreshed "
            "request_id=abc"
        )
        repeat = (
            "2026-08-08T12:00:01Z stdout F api | refreshed "
            "request_id=def"
        )

        output, _ = self.run_stream([first, repeat])

        self.assertIn(first + "\n", output)
        self.assertNotIn(repeat, output)
        self.assertIn("[1 similar before stop]", output)

    def test_json_metadata_and_ids_are_volatile(self) -> None:
        first = (
            '{"time":1,"pid":14,"hostname":"one","level":"info",'
            '"requestId":"abc","message":"cache refreshed"}'
        )
        repeat = (
            '{"time":2,"pid":19,"hostname":"two","level":"info",'
            '"requestId":"def","message":"cache refreshed"}'
        )

        output, _ = self.run_stream([first, repeat])

        self.assertEqual(output, first + "\n. [1 similar before stop]\n")

    def test_severity_and_event_changes_in_json_remain_visible(self) -> None:
        info = (
            '{"time":1,"level":"info","eventId":1000,'
            '"message":"worker heartbeat"}'
        )
        warning = (
            '{"time":2,"level":"warning","eventId":1001,'
            '"message":"worker heartbeat"}'
        )

        output, _ = self.run_stream([info, warning])

        self.assertEqual(output, info + "\n" + warning + "\n")

    def test_http_status_changes_remain_visible(self) -> None:
        ok = (
            '127.0.0.1 [07/Aug/2026:09:30:00 -0500] '
            '"GET /health HTTP/1.1" 200 17'
        )
        ok_repeat = (
            '127.0.0.1 [07/Aug/2026:09:30:01 -0500] '
            '"GET /health HTTP/1.1" 200 17'
        )
        failed = (
            '127.0.0.1 [07/Aug/2026:09:30:02 -0500] '
            '"GET /health HTTP/1.1" 500 17'
        )

        output, _ = self.run_stream([ok, ok_repeat, failed])

        self.assertEqual(
            output,
            ok + "\n. [1 similar in 0s]\n" + failed + "\n",
        )

    def test_changed_error_payloads_stay_visible_and_identical_errors_repeat(self) -> None:
        disk_full = (
            '{"timestamp":"2026-08-08T12:00:00Z","level":"error",'
            '"error":{"message":"disk full","code":"EIO"}}'
        )
        disk_full_repeat = (
            '{"timestamp":"2026-08-08T12:00:01Z","level":"error",'
            '"error":{"message":"disk full","code":"EIO"}}'
        )
        permission = (
            '{"timestamp":"2026-08-08T12:00:02Z","level":"error",'
            '"error":{"message":"permission denied","code":"EACCES"}}'
        )

        output, _ = self.run_stream(
            [disk_full, disk_full_repeat, permission]
        )

        self.assertEqual(
            output,
            disk_full + "\n. [1 similar in 0s]\n" + permission + "\n",
        )

    def test_malformed_and_unknown_structured_records_fail_open(self) -> None:
        lines = ["{malformed", "{malformed", "[1, 2]", "[1, 2]"]

        output, _ = self.run_stream(lines)

        self.assertEqual(output, "".join(line + "\n" for line in lines))

    def test_deeply_nested_json_fails_open(self) -> None:
        line = '{"value":' + "[" * 1_200 + "0" + "]" * 1_200 + "}"

        output, _ = self.run_stream([line, line])

        self.assertEqual(output, line + "\n" + line + "\n")

    def test_multiline_diagnostics_remain_complete(self) -> None:
        lines = [
            "Traceback (most recent call last):",
            '  File "worker.py", line 10, in run',
            "    refresh()",
            "ValueError: cache key missing",
            "Traceback (most recent call last):",
            '  File "worker.py", line 10, in run',
            "    refresh()",
            "ValueError: cache key missing",
            "2026-08-08T12:01:00Z INFO worker recovered",
        ]

        output, _ = self.run_stream(lines)

        self.assertEqual(output, "".join(line + "\n" for line in lines))

    def test_suppressed_records_do_not_extend_the_pattern_index(self) -> None:
        output, reducer = self.run_stream(
            ["first", "second", "first", "third", "first"],
            pattern_limit=2,
        )

        self.assertEqual(
            output,
            "first\nsecond\nfirst\nthird\nfirst\n",
        )
        self.assertEqual(len(reducer._patterns), 2)

    def test_dots_are_buffered_into_short_runs(self) -> None:
        output = StringIO()
        reducer = _StreamReducer(output)
        reducer.process("heartbeat")
        for _ in range(7):
            reducer.process("heartbeat")
        self.assertEqual(output.getvalue(), "heartbeat\n")

        reducer.process("heartbeat")

        self.assertEqual(output.getvalue(), "heartbeat\n........")
        reducer.finish()
        self.assertEqual(
            output.getvalue(),
            "heartbeat\n........ [8 similar before stop]\n",
        )

    def test_dot_every_keeps_an_exact_closing_count(self) -> None:
        output, _ = self.run_stream(
            ["heartbeat"] * 9,
            dot_every=3,
        )

        self.assertEqual(
            output,
            "heartbeat\n.. [8 similar before stop]\n",
        )

    def test_pattern_change_closes_the_run_with_elapsed_time(self) -> None:
        clock = FakeClock()
        output = StringIO()
        reducer = _StreamReducer(output, clock=clock)
        reducer.process("heartbeat")
        clock.now = 1.0
        reducer.process("heartbeat")
        clock.now = 2.9
        reducer.process("ERROR connection refused")
        reducer.finish()

        self.assertEqual(
            output.getvalue(),
            "heartbeat\n. [1 similar in 1s]\nERROR connection refused\n",
        )

    def test_logical_clock_closes_a_run_at_the_summary_interval(self) -> None:
        clock = FakeClock()
        output = StringIO()
        reducer = _StreamReducer(
            output,
            clock=clock,
            summary_interval=30.0,
        )
        reducer.process("heartbeat")
        reducer.process("heartbeat")
        clock.now = 30.0

        reducer.tick()
        reducer.finish()

        self.assertEqual(
            output.getvalue(),
            "heartbeat\n. [1 similar in 30s]\n",
        )

    def test_live_timer_closes_an_idle_run(self) -> None:
        output = SignalingBuffer()
        reducer = _StreamReducer(
            output,
            summary_interval=0.02,
            live_timer=True,
        )
        reducer.process("heartbeat")
        reducer.process("heartbeat")

        self.assertTrue(output.summary_written.wait(0.5))
        reducer.finish()
        self.assertIn(". [1 similar in", output.getvalue())

    def test_summaries_never_use_terminal_rewrite_controls(self) -> None:
        output, _ = self.run_stream(["heartbeat", "heartbeat"])

        self.assertNotIn("\b", output)
        self.assertNotIn("\r", output)

    def test_process_after_finish_is_rejected(self) -> None:
        reducer = _StreamReducer(StringIO())
        reducer.finish()

        with self.assertRaisesRegex(RuntimeError, "after finish"):
            reducer.process("late")

    def test_signature_only_normalizes_named_volatile_values(self) -> None:
        self.assertEqual(
            _signature("request_id=abc lag=1"),
            _signature("request_id=def lag=1"),
        )
        self.assertNotEqual(
            _signature("request_id=abc lag=1"),
            _signature("request_id=def lag=900"),
        )

    def test_fast_timestamp_id_shape_preserves_every_stable_segment(self) -> None:
        baseline = (
            "2026-08-08T12:00:00Z INFO refreshed cache "
            "request_id=abc result=ready"
        )
        repeat = (
            "2026-08-08T12:00:01Z INFO refreshed cache "
            "request_id=def result=ready"
        )
        changed = (
            "2026-08-08T12:00:02Z INFO refreshed cache "
            "request_id=ghi result=failed"
        )

        output, _ = self.run_stream([baseline, repeat, changed])

        self.assertEqual(
            output,
            baseline + "\n. [1 similar in 0s]\n" + changed + "\n",
        )

    def test_multiple_named_ids_and_uuid_values_use_complete_normalization(self) -> None:
        first = (
            "2026-08-08T12:00:00Z INFO request_id=abc trace_id=def "
            "job=21bb6a55-7354-4b15-9d8d-42ff1e84b89d"
        )
        repeat = (
            "2026-08-08T12:00:01Z INFO request_id=ghi trace_id=jkl "
            "job=564e5727-9465-44bb-8679-0c1465036a3b"
        )

        output, _ = self.run_stream([first, repeat])

        self.assertEqual(output, first + "\n. [1 similar before stop]\n")

    def test_clock_shaped_state_values_remain_visible(self) -> None:
        first = "replication lag is 00:00:01"
        changed = "replication lag is 00:15:00"

        output, _ = self.run_stream([first, changed])

        self.assertEqual(output, first + "\n" + changed + "\n")

    def test_leading_clock_before_a_level_is_normalized(self) -> None:
        first = "12:00:00 INFO worker heartbeat"
        repeat = "12:00:01 INFO worker heartbeat"

        output, _ = self.run_stream([first, repeat])

        self.assertEqual(output, first + "\n. [1 similar before stop]\n")

    def test_invalid_leading_clocks_fail_open(self) -> None:
        first = "99:99:98 INFO worker heartbeat"
        changed = "99:99:99 INFO worker heartbeat"

        output, _ = self.run_stream([first, changed])

        self.assertEqual(output, first + "\n" + changed + "\n")

    def test_oversized_signatures_fail_open(self) -> None:
        stable = "x" * 1100
        first = f"2026-08-08T12:00:00Z INFO {stable} request_id=abc"
        changed = f"2026-08-08T12:00:01Z INFO {stable} request_id=def"

        output, _ = self.run_stream([first, changed])

        self.assertEqual(output, first + "\n" + changed + "\n")

    def test_resumed_pattern_prints_a_new_exemplar_after_a_diagnostic(self) -> None:
        ordinary = "2026-08-08T12:00:00Z INFO heartbeat request_id=abc"
        repeat = "2026-08-08T12:00:01Z INFO heartbeat request_id=def"
        error = "2026-08-08T12:00:02Z ERROR connection refused"
        resumed = "2026-08-08T12:00:03Z INFO heartbeat request_id=ghi"

        output, _ = self.run_stream([ordinary, repeat, error, resumed, resumed])

        self.assertEqual(
            output,
            ordinary
            + "\n. [1 similar in 0s]\n"
            + error
            + "\n"
            + resumed
            + "\n. [1 similar before stop]\n",
        )


class CommandTests(unittest.TestCase):
    """Verify the small 1.0 command surface and tail behavior."""

    @staticmethod
    def stop_runner(process: subprocess.Popen[str]) -> None:
        """Prevent a failed integration assertion from leaving child processes."""
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def test_file_input_uses_the_last_ten_lines_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            path.write_text(
                "".join(f"line {number}\n" for number in range(15)),
                encoding="utf-8",
            )
            output = StringIO()

            with patch.object(sys, "stdout", output):
                status = main([str(path)])

        self.assertEqual(status, 0)
        self.assertNotIn("line 4\n", output.getvalue())
        self.assertTrue(output.getvalue().startswith("line 5\n"))
        self.assertTrue(output.getvalue().endswith("line 14\n"))

    def test_lines_option_controls_existing_file_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            output = StringIO()

            with patch.object(sys, "stdout", output):
                status = main(["-n", "2", str(path)])

        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), "two\nthree\n")

    def test_stdin_is_read_until_the_stream_closes(self) -> None:
        output = StringIO()
        with (
            patch.object(sys, "stdin", StringIO("one\none\ntwo\n")),
            patch.object(sys, "stdout", output),
        ):
            status = main([])

        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue(),
            "one\n. [1 similar in 0s]\ntwo\n",
        )

    def test_runner_compacts_combined_output_captures_raw_bytes_and_returns_status(
        self,
    ) -> None:
        script = (
            "import os, sys; "
            "os.write(1, b'heartbeat\\n'); "
            "os.write(2, b'heartbeat\\n'); "
            "sys.exit(7)"
        )
        with tempfile.TemporaryDirectory() as temporary:
            raw_log = Path(temporary) / "child.raw.log"
            output = StringIO()

            with patch.object(sys, "stdout", output):
                status = main(
                    [
                        "run",
                        "--raw-log",
                        str(raw_log),
                        "--",
                        sys.executable,
                        "-c",
                        script,
                    ]
                )

            raw_bytes = raw_log.read_bytes()

        self.assertEqual(status, 7)
        self.assertEqual(output.getvalue(), "heartbeat\n. [1 similar before stop]\n")
        self.assertEqual(raw_bytes, b"heartbeat\nheartbeat\n")

    def test_runner_passes_arguments_without_shell_interpretation(self) -> None:
        script = "import json, sys; print(json.dumps(sys.argv[1:]))"
        output = StringIO()

        with patch.object(sys, "stdout", output):
            status = main(
                [
                    "run",
                    "--",
                    sys.executable,
                    "-c",
                    script,
                    "left;right",
                    "$(echo nope)",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), '["left;right", "$(echo nope)"]\n')

    def test_runner_requires_a_child_command(self) -> None:
        errors = StringIO()

        with self.assertRaises(SystemExit) as raised:
            with redirect_stderr(errors):
                main(["run", "--"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("a command is required after --", errors.getvalue())

    def test_interrupt_forwarding_reaps_the_child_and_maps_signal_status(self) -> None:
        class FakeProcess:
            pid = 123
            returncode = None

            def __init__(self) -> None:
                self.wait_calls: list[float | None] = []

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls.append(timeout)
                self.returncode = -qurtail.signal.SIGINT
                return self.returncode

            def send_signal(self, _: int) -> None:
                raise AssertionError("POSIX forwarding should target the process group")

            def terminate(self) -> None:
                raise AssertionError("a cooperative child should not be terminated")

            def kill(self) -> None:
                raise AssertionError("a cooperative child should not be killed")

        process = FakeProcess()
        with (
            patch.object(qurtail.os, "name", "posix"),
            patch("qurtail.os.killpg", create=True) as kill_group,
            patch("qurtail._wait_for_process_group", return_value=True),
        ):
            return_code = qurtail._interrupt_child(process)

        self.assertEqual(return_code, -qurtail.signal.SIGINT)
        self.assertEqual(
            qurtail._child_exit_status(
                return_code,
                requested_signal=qurtail.signal.SIGINT,
            ),
            130,
        )
        kill_group.assert_called_once_with(process.pid, qurtail.signal.SIGINT)
        self.assertEqual(process.wait_calls, [None])

    def test_windows_interrupt_uses_control_break_and_reaps_the_child(self) -> None:
        class FakeProcess:
            returncode = None

            def __init__(self) -> None:
                self.signals: list[int] = []
                self.wait_calls: list[float | None] = []

            def poll(self) -> None:
                return None

            def send_signal(self, event: int) -> None:
                self.signals.append(event)

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls.append(timeout)
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                raise AssertionError("a cooperative child should not be terminated")

            def kill(self) -> None:
                raise AssertionError("a cooperative child should not be killed")

        process = FakeProcess()
        control_break = 1
        with (
            patch.object(qurtail.os, "name", "nt"),
            patch.object(
                qurtail.signal,
                "CTRL_BREAK_EVENT",
                control_break,
                create=True,
            ),
        ):
            return_code = qurtail._interrupt_child(process)

        self.assertEqual(return_code, 0)
        self.assertEqual(
            qurtail._child_exit_status(
                return_code,
                requested_signal=qurtail.signal.SIGINT,
            ),
            130,
        )
        self.assertEqual(process.signals, [control_break])
        self.assertEqual(process.wait_calls, [qurtail.INTERRUPT_GRACE_SECONDS])

    def test_runner_drains_cleanup_output_after_forwarding_an_interrupt(self) -> None:
        class InterruptingStream:
            def __init__(self) -> None:
                self.iterations = 0
                self.closed = False

            def __iter__(self):
                self.iterations += 1
                if self.iterations == 1:
                    raise KeyboardInterrupt
                return iter([b"cleanup complete\n"])

            def close(self) -> None:
                self.closed = True

        class FakeProcess:
            pid = 123
            returncode = None

            def __init__(self) -> None:
                self.stdout = InterruptingStream()

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = -qurtail.signal.SIGINT
                return self.returncode

            def terminate(self) -> None:
                raise AssertionError("a cooperative child should not be terminated")

            def kill(self) -> None:
                raise AssertionError("a cooperative child should not be killed")

        process = FakeProcess()
        output = StringIO()
        reducer = _StreamReducer(output)
        with tempfile.TemporaryDirectory() as temporary:
            raw_log = Path(temporary) / "child.raw.log"
            with (
                patch.object(qurtail.os, "name", "posix"),
                patch("qurtail.os.killpg", create=True),
                patch("qurtail._wait_for_process_group", return_value=True),
                patch("qurtail.subprocess.Popen", return_value=process),
            ):
                status = qurtail._run_child_command(
                    ["child"],
                    reducer,
                    raw_log=raw_log,
                )
            reducer.finish()
            raw_bytes = raw_log.read_bytes()

        self.assertEqual(status, 130)
        self.assertEqual(output.getvalue(), "cleanup complete\n")
        self.assertEqual(raw_bytes, b"cleanup complete\n")
        self.assertTrue(process.stdout.closed)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group behavior")
    def test_runner_drains_cleanup_larger_than_the_child_pipe(self) -> None:
        payload_size = 512 * 1024
        child = """
import os
from pathlib import Path
import signal
import sys
import time

payload = b"x" * int(sys.argv[2]) + b"\\n"

def stop(_signum, _frame):
    remaining = payload
    while remaining:
        remaining = remaining[os.write(1, remaining):]
    raise SystemExit(0)

signal.signal(signal.SIGINT, stop)
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
while True:
    time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "ready"
            raw_log = root / "child.raw.log"
            runner = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(qurtail.__file__).resolve()),
                    "run",
                    "--raw-log",
                    str(raw_log),
                    "--",
                    sys.executable,
                    "-c",
                    child,
                    str(ready),
                    str(payload_size),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(self.stop_runner, runner)
            deadline = time.monotonic() + 5
            while not ready.exists() and runner.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail("child did not become ready")
                time.sleep(0.01)
            os.kill(runner.pid, signal.SIGINT)
            _, errors = runner.communicate(timeout=10)

            self.assertEqual(runner.returncode, 130, errors)
            self.assertEqual(raw_log.stat().st_size, payload_size + 1)

    @unittest.skipUnless(os.name == "posix", "POSIX signal forwarding")
    def test_sigterm_stops_the_runner_and_its_child(self) -> None:
        child = """
from pathlib import Path
import os
import sys
import time

Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "child.pid"
            runner = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(qurtail.__file__).resolve()),
                    "run",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                    str(pid_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(self.stop_runner, runner)
            deadline = time.monotonic() + 5
            while not pid_path.exists() and runner.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail("child did not record its process id")
                time.sleep(0.01)
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            os.kill(runner.pid, signal.SIGTERM)
            _, errors = runner.communicate(timeout=10)

            self.assertEqual(runner.returncode, 143, errors)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_follow_aliases_share_rotation_safe_file_following(self) -> None:
        for flag in ("-f", "-F"):
            with self.subTest(flag=flag):
                output = StringIO()
                with (
                    patch.object(sys, "stdout", output),
                    patch(
                        "qurtail._follow_file",
                        side_effect=KeyboardInterrupt,
                    ) as follow,
                ):
                    status = main([flag, "-n", "50", "app.log"])

                self.assertEqual(status, 0)
                follow.assert_called_once()
                self.assertEqual(follow.call_args.args[0], Path("app.log"))
                self.assertEqual(follow.call_args.args[1], 50)

    def test_file_follower_handles_append_truncation_and_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            replacement = Path(temporary) / "replacement.log"
            rotated = Path(temporary) / "app.log.1"
            path.write_text("one\n", encoding="utf-8")
            output = StringIO()
            reducer = _StreamReducer(output)
            sleep_calls = 0

            def advance_file(_: float) -> None:
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    with path.open("a", encoding="utf-8") as destination:
                        destination.write("two\n")
                elif sleep_calls == 2:
                    path.write_text("three\n", encoding="utf-8")
                elif sleep_calls == 3:
                    replacement.write_text("four\n", encoding="utf-8")
                    path.replace(rotated)
                    replacement.replace(path)
                else:
                    raise KeyboardInterrupt

            with patch("qurtail.time.sleep", side_effect=advance_file):
                with self.assertRaises(KeyboardInterrupt):
                    qurtail._follow_file(path, 10, reducer)
            reducer.finish()

        self.assertEqual(output.getvalue(), "one\ntwo\nthree\nfour\n")

    def test_file_follower_rewinds_after_a_larger_copy_truncate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            path.write_text("old!\n", encoding="utf-8")
            output = StringIO()
            reducer = _StreamReducer(output)
            sleep_calls = 0

            def rewrite_file(_: float) -> None:
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    path.write_text("newer!\n", encoding="utf-8")
                else:
                    raise KeyboardInterrupt

            with patch("qurtail.time.sleep", side_effect=rewrite_file):
                with self.assertRaises(KeyboardInterrupt):
                    qurtail._follow_file(path, 10, reducer)
            reducer.finish()

        self.assertEqual(output.getvalue(), "old!\nnewer!\n")

    def test_file_follower_detects_rewrite_with_unchanged_final_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            path.write_text("old\nsame\n", encoding="utf-8")
            output = StringIO()
            reducer = _StreamReducer(output)
            sleep_calls = 0

            def rewrite_file(_: float) -> None:
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    path.write_text("new\nsame\n", encoding="utf-8")
                else:
                    raise KeyboardInterrupt

            with patch("qurtail.time.sleep", side_effect=rewrite_file):
                with self.assertRaises(KeyboardInterrupt):
                    qurtail._follow_file(path, 10, reducer)
            reducer.finish()

        self.assertEqual(output.getvalue(), "old\nsame\nnew\nsame\n")

    def test_file_follower_detects_changed_partial_final_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            path.write_text("same\nold-part", encoding="utf-8")
            output = StringIO()
            reducer = _StreamReducer(output)
            sleep_calls = 0

            def rewrite_file(_: float) -> None:
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    path.write_text("same\nnew-part-long", encoding="utf-8")
                else:
                    raise KeyboardInterrupt

            with patch("qurtail.time.sleep", side_effect=rewrite_file):
                with self.assertRaises(KeyboardInterrupt):
                    qurtail._follow_file(path, 10, reducer)
            reducer.finish()

        self.assertEqual(
            output.getvalue(),
            "same\nold-part\nsame\nnew-part-long\n",
        )

    def test_file_follower_with_zero_context_detects_equal_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            prefix = "one\ntwo\nthree\nfour\n"
            path.write_text(prefix + "old!\n", encoding="utf-8")
            output = StringIO()
            reducer = _StreamReducer(output)
            sleep_calls = 0

            def rewrite_file(_: float) -> None:
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    path.write_text(prefix + "new!\n", encoding="utf-8")
                else:
                    raise KeyboardInterrupt

            with patch("qurtail.time.sleep", side_effect=rewrite_file):
                with self.assertRaises(KeyboardInterrupt):
                    qurtail._follow_file(path, 0, reducer)
            reducer.finish()

        self.assertEqual(output.getvalue(), prefix + "new!\n")

    def test_file_follower_with_zero_context_detects_larger_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.log"
            prefix = "one\ntwo\nthree\nfour\n"
            path.write_text(prefix + "old!\n", encoding="utf-8")
            output = StringIO()
            reducer = _StreamReducer(output)
            sleep_calls = 0

            def rewrite_file(_: float) -> None:
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls == 1:
                    path.write_text(prefix + "newer!\n", encoding="utf-8")
                else:
                    raise KeyboardInterrupt

            with patch("qurtail.time.sleep", side_effect=rewrite_file):
                with self.assertRaises(KeyboardInterrupt):
                    qurtail._follow_file(path, 0, reducer)
            reducer.finish()

        self.assertEqual(output.getvalue(), prefix + "newer!\n")

    def test_help_describes_only_the_supported_command_surface(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit) as raised:
            with patch.object(sys, "stdout", output):
                main(["-h"])

        self.assertEqual(raised.exception.code, 0)
        help_text = output.getvalue()
        for option in ("-f", "-F", "-n", "--dot-every", "qurtail run", "--raw-log"):
            self.assertIn(option, help_text)
        for removed in ("--similarity", "--history", "--config", "--mode"):
            self.assertNotIn(removed, help_text)

    def test_removed_options_are_rejected(self) -> None:
        for option in ("--similarity", "--history", "--config", "--mode"):
            with self.subTest(option=option):
                errors = StringIO()
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stderr(errors):
                        main([option, "value"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("unrecognized arguments", errors.getvalue())

    def test_invalid_numeric_options_report_cli_errors(self) -> None:
        for arguments, message in (
            (["-n", "-1", "app.log"], "--lines must be zero or greater"),
            (["--dot-every", "0"], "--dot-every must be at least 1"),
            (["run", "--dot-every", "0", "--", "child"], "--dot-every must be at least 1"),
        ):
            with self.subTest(arguments=arguments):
                errors = StringIO()
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stderr(errors):
                        main(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, errors.getvalue())

    def test_version_matches_the_package_release(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit) as raised:
            with patch.object(sys, "stdout", output):
                main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "qurtail 1.0.0\n")

    def test_previous_python_api_is_removed(self) -> None:
        self.assertFalse(hasattr(qurtail, "QurTail"))
        self.assertFalse(hasattr(qurtail, "compress"))


if __name__ == "__main__":
    unittest.main()
