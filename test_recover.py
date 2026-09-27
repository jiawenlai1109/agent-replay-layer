"""Red/green fixtures for the recovery side.

Every test here runs against a synthesized run directory and an injected
commitment subset: no provider call, no live workspace, nothing to top up. A
detector that can only be exercised by spending money is not a detector.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import sys
import unittest

LINUX = sys.platform.startswith("linux")   # predicates shell out to python3 and POSIX paths, so off-Linux these suites skip, not fail
from pathlib import Path

import recover
import snapshot
from predicates import csv_row_count  # used to build commitments the same way the ledger does

WS = Path("/work/ws_fixture")
CSV12 = "order_id,date,region,amount\n" + "".join(
    f"O{i:03d},2026-01-0{i % 3 + 1},r{i % 3},{i * 10}\n" for i in range(12)
)
CSV3 = "order_id,date,region,amount\nO000,2026-01-01,r0,0\nO001,2026-01-02,r1,10\nO002,2026-01-03,r2,20\n"

CARD = {"id": "c-card", "predicate": "csv_row_count", "applies_to": "workspace", "args": {"path": "data/orders.csv", "rows": 12}}
OTHER = {"id": "c-other", "predicate": "artifact_exists", "applies_to": "workspace", "args": {"path": "STATUS.md"}}


def write_step(n: int, rel: str, content: str) -> dict:
    return {"kind": "tool_use", "name": "write", "step_num": n, "input": {"filePath": str(WS / rel), "content": content}}


def edit_step(n: int, rel: str, old: str, new: str) -> dict:
    return {
        "kind": "tool_use",
        "name": "edit",
        "step_num": n,
        "input": {"filePath": str(WS / rel), "oldString": old, "newString": new},
    }


def bash_step(n: int, cmd: str) -> dict:
    return {"kind": "tool_use", "name": "bash", "step_num": n, "input": {"command": cmd}}


def result_step(n: int) -> dict:
    return {"kind": "tool_result", "step_num": n, "is_error": False, "text": "ok"}


FIXTURE_GUARD = unittest.skipUnless(LINUX, "predicates shell out: needs python3 and POSIX paths")


@FIXTURE_GUARD
class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sbtest_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_run(self, rounds: dict[str, list[dict]]) -> Path:
        run_dir = self.tmp / "runX"
        base = run_dir / "lh_harness" / "role_orchestration" / "rounds"
        for round_no, steps in rounds.items():
            (base / round_no).mkdir(parents=True, exist_ok=True)
            (base / round_no / "executor_trajectory.jsonl").write_text(
                "".join(json.dumps(s) + "\n" for s in steps), encoding="utf-8"
            )
        return run_dir

    def scan(self, run_dir: Path, commitments: list[dict], attempt: int = 1) -> recover.VerdictStore:
        return recover.scan_run(
            "T-PIPE-01", run_dir, WS, run_id="runX", commitments=commitments, attempt=attempt
        )


class TestCheckpointSelection(Fixture):
    def test_read_only_bash_is_not_a_checkpoint(self) -> None:
        self.assertFalse(recover.is_write_step({"kind": "tool_use", "name": "bash", "input_excerpt": "ls -la /x"}))
        self.assertFalse(recover.is_write_step({"kind": "tool_use", "name": "read", "input_excerpt": "/x"}))
        self.assertFalse(recover.is_write_step({"kind": "tool_result", "name": None}))

    def test_write_and_mutating_bash_are_checkpoints(self) -> None:
        self.assertTrue(recover.is_write_step({"kind": "tool_use", "name": "write", "input_excerpt": "x"}))
        self.assertTrue(recover.is_write_step({"kind": "tool_use", "name": "edit", "input_excerpt": "x"}))
        self.assertTrue(
            recover.is_write_step({"kind": "tool_use", "name": "bash", "input_excerpt": "python3 gen.py > out.csv"})
        )
        self.assertTrue(recover.is_write_step({"kind": "tool_use", "name": "bash", "input_excerpt": ""}))


class TestLocateTiers(Fixture):
    def test_detected_at_when_a_pass_was_sampled_one_checkpoint_earlier(self) -> None:
        run = self.make_run(
            {
                "round_001": [write_step(1, "data/orders.csv", CSV12), result_step(2)],
                "round_002": [write_step(1, "data/orders.csv", CSV3), result_step(2)],
            }
        )
        store = self.scan(run, [CARD])
        ev = recover.locate(store)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["tier"], "DETECTED_AT")
        self.assertIn("round_002", ev[0]["first_detectable_step"])
        self.assertIn("round_001", ev[0]["last_invariant_holding_step"])
        self.assertIsNone(ev[0]["bounded_by"])

    def test_bounded_by_when_the_predicate_was_only_sampled_at_boundaries(self) -> None:
        run = self.make_run(
            {
                "round_001": [write_step(1, "data/orders.csv", CSV12), result_step(2)],
                "round_002": [write_step(1, "STATUS.md", "done"), result_step(2), write_step(3, "data/orders.csv", CSV3)],
            }
        )
        saved = recover.HEAVY
        recover.HEAVY = {"csv_row_count"}
        try:
            store = self.scan(run, [CARD, OTHER])
        finally:
            recover.HEAVY = saved
        ev = {e["commitment_id"]: e for e in recover.locate(store)}
        self.assertEqual(list(ev), ["c-card"], json.dumps({k: v["tier"] for k, v in ev.items()}))
        self.assertEqual(ev["c-card"]["tier"], "BOUNDED_BY")
        lo, hi = ev["c-card"]["bounded_by"]
        self.assertTrue(lo.endswith("/boundary"), lo)
        self.assertIn("round_002", hi)
        self.assertEqual(ev["c-card"]["last_invariant_holding_step"], lo)

    def test_detected_only_when_the_first_observation_is_already_red(self) -> None:
        run = self.make_run({"round_001": [write_step(1, "data/orders.csv", CSV3), result_step(2)]})
        store = self.scan(run, [CARD])
        ev = recover.locate(store)
        self.assertEqual([e["tier"] for e in ev], ["DETECTED_ONLY"])
        self.assertIsNone(ev[0]["last_invariant_holding_seq"])
        rec = recover.escalate(store, ev)
        self.assertEqual(rec["response"], "HALT")
        self.assertIsNone(rec["rewind_to"])

    def test_an_untouched_promise_is_never_judged_at_a_step(self) -> None:
        """'Not yet applicable' must not become a violation (DESIGN 4 symbols)."""
        run = self.make_run({"round_001": [write_step(1, "data/orders.csv", CSV12)]})
        store = self.scan(run, [CARD, OTHER])
        judged = [r for r in store.rows if r["commitment_id"] == "c-other"]
        self.assertEqual(judged, [], "STATUS.md was never written, so no step may be blamed for it")

    def test_only_the_first_red_localizes(self) -> None:
        run = self.make_run(
            {
                "round_001": [write_step(1, "data/orders.csv", CSV12)],
                "round_002": [write_step(1, "data/orders.csv", CSV3)],
                "round_003": [write_step(1, "data/orders.csv", CSV3)],
            }
        )
        store = self.scan(run, [CARD])
        self.assertEqual(len(store.reds()), 2)
        self.assertEqual(len(recover.locate(store)), 1)


class TestEscalation(Fixture):
    def test_no_red_means_observe(self) -> None:
        run = self.make_run({"round_001": [write_step(1, "data/orders.csv", CSV12)]})
        store = self.scan(run, [CARD])
        self.assertEqual(recover.escalate(store, recover.locate(store))["response"], "OBSERVE")

    def test_repeated_sampling_of_one_red_is_not_a_loop(self) -> None:
        """16 identical reds inside one attempt is latching; only a second attempt loops."""
        rounds = {
            f"round_{i:03d}": [write_step(1, "data/orders.csv", CSV3), write_step(2, "STATUS.md", "x")]
            for i in range(1, 9)
        }
        run = self.make_run(rounds)
        store = self.scan(run, [CARD, OTHER])
        self.assertGreaterEqual(len(store.reds()), 8)
        self.assertEqual(set(r["attempt"] for r in store.rows), {1})
        ev = recover.locate(store)
        rec = recover.escalate(store, ev)
        self.assertNotEqual(rec["reason"], "circuit_breaker", rec)

    def test_the_same_red_in_a_second_attempt_halts(self) -> None:
        run = self.make_run({"round_001": [write_step(1, "data/orders.csv", CSV3)]})
        store = self.scan(run, [CARD])
        first = recover.escalate(store, recover.locate(store))
        self.assertEqual(first["response"], "HALT")
        self.assertNotEqual(first["reason"], "circuit_breaker", first)
        second = self.scan(run, [CARD], attempt=2)
        for r in second.rows:
            store.record(**r)
        rec = recover.escalate(store, recover.locate(store))
        self.assertEqual(rec["response"], "HALT")
        self.assertEqual(rec["reason"], "circuit_breaker")

    def test_contaminated_product_with_a_safe_point_votes_veto_and_rewinds(self) -> None:
        run = self.make_run(
            {
                "round_001": [write_step(1, "data/orders.csv", CSV12)],
                "round_002": [write_step(1, "data/orders.csv", CSV3)],
            }
        )
        store = self.scan(run, [CARD])
        rec = recover.escalate(store, recover.locate(store))
        self.assertEqual(rec["response"], "VETO")
        self.assertIsNotNone(rec["rewind_to"])


class TestSnapshotHonesty(Fixture):
    def test_bash_write_makes_the_snapshot_incomplete_and_heavy_sensors_go_na(self) -> None:
        run = self.make_run(
            {"round_001": [bash_step(1, "python3 gen.py"), write_step(2, "data/orders.csv", CSV12)]}
        )
        saved = recover.HEAVY
        recover.HEAVY = {"csv_row_count"}
        try:
            store = self.scan(run, [CARD, OTHER])
        finally:
            recover.HEAVY = saved
        rows = [r for r in store.rows if r["trigger"] == "round_boundary"]
        self.assertEqual([r["status"] for r in rows], ["na"])
        self.assertIn("snapshot_incomplete", rows[0]["actual"])

    def test_unreplayable_edit_is_recorded_as_a_divergence(self) -> None:
        with snapshot.Snapshot("T-PIPE-01", WS, root=self.tmp / "snap") as snap:
            snap.apply(edit_step(1, "data/orders.csv", "not-there", "whatever"))
            self.assertFalse(snap.complete)
            self.assertEqual(snap.divergences[0]["why"], "file_not_in_snapshot")
            snap.apply(write_step(2, "data/orders.csv", CSV12))
            self.assertEqual(csv_row_count({"workspace": str(snap.dir), "path": "data/orders.csv", "rows": 12})["status"], "pass")
            snap.apply(edit_step(3, "data/orders.csv", "O000", "O999"))
            self.assertEqual(snap.applied_edit, 1)
            self.assertTrue((snap.dir / "data" / "orders.csv").read_text(encoding="utf-8").startswith("order_id"))
            self.assertIn("O999", (snap.dir / "data" / "orders.csv").read_text(encoding="utf-8"))

    def test_paths_outside_the_workspace_are_refused(self) -> None:
        with snapshot.Snapshot("T-PIPE-01", WS, root=self.tmp / "snap2") as snap:
            snap.apply(write_step(1, "/etc/passwd", "pwned"))
            self.assertEqual(snap.applied_write, 0)
            self.assertEqual(snap.divergences[-1]["why"], "path_outside_workspace")
            self.assertFalse((snap.dir / "etc" / "passwd").exists())


class TestDeterminism(Fixture):
    def test_two_scans_of_the_same_run_agree(self) -> None:
        rounds = {
            "round_001": [write_step(1, "data/orders.csv", CSV12), result_step(2), bash_step(3, "grep -c x data/orders.csv")],
            "round_002": [write_step(1, "STATUS.md", "UNITTEST_CASES=2"), write_step(2, "data/orders.csv", CSV3)],
        }
        run_a = self.make_run(rounds)
        store_a = self.scan(run_a, [CARD, OTHER])
        store_b = self.scan(run_a, [CARD, OTHER])
        key = lambda store: [(r["seq"], r["commitment_id"], r["status"], r["step"]) for r in store.rows]  # noqa: E731
        self.assertEqual(key(store_a), key(store_b))
        self.assertEqual(recover.report(store_a), recover.report(store_b))

    def test_every_row_carries_provenance(self) -> None:
        run = self.make_run({"round_001": [write_step(1, "data/orders.csv", CSV3)]})
        store = self.scan(run, [CARD])
        for r in store.rows:
            self.assertTrue(r["provenance"].startswith("rule:c-card@"), r["provenance"])


class TestStorePersistence(Fixture):
    def test_recording_into_a_fresh_directory_works(self) -> None:
        """record() appends straight to disk, so the parent must exist already."""
        target = self.tmp / "verdicts" / "nested" / "runX.jsonl"
        store = recover.VerdictStore(target)
        store.record(run_id="runX", seq=1, step="round_001/e#1:p1", commitment_id="c-a",
                     predicate="artifact_exists", status="fail", expected="x", actual="y",
                     trigger="write_step:write", provenance="rule:c-a@round_001/e#1:p1", ms=1)
        self.assertTrue(target.is_file())
        reloaded = recover.VerdictStore.load(target)
        self.assertEqual([r["commitment_id"] for r in reloaded.rows], ["c-a"])
        self.assertEqual(recover.locate(reloaded)[0]["tier"], "DETECTED_ONLY")


class TestDirective(Fixture):
    def test_directive_quotes_the_verdict_not_a_rephrased_task(self) -> None:
        run = self.make_run(
            {
                "round_001": [write_step(1, "data/orders.csv", CSV12)],
                "round_002": [write_step(1, "data/orders.csv", CSV3)],
            }
        )
        store = self.scan(run, [CARD])
        ev = recover.locate(store)
        rec = recover.escalate(store, ev)
        text = recover.compose_directive(store, ev, rec, task_id="T-PIPE-01")
        self.assertIn("DETECTED_AT", text)
        self.assertIn("rule:c-card@", text)
        self.assertIn("12 data rows", text)
        self.assertIn("3 data rows", text)
        self.assertIn("禁止重放", text)
        self.assertNotIn("请完成任务", text)

    def test_bounded_tier_says_it_cannot_claim_an_earlier_position(self) -> None:
        run = self.make_run({"round_001": [write_step(1, "data/orders.csv", CSV3)]})
        store = self.scan(run, [CARD])
        ev = recover.locate(store)
        text = recover.compose_directive(store, ev, recover.escalate(store, ev))
        self.assertIn("无安全回退点", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
