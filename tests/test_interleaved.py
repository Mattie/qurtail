"""Verify shared repeat counts and the adjacent-only escape hatch."""

from io import StringIO
import json
import unittest
from unittest.mock import patch

import qurtail
from qurtail import _StreamReducer


A = "INFO service-a completed its scheduled cache refresh"
B = "INFO service-b completed its scheduled cache refresh"


def render(lines: list[str], **options: object) -> str:
    """Render finite input with deterministic summary timing."""
    output = StringIO()
    reducer = _StreamReducer(output, clock=lambda: 0.0, **options)
    for line in lines:
        reducer.process(line)
    reducer.finish()
    return output.getvalue()


class InterleavingTests(unittest.TestCase):
    """Exercise the public output contract rather than a reference protocol."""

    def test_irregular_familiar_messages_share_one_count(self):
        self.assertEqual(render([A, B, A, B, B, A, A]),
                         f"{A}\n{B}\n..... [5 similar before stop]\n")

    def test_short_patterns_use_the_same_dots_and_counts(self):
        self.assertEqual(render(["A", "B", "A", "B", "A"]),
                         "A\nB\n... [3 similar before stop]\n")

    def test_unfamiliar_changes_close_counts_before_printing_immediately(self):
        output = StringIO()
        reducer = _StreamReducer(output, clock=lambda: 0.0)
        for line in [A, B, A, "INFO timeout: 400", B, A, "INFO timeout: 800"]:
            reducer.process(line)
        self.assertEqual(output.getvalue(),
                         f"{A}\n{B}\n. [1 similar in 0s]\nINFO timeout: 400\n"
                         ".. [2 similar in 0s]\nINFO timeout: 800\n")
        reducer.finish()

    def test_interleaving_uses_existing_timestamp_and_request_id_normalization(self):
        a = "2026-09-10T12:00:00Z INFO service-a completed request_id=aaa"
        changed = a.replace("12:00:00", "12:00:09").replace("id=aaa", "id=bbb")
        self.assertEqual(render([a, B, changed]), f"{a}\n{B}\n. [1 similar before stop]\n")

    def test_json_metadata_normalization_and_error_payload_protection_are_shared(self):
        a = {"timestamp": "2026-09-10T12:00:00Z", "requestId": "aaa", "error": {"message": "disk full"}}
        repeat = {**a, "timestamp": "2026-09-10T12:00:01Z", "requestId": "bbb"}
        changed = {**repeat, "error": {"message": "permission denied"}}
        lines = [json.dumps(item) for item in (a, repeat, changed)]
        self.assertEqual(render([lines[0], B, lines[1], lines[2]]),
                         f"{lines[0]}\n{B}\n. [1 similar in 0s]\n{lines[2]}\n")

    def test_aggressive_option_applies_to_nonadjacent_matches(self):
        a, changed = "INFO scanning /srv/one", "INFO scanning /srv/two"
        self.assertEqual(render([a, B, changed]), f"{a}\n{B}\n{changed}\n")
        self.assertEqual(render([a, B, changed], aggressive=True),
                         f"{a}\n{B}\n. [1 similar before stop]\n")

    def test_familiar_recovery_is_a_dot_and_opt_out_restores_the_full_line(self):
        healthy, failed = "INFO worker healthy", "ERROR worker failed"
        lines = [healthy, failed, healthy, healthy]
        self.assertEqual(render(lines), f"{healthy}\n{failed}\n.. [2 similar before stop]\n")
        self.assertEqual(render(lines, interleaving=False),
                         f"{healthy}\n{failed}\n{healthy}\n. [1 similar before stop]\n")

    def test_opt_out_retains_original_alternation_and_adjacent_counts(self):
        self.assertEqual(render([A, B, A, A, B], interleaving=False),
                         f"{A}\n{B}\n{A}\n. [1 similar in 0s]\n{B}\n")

    def test_adjacent_only_streams_have_identical_output_in_both_modes(self):
        lines = [A] * 17 + [B] * 13 + ["INFO finished"]
        for dot_every in (1, 3, 10):
            with self.subTest(dot_every=dot_every):
                self.assertEqual(render(lines, dot_every=dot_every),
                                 render(lines, dot_every=dot_every, interleaving=False))

    def test_suppressed_matches_do_not_refresh_the_bounded_pattern_index(self):
        for lines in (["first", "second", "third"],
                      [f"2026-09-10T12:00:00Z INFO service-{i} request_id=aaa" for i in range(3)]):
            with self.subTest(lines=lines):
                a, b, c = lines
                self.assertEqual(render([a, b, a, c, a], pattern_limit=2),
                                 f"{a}\n{b}\n. [1 similar in 0s]\n{c}\n{a}\n")
                self.assertEqual(render([a, b, a, c, a], pattern_limit=2, interleaving=False),
                                 f"{a}\n{b}\n{a}\n{c}\n{a}\n")

    def test_alternating_fast_patterns_compile_once_while_retained(self):
        lines = [f"2026-09-10T12:00:00Z INFO service-{i} request_id=aaa" for i in range(600)]
        for interleaving in (True, False):
            with self.subTest(interleaving=interleaving):
                with patch("qurtail._compile_fast_text_matcher",
                           wraps=qurtail._compile_fast_text_matcher) as compile_matcher:
                    output = render(lines * 3, interleaving=interleaving)
                self.assertEqual(compile_matcher.call_count, 600)
                if interleaving:
                    self.assertTrue(output.startswith("\n".join(lines) + "\n"))
                    self.assertTrue(output.endswith("[1200 similar before stop]\n"))
                else:
                    self.assertEqual(output, "\n".join(lines * 3) + "\n")

    def test_protected_content_stays_full_between_repeat_counts(self):
        for protected in (["{malformed"] * 2, ["  uncertain content"] * 2,
                          ["Traceback (most recent call last):", '  File "job.py", line 1', "ValueError: failed"]):
            with self.subTest(protected=protected):
                expected = f"{A}\n{B}\n. [1 similar in 0s]\n" + "\n".join(protected) + "\n"
                self.assertEqual(render([A, B, A] + protected), expected)

    def test_mixed_dots_obey_live_and_periodic_deadlines(self):
        now = [0.0]
        output = StringIO()
        reducer = _StreamReducer(output, clock=lambda: now[0])
        for line in [A, B, A, B, A]:
            reducer.process(line)
        self.assertEqual(output.getvalue(), f"{A}\n{B}\n")
        now[0] = 1.0
        reducer.tick()
        self.assertEqual(output.getvalue(), f"{A}\n{B}\n...")
        now[0] = 30.0
        reducer.tick()
        reducer.process(B)
        reducer.finish()
        self.assertEqual(output.getvalue(),
                         f"{A}\n{B}\n... [3 similar in 30s]\n. [1 similar before stop]\n")


if __name__ == "__main__":
    unittest.main()
