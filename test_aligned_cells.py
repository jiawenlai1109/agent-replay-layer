#!/usr/bin/env python3
"""Cell grouping tests for aligned_cells.py.

Written as a regression test for the bug it had: the cell key was
(task, rep, round ceiling) without the model, so a second provider run on the same
task/rep overwrote the deepseek side instead of becoming its own cell. Two models
in one cell is a pooled cost number with no unit.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import aligned_cells


def manifest(run_id: str, task: str, arm: str, model: str, cost: float, tokens: int) -> dict:
    return {
        "run_id": run_id,
        "task_id": task,
        "arm": arm,
        "model": model,
        "budget": {"max_rounds": 3, "extra_rounds": 2, "minutes": 12, "inject_after": 6},
        "metrics": {"steps": 70, "rounds": 2, "tokens": tokens, "cache_read": int(tokens * 0.8), "cost": cost},
        "sensor": {"provider_step": 9, "detected_lag_steps": 2, "commitment_id": "c-x"},
        "injected_at_provider_step": 7,
        "last_invariant_holding_provider_step": 7,
        "rollback_ok": arm == "C-REC",
        "verdict_end_state": {"ledger_verdict": "satisfied", "latched_counts": {"pass": 9, "fail": 0, "na": 0}},
        "report": {"abort_reason": "", "report_status": "ok"},
    }


class ModelIsPartOfTheCell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.man = Path(self.tmp.name) / "manifests"
        self.man.mkdir()
        self.out = Path(self.tmp.name) / "aligned_cells.csv"
        for m in [
            manifest("T-C1-D20-100000", "T-C1", "D", "deepseek/deepseek-flash", 0.020, 400_000),
            manifest("T-C1-C-REC20-100001", "T-C1", "C-REC", "deepseek/deepseek-flash", 0.010, 200_000),
            manifest("T-C1-D20-100002", "T-C1", "D", "xiaomi/mimo-v2.6-flash", 0.060, 900_000),
            manifest("T-C1-C-REC20-100003", "T-C1", "C-REC", "xiaomi/mimo-v2.6-flash", 0.030, 300_000),
        ]:
            (self.man / (m["run_id"] + ".json")).write_text(json.dumps(m), encoding="utf-8")
        self._man = aligned_cells.MAN
        self._out = aligned_cells.OUT
        aligned_cells.MAN = self.man
        aligned_cells.OUT = self.out

    def tearDown(self):
        aligned_cells.MAN = self._man
        aligned_cells.OUT = self._out
        self.tmp.cleanup()

    def run_cli(self) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            aligned_cells.main()
        return buf.getvalue()

    def test_two_models_make_two_cells_not_one(self):
        out = self.run_cli()
        # The bug showed up as one cell whose numbers came from whichever model was
        # read last, so the assertion is on the cell count per model, not on the file.
        self.assertIn("model=deepseek/deepseek-flash", out)
        self.assertIn("model=xiaomi/mimo-v2.6-flash", out)
        self.assertEqual(out.count("格子数 1，两臂都做成 1"), 2)

    def test_cost_ratios_are_reported_inside_each_model(self):
        out = self.run_cli()
        self.assertIn("x0.5000", out)              # both models are x0.5 by construction
        self.assertIn("D 0.02 USD", out)           # the deepseek cell quotes its own money
        self.assertNotIn("0.08", out)              # a pooled cell would print 0.02+0.06

    def test_noncache_share_is_the_column_money_follows(self):
        self.run_cli()
        rows = self.out.read_text(encoding="utf-8").splitlines()
        header = rows[0].split(",")
        for col in ("d_noncache_tokens", "rec_noncache_tokens", "noncache_ratio_rec_over_d",
                    "d_cache_read", "rec_cache_read", "d_cache_hit_rate", "rec_cache_hit_rate",
                    "cache_ratio_rec_over_d", "model"):
            self.assertIn(col, header)

    def test_hit_rates_are_printed_next_to_the_ratios(self):
        out = self.run_cli()
        self.assertIn("命中率", out)
        self.assertIn("两臂缓存条件可比", out)   # the fixture gives both arms 80% cache


if __name__ == "__main__":
    unittest.main()
