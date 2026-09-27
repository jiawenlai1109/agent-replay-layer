"""The two step-numbering systems in one run must be reconciled before any
latency number is published. These assertions are the reconciliation contract.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import sbr_paths
import tap

RUN = sbr_paths.RUNS_ROOT / "T-PIPE-01F-C1-csv-short-A1-181324"


@unittest.skipUnless(RUN.is_dir(), "injected run not present")
class TestStepAlignment(unittest.TestCase):
    def test_every_normalised_step_maps_to_a_provider_step(self) -> None:
        rows = tap.aligned_steps(RUN, "round_001", "executor")
        self.assertTrue(rows)
        unmapped = [r["ordinal"] for r in rows if r["provider_step"] is None]
        self.assertEqual(unmapped, [], "存在归不到 provider 步的归一化步，延迟数字不可用")

    def test_provider_step_is_monotone(self) -> None:
        rows = tap.aligned_steps(RUN, "round_001", "executor")
        prov = [r["provider_step"] for r in rows]
        self.assertEqual(prov, sorted(prov), "provider 步号必须随归一化顺序单调不减")

    def test_tool_bearing_provider_steps_match_the_raw_stream(self) -> None:
        """Cross-check the mapping against the raw event stream, independently.

        'max(provider_step) == number of step_finish events' is NOT a valid
        contract: the closing provider step is text-only and therefore has no
        normalised step to map to (measured: 8 step_finish events, max mapped
        step 7). The invariant that does hold is that the set of provider steps
        containing a tool call is identical on both sides.
        """
        rows = tap.aligned_steps(RUN, "round_001", "executor")
        from_rows = {r["provider_step"] for r in rows if r["kind"] == "tool_use"}

        raw = RUN / "lh_harness/role_orchestration/rounds/round_001/executor_raw_trajectory.jsonl"
        clock = tap.provider_step_index(raw)
        from_raw = set()
        current = 0
        for rec in tap._jsonl(raw):
            kind = rec.get("type")
            part = rec.get("part") or {}
            if kind == "step_start":
                current += 1
            elif kind == "tool_use":
                from_raw.add(max(current, 1))
        self.assertEqual(from_rows, from_raw, "含工具调用的 provider 步两侧不一致")
        self.assertLessEqual(max(r["provider_step"] for r in rows), len(tap.step_tokens(raw)))

    def test_normalised_and_provider_counts_differ_so_mixing_them_is_a_real_risk(self) -> None:
        rows = tap.aligned_steps(RUN, "round_001", "executor")
        n_finish = len(tap.step_tokens(RUN / "lh_harness/role_orchestration/rounds/round_001/executor_raw_trajectory.jsonl"))
        self.assertGreater(len(rows), n_finish, "本用例的前提是两套编号确实不同尺；若相等说明上游改了口径")


    def test_the_three_walkers_of_the_same_universe_agree(self) -> None:
        """live_progress, cost_timeline and epoch_split each glob the round files.
        They answer different questions, but they must count the *same* events: a
        fourth ruler is how the withdrawn cost numbers happened."""
        rid = "T-PIPE-01F-C1-csv-short-C-REC11-181604"
        run = RUN.parent / rid
        if not run.is_dir():
            self.skipTest("that C run is not on this machine")
        live = tap.live_progress(run)["steps"]
        timeline = len(tap.cost_timeline(run))
        split = tap.epoch_split(run, __import__("json").loads(
            (run / "control/owner.json").read_text(encoding="utf-8"))["started_at"])
        epoch = split["total_before"] + split["total_after"] + split.get("no_ts", 0)
        self.assertEqual(live, timeline)
        self.assertEqual(live, epoch, "epoch_split 与另两把尺数到的事件数不等")


class TestLiveProgress(unittest.TestCase):
    """The sampler the C arm calls every poll. Its whole job is that the step
    number and the cost come from ONE walk — the metric built from two separate
    readings is what produced the withdrawn "spend after detection" numbers."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, round_no: str, role: str, lines: list[str]) -> None:
        d = self.root / "lh_harness/role_orchestration/rounds" / round_no
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{role}_raw_trajectory.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _ev(kind: str, ts: float, cost: float = 0.0) -> str:
        return json.dumps({"type": kind, "part": {"cost": cost, "tokens": {"total": 10}},
                           "timestamp": ts})

    def test_steps_and_cost_come_from_the_same_walk(self) -> None:
        self._write("round_001", "manager", [self._ev("step_start", 100), self._ev("step_finish", 110, 0.001)])
        self._write("round_001", "executor", [self._ev("step_start", 120), self._ev("step_finish", 130, 0.002),
                                              self._ev("step_start", 140), self._ev("step_finish", 150, 0.004)])
        prog = tap.live_progress(self.root)
        self.assertEqual(prog["steps"], 3)
        self.assertAlmostEqual(prog["cost_usd"], 0.007, places=9)
        total = sum(len(tap.step_tokens(f)) for f in
                    self.root.glob("lh_harness/role_orchestration/rounds/round_*/*_raw_trajectory.jsonl"))
        self.assertEqual(prog["steps"], total, "采样器与离线时间线必须是同一把尺")

    def test_a_step_cut_mid_flight_shows_as_unclosed(self) -> None:
        """This is the case the offline re-read cannot see: the step opened, the
        halt landed, no step_finish was ever written."""
        self._write("round_001", "executor", [self._ev("step_start", 100), self._ev("step_finish", 110, 0.001),
                                              self._ev("step_start", 120)])
        prog = tap.live_progress(self.root)
        self.assertEqual((prog["steps"], prog["unclosed_steps"]), (1, 1))
        self.assertEqual(prog["last_event_type"], "step_start")

    def test_unclosed_never_goes_negative(self) -> None:
        """A file may legitimately hold more finishes than starts (the closing
        text-only step counts neither) — that is not minus one cut step."""
        self._write("round_001", "auditor", [self._ev("step_finish", 110, 0.001), self._ev("step_finish", 120, 0.002)])
        prog = tap.live_progress(self.root)
        self.assertEqual((prog["steps"], prog["unclosed_steps"]), (2, 0))

    def test_empty_run_reads_zero_not_crashes(self) -> None:
        prog = tap.live_progress(self.root / "nowhere")
        self.assertEqual(prog["steps"], 0)
        self.assertIsNone(prog["last_event_ts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
