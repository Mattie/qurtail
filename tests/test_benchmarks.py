"""Verify held-out monitoring episodes without optional token dependencies."""

from __future__ import annotations

import json
import re
import unittest

from benchmarks.run_benchmarks import (
    EPISODES,
    ROOT,
    SKILL_PATH,
    _qurtail_output,
    evaluate_logical_clock_replay,
    evaluate_suite,
)


DISCOVERY_SKILL_PATH = (
    ROOT / ".agents" / "skills" / "qurtail-fluency" / "SKILL.md"
)
SKILL_UI_PATH = SKILL_PATH.parent / "agents" / "openai.yaml"
DISCOVERY_SKILL_UI_PATH = DISCOVERY_SKILL_PATH.parent / "agents" / "openai.yaml"


def _portable_token_count(text: str) -> int:
    """Provide a stable dependency-free token proxy for regression tests."""
    return len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE))


class MonitoringEpisodeTests(unittest.TestCase):
    """Keep safety gates independent from the tuned matcher fixtures."""

    def test_agent_skill_is_short_and_uses_the_shipped_cli(self) -> None:
        skill = SKILL_PATH.read_text(encoding="utf-8")
        words = re.findall(r"\b[\w-]+\b", skill, re.UNICODE)

        self.assertEqual(
            DISCOVERY_SKILL_PATH.read_text(encoding="utf-8"),
            skill,
        )
        self.assertEqual(
            DISCOVERY_SKILL_UI_PATH.read_bytes(),
            SKILL_UI_PATH.read_bytes(),
        )
        self.assertLess(len(words), 200)
        for command in (
            "qurtail -F -n 50 app.log",
            "qurtail run -- COMMAND...",
            "qurtail run --raw-log app.raw.log -- COMMAND...",
            "--dot-every 10",
            "tee app.raw.log | qurtail",
        ):
            self.assertIn(command, skill)
        for removed_option in ("--similarity", "--history", "--config", "--mode"):
            self.assertNotIn(removed_option, skill)

    def test_historical_paired_agent_report_is_not_current_release_evidence(self) -> None:
        report = json.loads(
            (ROOT / "benchmarks" / "paired-agent-results.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["status"], "historical-pre-1.0")
        self.assertFalse(report["release_evidence"])
        self.assertIn("evaluated inputs", report["historical_note"])
        self.assertTrue(report["inputs"]["qurtail_sha256"])
        self.assertTrue(report["inputs"]["skill_sha256"])

    def test_episode_matrix_covers_the_documented_safety_cases(self) -> None:
        self.assertEqual(
            {episode.category for episode in EPISODES},
            {
                "error",
                "status",
                "structured-error",
                "multiline",
                "resumed-pattern",
                "numeric-change",
                "fail-open",
            },
        )
        self.assertEqual(
            len({episode.slug for episode in EPISODES}),
            len(EPISODES),
        )
        for episode in EPISODES:
            with self.subTest(episode=episode.slug):
                self.assertTrue(episode.task)
                self.assertTrue(episode.expected_decision)
                self.assertTrue(episode.required_blocks)

    def test_every_required_diagnostic_block_remains_complete(self) -> None:
        for episode in EPISODES:
            with self.subTest(episode=episode.slug):
                output = _qurtail_output(episode.lines)
                for block in episode.required_blocks:
                    self.assertIn("\n".join(block) + "\n", output)

    def test_dependency_free_gate_replay_passes(self) -> None:
        result = evaluate_suite(_portable_token_count)

        self.assertTrue(result["passed"], result)
        self.assertGreaterEqual(
            result["summary"]["median_repetitive_token_reduction"],
            0.80,
        )
        self.assertLessEqual(
            result["summary"]["maximum_nonrepetitive_token_inflation"],
            0.02,
        )
        self.assertTrue(
            all(
                episode["all_required_blocks_visible"]
                for episode in result["episodes"]
            )
        )

    def test_nonrepetitive_episodes_are_emitted_without_rewrites(self) -> None:
        for episode in EPISODES:
            if episode.repetitive:
                continue
            with self.subTest(episode=episode.slug):
                self.assertEqual(
                    _qurtail_output(episode.lines),
                    "".join(line + "\n" for line in episode.lines),
                )

    def test_logical_clock_replay_closes_periodic_summary_without_sleeping(self) -> None:
        result = evaluate_logical_clock_replay()

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["source_duration_seconds"], 31.0)
        self.assertEqual(result["wall_clock_sleep_seconds"], 0.0)
        self.assertEqual(
            result["output"],
            "INFO worker heartbeat\n. [1 similar in 30s]\n",
        )


if __name__ == "__main__":
    unittest.main()
