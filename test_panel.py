"""Offline tests for the panel renderer.

The panel's whole claim is that it shows what is already on disk, so these build a fake
manifest directory and check the page names the right step, the right promise, the right
rewind target and the generated breakpoint text, without binding a socket or calling
anything. They also pin two safety properties: `na` is never rendered as a pass, and
untrusted task text cannot smuggle markup into the page.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import panel
import sbr_paths

COMMIT = "c-summary-recompute"


def manifest(workspace: Path) -> dict:
    directive = workspace / "RECOVERY.md"
    directive.write_text("断点：探针在第 8 步发现破坏\n不要重放已经写过的产物\n", encoding="utf-8")
    return {
        "run_id": "T-ONE-C-REC1-000000", "task_id": "T-ONE", "arm": "C-REC",
        "model": "some/provider-model", "agent": "opencode", "started_at": "2026-09-29 00:00:00 CST",
        "trigger_rule": "new_red_after_green_v1",
        "injected_at_provider_step": 6,
        "sensor": {"index": 40, "provider_step": 8, "commitment_id": COMMIT,
                   "expected": "6 rows", "actual": "<script>alert(1)</script> 7 rows"},
        "injection": {"recipe": "C1-x", "root_cause": COMMIT, "cascade": ["c-claim-honest"]},
        "sensor_commitments": [COMMIT, "c-orders-file"],
        "readings": [
            {"step": 6, "cost_usd": 0.0010, COMMIT: {"status": "pass", "expected": "6 rows", "actual": "6 rows"},
             "c-orders-file": {"status": "na", "expected": "not in play", "actual": "not_in_play:data/orders.csv"}},
            {"step": 8, "cost_usd": 0.0012, COMMIT: {"status": "fail", "expected": "6 rows", "actual": "7 rows"},
             "c-orders-file": {"status": "na", "expected": "not in play", "actual": "not_in_play:data/orders.csv"}},
        ],
        "status_flips": [{"step": 8, "commitment_id": COMMIT, "from": "pass", "to": "fail"}],
        "last_invariant_holding_provider_step": 6, "rollback_ok": True,
        "recovery_md": str(directive),
        "at_halt": {"steps_after_detection": 0, "cost_after_detection_usd": 0.0,
                    "unclosed_steps": 1, "mid_flight_step_cut": True},
        "metrics": {"rounds": 2, "steps": 70, "tokens": 240000, "cache_read": 200000, "cost": 0.0142},
        "budget": {"max_rounds": 3, "extra_rounds": 2, "inject_after": 6, "minutes": 12},
        "report": {"report_status": "complete", "completion_satisfied": True, "abort_reason": ""},
        "verdict_end_state": {"ledger_verdict": "satisfied", "violated": [],
                              "latched_counts": {"pass": 9, "fail": 0, "na": 0}},
    }


class Render(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.m = manifest(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def page(self, m=None, table=None) -> str:
        return panel.render_run(m or self.m, table)

    def test_names_the_red_step_and_the_promise(self):
        page = self.page()
        self.assertIn(COMMIT, page)
        self.assertIn("在第 8 步变红", page)
        self.assertIn("注入在第 6 步，lag 2", page)

    def test_lag_is_detection_minus_injection(self):
        self.assertEqual(panel.alarm(self.m)["lag"], 2)

    def test_rewind_target_and_result_are_shown(self):
        self.assertIn("回滚目标 = 第 6 步", self.page())
        self.assertIn("结果 成功", self.page())

    def test_directive_text_is_rendered_from_disk(self):
        self.assertIn("不要重放已经写过的产物", self.page())

    def test_na_is_not_a_pass(self):
        grid = panel.matrix(self.m)
        self.assertEqual(grid.count(">绿</td>"), 1)
        self.assertEqual(grid.count(">红</td>"), 1)
        self.assertEqual(grid.count(">未到</td>"), 2)

    def test_untrusted_text_cannot_smuggle_markup(self):
        page = self.page()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_conflict_flag_only_when_upstream_claim_disagrees(self):
        self.assertNotIn("冲突", self.page())
        bad = dict(self.m, verdict_end_state={"ledger_verdict": "violated",
                                              "violated": ["c-no-silent-timeout"],
                                              "latched_counts": {"pass": 8, "fail": 1, "na": 0}})
        self.assertIn("冲突", self.page(bad))
        self.assertTrue(panel.row(bad)["conflict"])

    def test_d_arm_has_no_directive_and_says_so(self):
        d = dict(self.m, arm="D", no_rewind=True)
        d.pop("recovery_md")
        page = self.page(d)
        self.assertIn("D 臂：检测到，但不回滚、不给说明", page)
        self.assertIn("这条 run 没有断点说明", page)


    def test_rescored_verdict_wins_over_stored(self):
        bad = dict(self.m, verdict_end_state={"ledger_verdict": "violated",
                                              "violated": ["c-local-suite-no-regression"],
                                              "latched_counts": {"pass": 4, "fail": 1, "na": 0}})
        table = {bad["run_id"]: {"new": "satisfied", "old": "not_satisfied", "pass": "5",
                                 "fail": "0", "na": "0", "unevaluated": ""}}
        self.assertIn("冲突：上游自称完成", self.page(bad))
        page = self.page(bad, table)
        self.assertNotIn("冲突：上游自称完成", page)
        self.assertIn("依据：离线重算", page)
        self.assertIn("当时记录是 violated", page)
        self.assertFalse(panel.row(bad, table)["conflict"])

    def test_run_missing_from_rescore_is_labelled_as_stored(self):
        page = self.page(self.m, {"other-run": {"new": "satisfied", "old": "satisfied",
                                                "pass": "9", "fail": "0", "na": "0", "unevaluated": ""}})
        self.assertIn("依据：当时记录", page)


class Index(unittest.TestCase):
    def test_rows_separate_aborted_from_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead = manifest(root)
            dead["run_id"] = "T-DEAD-A1-000001"
            dead["metrics"] = {"rounds": 0, "steps": 0, "tokens": 0, "cache_read": 0, "cost": 0.0}
            (root / "good.json").write_text(json.dumps(manifest(root)), encoding="utf-8")
            (root / "dead.json").write_text(json.dumps(dead), encoding="utf-8")
            real = sbr_paths.MANIFESTS
            sbr_paths.MANIFESTS = root
            try:
                rows = [panel.row(m) for m in panel.load()]
                page = panel.render_index(rows)
            finally:
                sbr_paths.MANIFESTS = real
        self.assertEqual(sorted(r["valid"] for r in rows), [False, True])
        self.assertIn("<a href='/run/T-ONE-C-REC1-000000'>", page)
        self.assertNotIn("<a href='/run/T-DEAD-A1-000001'>", page)


if __name__ == "__main__":
    unittest.main()
