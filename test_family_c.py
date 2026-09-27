"""Regression: every family-C seed must raise exactly the alarm set its truth says.

This pins the cascade behaviour. If a predicate changes and an injection stops
cascading (or starts cascading further), that is a change in the detector's
reach and must be a deliberate, recorded decision.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

LINUX = sys.platform.startswith("linux")   # predicates shell out to python3 and POSIX paths, so off-Linux these suites skip, not fail

import audit as A

SBR = pathlib.Path(__file__).resolve().parent
BASE = "T-PIPE-01F"
VARIANTS = ["C1-csv-short", "C2-stale-summary", "C3-dup-key", "C4-region-typo"]


@unittest.skipUnless(LINUX, "predicates run the task test suite as a subprocess")
class TestCleanControl(unittest.TestCase):
    def test_uninjected_seed_raises_nothing(self) -> None:
        seed = SBR / "tasks" / BASE / "seed"
        if not seed.is_dir():
            self.skipTest("clean seed not built")
        alarms = A.audit_workspace(BASE, seed)
        self.assertEqual([a["commitment_id"] for a in alarms if a["status"] == "fail"], [])


@unittest.skipUnless(LINUX, "predicates run the task test suite as a subprocess")
class TestInjections(unittest.TestCase):
    def _check(self, variant: str) -> None:
        tid = f"{BASE}-{variant}"
        truth_path = SBR / "ledger" / tid / "truth.json"
        seed = SBR / "tasks" / tid / "seed"
        if not truth_path.is_file() or not seed.is_dir():
            self.skipTest(f"{tid} not injected yet")
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        alarms = A.audit_workspace(tid, seed)
        got = sorted({a["commitment_id"] for a in alarms if a["status"] == "fail"})
        self.assertEqual(
            got,
            truth["expected_violations"],
            f"{tid}: 实际亮灯 {got} 与真值 {truth['expected_violations']} 不符",
        )
        self.assertIn(truth["root_cause"], got, f"{tid}: 根因承诺没有亮灯")
        self.assertTrue(truth["task_text_unchanged"], f"{tid}: 注入后任务原文被改动了")
        self.assertFalse(truth["told_to_agent"], f"{tid}: 不应告知 agent 缺陷")

    def test_c1_csv_short(self) -> None:
        self._check("C1-csv-short")

    def test_c2_stale_summary(self) -> None:
        self._check("C2-stale-summary")

    def test_c3_dup_key(self) -> None:
        self._check("C3-dup-key")

    def test_c4_region_typo(self) -> None:
        self._check("C4-region-typo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
