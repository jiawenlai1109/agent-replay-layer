#!/usr/bin/env python3
"""Per-CLI stream names.

lh-harness writes the episode mirror under a name chosen by the agent CLI
(manager.py:1955-1962), so a reader hardcoded to `provider_stream.jsonl` reads zero
episodes for a codex or claude_code run and reports a cost timeline that looks
complete but is missing the pre-halt attempt. That is defect #30's shape: silent,
not loud.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tap


def _finish(ts: float, cost: float) -> str:
    return json.dumps({"type": "step_finish", "timestamp": ts,
                      "part": {"type": "step-finish", "cost": cost,
                               "tokens": {"total": 100, "input": 40, "output": 60}}})


class StreamsPerCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name) / "R-1"
        self.ep = self.run / "lh_harness" / "cli_executor_episodes" / "ep001"
        self.ep.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_stream_is_read(self):
        (self.ep / "codex_stream.jsonl").write_text(
            _finish(1000.0, 0.01) + "\n" + _finish(2000.0, 0.02) + "\n", encoding="utf-8")
        events = tap.cost_timeline(self.run, with_episodes=True)
        self.assertEqual(len(events), 2, events)
        self.assertEqual({e["role"] for e in events}, {"executor"})
        self.assertAlmostEqual(sum(e["cost"] for e in events), 0.03)

    def test_claude_stream_is_read(self):
        (self.ep / "claude_stream.jsonl").write_text(_finish(1500.0, 0.005) + "\n", encoding="utf-8")
        self.assertEqual(len(tap.cost_timeline(self.run, with_episodes=True)), 1)

    def test_all_three_names_are_found_and_sorted(self):
        (self.ep / "provider_stream.jsonl").write_text(_finish(1000.0, 0.01) + "\n", encoding="utf-8")
        other = self.run / "lh_harness" / "cli_manager_episodes" / "ep002"
        other.mkdir(parents=True)
        (other / "codex_stream.jsonl").write_text(_finish(900.0, 0.02) + "\n", encoding="utf-8")
        found = tap.episode_streams(self.run)
        # Sorted by episode directory, so ep001 (executor) comes before ep002 (manager).
        self.assertEqual([p.name for p in found],
                         ["provider_stream.jsonl", "codex_stream.jsonl"], found)
        events = tap.cost_timeline(self.run, with_episodes=True)
        self.assertEqual([e["role"] for e in events], ["manager", "executor"])

    def test_same_millisecond_in_one_file_is_not_deduplicated(self):
        # Dedup is for the mirror only, and two steps finishing in the same ms in
        # the same role file are two steps: dropping one shortens the clock.
        (self.ep / "codex_stream.jsonl").write_text(
            _finish(1000.0, 0.01) + "\n" + _finish(1000.0, 0.01) + "\n", encoding="utf-8")
        self.assertEqual(len(tap.cost_timeline(self.run, with_episodes=True)), 2)


if __name__ == "__main__":
    unittest.main()
