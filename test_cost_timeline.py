"""The cost cut must land on the moment the probe looked, not on a file-walk
position, and it must refuse when the events the cut refers to are no longer on
disk. Fixtures are synthetic so the expected answer is known by construction
rather than by reading last week's output.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import cost_to_success
import runner
import tap


def event(ts: float, cost: float, total: int = 1000) -> str:
    return json.dumps({
        "type": "step_finish",
        "part": {"reason": "model-end", "tokens": {"total": total}, "cost": cost},
        "timestamp": ts,
    })


def write_round(root: pathlib.Path, round_no: str, role: str, lines: list[str]) -> None:
    d = root / "lh_harness" / "role_orchestration" / "rounds" / round_no
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{role}_raw_trajectory.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_owner(root: pathlib.Path, started_at: float, epoch: int = 1) -> None:
    d = root / "control"
    d.mkdir(parents=True, exist_ok=True)
    (d / "owner.json").write_text(json.dumps(
        {"started_at": started_at, "resume_epoch": epoch}), encoding="utf-8")


class TestCostTimeline(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cut_follows_time_not_the_alphabetical_role_walk(self) -> None:
        """Roles run manager -> executor -> auditor, but the glob walks auditor
        first. Counting N events is still correct (it is a total, not a position);
        slicing the walk sequence at N is not.
        """
        write_round(self.root, "round_001", "manager", [event(100, 0.001)])          # ts 100
        write_round(self.root, "round_001", "executor", [event(200, 0.002, 2000)])   # ts 200
        write_round(self.root, "round_001", "auditor", [event(300, 0.004, 3000)])    # ts 300

        walk_order = [f"{e['round']}/{e['role']}" for e in tap.cost_timeline(self.root)]
        self.assertEqual(
            walk_order, ["round_001/manager", "round_001/executor", "round_001/auditor"],
            "时间线必须按毫秒戳排，而按文件名字典序会是 auditor/executor/manager")

        sp = tap.split_at_detection(tap.cost_timeline(self.root), 2)
        self.assertEqual(sp["error"], "")
        self.assertAlmostEqual(sp["cost_after_usd"], 0.004, places=9)
        self.assertAlmostEqual(sp["cost_before_usd"], 0.003, places=9)
        self.assertEqual(sp["cut_ts_ms"], 200.0)

    def test_before_plus_after_equals_the_timeline_total(self) -> None:
        write_round(self.root, "round_001", "executor",
                    [event(10 + i, 0.001 * (i + 1)) for i in range(5)])
        events = tap.cost_timeline(self.root)
        for n in range(6):
            sp = tap.split_at_detection(events, n)
            self.assertAlmostEqual(
                (sp["cost_before_usd"] or 0) + (sp["cost_after_usd"] or 0),
                sp["timeline_cost_usd"], places=9, msg=f"n={n} 前后之和应等于总量")

    def test_shrinking_artifact_set_is_an_error_not_a_zero(self) -> None:
        """If the probe counted more steps than are on disk now, the cut is
        unlocatable. Returning 0.0 here would read as 'no waste'."""
        write_round(self.root, "round_001", "executor", [event(10, 0.001), event(20, 0.002)])
        sp = tap.split_at_detection(tap.cost_timeline(self.root), 7)
        self.assertEqual(sp["error"], "count_exceeds_events")
        self.assertIsNone(sp["cost_after_usd"])

    def test_untimestamped_cost_blocks_the_cut_but_free_steps_do_not(self) -> None:
        """A step with no timestamp could be on either side of the boundary. If it
        costs nothing the share is still right; if it costs money the number is
        unknowable and must not be reported as 0% waste."""
        write_round(self.root, "round_001", "executor", [event(10, 0.001), event(20, 0.002)])
        write_round(self.root, "round_002", "executor", [json.dumps(
            {"type": "step_finish", "part": {"tokens": {}, "cost": 0.0}})])
        events = tap.cost_timeline(self.root)
        self.assertEqual(sum(1 for e in events if e["ts"] is None), 1)
        sp = tap.split_at_detection(events, 2)
        self.assertEqual(sp["error"], "")
        self.assertAlmostEqual(sp["cost_after_usd"], 0.0, places=9)

        write_round(self.root, "round_003", "executor", [json.dumps(
            {"type": "step_finish", "part": {"tokens": {"total": 5}, "cost": 0.003}})])
        sp = tap.split_at_detection(tap.cost_timeline(self.root), 2)
        self.assertEqual(sp["error"], "untimestamped_cost")
        self.assertIsNone(sp["cost_after_usd"])
        self.assertAlmostEqual(sp["no_ts_cost"], 0.003, places=9)

    def test_zero_detection_step_attributes_the_whole_run(self) -> None:
        """A probe that fired before any provider step existed leaves the whole
        episode after it — the share must be 1.0, not silently 0."""
        write_round(self.root, "round_001", "executor", [event(10, 0.001), event(20, 0.002)])
        sp = tap.split_at_detection(tap.cost_timeline(self.root), 0)
        self.assertAlmostEqual(sp["cost_after_usd"], 0.003, places=9)
        self.assertEqual(sp["cost_before_usd"], 0.0)
        self.assertIsNone(sp["cut_ts_ms"])

    def test_ties_at_the_cut_are_counted(self) -> None:
        """Two steps finishing in the same millisecond sit on the wrong side of
        the boundary by chance; the report has to say how often that happens."""
        write_round(self.root, "round_001", "executor", [event(10, 0.001), event(10, 0.002), event(30, 0.004)])
        sp = tap.split_at_detection(tap.cost_timeline(self.root), 2)
        self.assertEqual(sp["tie_at_cut"], 2)
        self.assertEqual(sp["cut_ts_ms"], 10.0)


class TestAttemptSplit(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recovery_work_is_separated_from_the_interrupted_attempt(self) -> None:
        write_round(self.root, "round_001", "executor", [
            event(100, 0.001),        # attempt 1
            event(200, 0.002),        # attempt 1, probe counts up to here
            event(9000, 0.020),       # attempt 2 (after the resume at 5 s)
            event(9100, 0.010),       # attempt 2
        ])
        write_owner(self.root, 5.0)
        bnd = tap.attempt_boundary(self.root)
        self.assertEqual(bnd["ts_ms"], 5000.0)
        self.assertEqual(bnd["resume_epoch"], 1)
        att = tap.attempt_split(tap.cost_timeline(self.root), bnd)
        self.assertEqual(att["error"], "")
        self.assertEqual((att["steps_before"], att["steps_recovery"]), (2, 2))
        self.assertAlmostEqual(att["cost_recovery_usd"], 0.030, places=9)
        self.assertAlmostEqual(att["cost_before_usd"], 0.003, places=9)

    def test_a_resume_that_ate_the_first_attempt_is_visible_as_a_shrink(self) -> None:
        """Measured on the real C samples: the resumed attempt rewrites the same
        round directory, so the steps the probe counted are gone. The split must
        report that rather than hand back a clean-looking number."""
        write_round(self.root, "round_001", "executor",
                    [event(9000, 0.020), event(9100, 0.010)])      # only attempt 2 survives
        write_owner(self.root, 5.0)
        events = tap.cost_timeline(self.root)
        att = tap.attempt_split(events, tap.attempt_boundary(self.root))
        self.assertEqual((att["steps_before"], att["steps_recovery"]), (0, 2))
        sp = tap.split_at_detection(events, 6)                     # probe had counted 6 steps
        self.assertEqual(sp["error"], "count_exceeds_events")

    def test_missing_owner_record_is_reported_not_guessed(self) -> None:
        write_round(self.root, "round_001", "executor", [event(100, 0.001)])
        bnd = tap.attempt_boundary(self.root)
        self.assertEqual(bnd["error"], "no_owner_file")
        att = tap.attempt_split(tap.cost_timeline(self.root), bnd)
        self.assertEqual(att["error"], "no_owner_file")
        self.assertIsNone(att["cost_recovery_usd"])


class TestEpisodeMirror(unittest.TestCase):
    """A resume rewrites `round_001/<role>_raw_trajectory.jsonl` wholesale, so the
    interrupted attempt is only recoverable from the per-CLI-episode mirror. The
    mirror carries the same events, so it must be unioned, not concatenated."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _episode(self, role_dir: str, ep: str, lines: list[str]) -> None:
        d = self.root / "lh_harness" / role_dir / ep
        d.mkdir(parents=True, exist_ok=True)
        (d / "provider_stream.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_shared_events_are_counted_once(self) -> None:
        both = [event(100, 0.001), event(200, 0.002)]
        write_round(self.root, "round_001", "executor", both)
        self._episode("cli_executor_episodes", "ep001", both)
        ev = tap.cost_timeline(self.root, with_episodes=True)
        self.assertEqual(len(ev), 2, "镜像与 round 文件是同一批事件，去重后不该翻倍")
        self.assertEqual({e["source"] for e in ev}, {"rounds"})
        self.assertAlmostEqual(sum(e["cost"] for e in ev), 0.003, places=9)

    def test_same_millisecond_steps_in_one_role_file_stay_two_steps(self) -> None:
        """The dedupe key must not collapse two genuinely distinct steps that
        finish in the same millisecond — that would shorten the authoritative
        stream to agree with the mirror."""
        pair = [event(10, 0.001), event(10, 0.002)]
        write_round(self.root, "round_001", "executor", pair)
        self._episode("cli_executor_episodes", "ep001", list(pair))
        ev = tap.cost_timeline(self.root, with_episodes=True)
        self.assertEqual(len(ev), 2)
        self.assertEqual(sum(e["cost"] for e in ev), 0.003)

    def test_role_name_survives_both_episode_dir_shapes(self) -> None:
        self._episode("cli_executor_episodes", "ep001", [event(100, 0.001)])
        self._episode("manager_episodes", "ep001", [event(120, 0.002)])
        roles = {e["role"] for e in tap.cost_timeline(self.root, with_episodes=True)}
        self.assertEqual(roles, {"executor", "manager"})

    def test_the_mirror_closes_a_cut_the_round_path_could_not_locate(self) -> None:
        self._episode("cli_executor_episodes", "ep001", [event(100, 0.001), event(200, 0.002)])
        write_round(self.root, "round_001", "executor", [event(9000, 0.004), event(9100, 0.003)])
        write_owner(self.root, 5.0)
        self.assertEqual(len(tap.cost_timeline(self.root)), 2, "只看 round 路径会漏掉停手前那两步")
        events = tap.cost_timeline(self.root, with_episodes=True)
        self.assertEqual(len(events), 4)
        sp = tap.split_at_detection(events, 2)          # probe counted exactly the 2 erased steps
        self.assertEqual(sp["error"], "")
        self.assertAlmostEqual(sp["cost_after_usd"], 0.007, places=9)
        att = tap.attempt_split(events, tap.attempt_boundary(self.root))
        self.assertEqual(att["steps_before"], 2)


class TestScoreRow(unittest.TestCase):
    """The row builder itself: a KeyError here used to kill the whole report after
    the batch had already been paid for, and a silently-absent flag would publish
    the number the flags exist to withhold. So the shape is asserted."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.old_runs = runner.RUNS_ROOT
        runner.RUNS_ROOT = self.root / "lh_runs"

    def tearDown(self) -> None:
        runner.RUNS_ROOT = self.old_runs
        self._tmp.cleanup()

    def _manifest(self, rid, **over):
        m = {"run_id": rid, "task_id": "T-X", "arm": "C-REC",
             "injected_at_provider_step": 1,
             "sensor": {"provider_step": 2, "detected_lag_steps": 1},
             "metrics": {"steps": 3, "cost": 0.007}}
        m.update(over)
        return m

    def test_row_carries_every_published_column(self) -> None:
        rid = "T-X-C1-csv-short-C-REC1-120000"
        run_dir = self.root / "lh_runs" / rid
        write_round(run_dir, "round_001", "manager", [event(100, 0.001)])
        write_round(run_dir, "round_001", "executor", [event(200, 0.002), event(300, 0.004)])
        write_owner(run_dir, 0.5)      # 边界 500ms，三条事件都在它之前
        row = cost_to_success.score_row(self._manifest(rid))
        missing = [k for k in cost_to_success.KEYS if k not in row]
        self.assertEqual(missing, [], "score_row 少列: %s" % missing)
        self.assertEqual(row["n_at_detect"], 2)
        self.assertEqual(row["events_total"], 3)
        self.assertAlmostEqual(row["cost_after_detection_usd"], 0.004, places=9)
        self.assertAlmostEqual(row["share_after_detection"], round(0.004 / 0.007, 3), places=9)
        self.assertEqual(row["events_before_boundary"], 3)
        self.assertEqual(row["attempt1_events_lost"], -1)
        self.assertIn("more_pre_halt_events_than_probe_counted", row["share_suspect"])

    def test_overwritten_first_attempt_is_flagged_so_the_share_is_not_published(self) -> None:
        rid = "T-X-C1-csv-short-C-REC2-120001"
        run_dir = self.root / "lh_runs" / rid
        write_round(run_dir, "round_001", "executor", [event(9000, 0.004), event(9100, 0.003)])
        write_owner(run_dir, 5.0)
        row = cost_to_success.score_row(self._manifest(rid))
        self.assertEqual(row["attempt1_events_lost"], 2)
        self.assertIn("attempt1_events_missing_2", row["share_suspect"])
        # 盘上只剩恢复那 2 步，切点落在时间线末尾 ⇒ 一个假的"零浪费"。
        # 这就是为什么这个数只能带 flag 出门，不能当中位数发布。
        self.assertEqual(row["share_after_detection"], 0.0)
        self.assertEqual(row["recovery_publishable"], "yes")             # 恢复成本本身仍可发布

    def test_reconciled_row_makes_the_after_detection_share_publishable(self) -> None:
        """The gate opens only when the probe's count and the surviving pre-halt
        events agree exactly — that is what makes the cut refer to something."""
        rid = "T-X-C1-csv-short-C-REC4-120003"
        run_dir = self.root / "lh_runs" / rid
        ep = run_dir / "lh_harness" / "cli_executor_episodes" / "ep001"
        ep.mkdir(parents=True, exist_ok=True)
        (ep / "provider_stream.jsonl").write_text(
            "\n".join([event(100, 0.001), event(200, 0.002)]) + "\n", encoding="utf-8")
        write_round(run_dir, "round_001", "executor", [event(9000, 0.004), event(9100, 0.003)])
        write_owner(run_dir, 5.0)
        row = cost_to_success.score_row(self._manifest(
            rid, metrics={"steps": 4, "cost": 0.003}))
        self.assertEqual(row["attempt1_events_lost"], 0)
        self.assertEqual(row["after_detect_publishable"], "yes")
        self.assertAlmostEqual(row["share_after_detection"], 0.7, places=9)
        self.assertEqual(row["episodes_only_steps"], 2)

    def test_live_halt_sample_is_marked_as_the_better_source(self) -> None:
        """Runs sampled at the halt carry a number that does not depend on the
        interrupted attempt surviving on disk, so the row must say which it is."""
        rid = "T-X-C1-csv-short-C-REC5-120004"
        run_dir = self.root / "lh_runs" / rid
        write_round(run_dir, "round_001", "executor", [event(100, 0.001), event(200, 0.002)])
        write_owner(run_dir, 5.0)
        row = cost_to_success.score_row(self._manifest(
            rid, at_halt={"steps_after_detection": 0, "cost_after_detection_usd": 0.0,
                          "seconds_after_detection": 1.0, "unclosed_steps": 1,
                          "mid_flight_step_cut": True}))
        self.assertEqual(row["after_detect_source"], "live")
        self.assertEqual(row["steps_after_detection_live"], 0)
        self.assertIs(row["mid_flight_step_cut"], True)
        other = cost_to_success.score_row(self._manifest(rid))
        self.assertEqual(other["after_detect_source"], "offline")

    def test_no_owner_record_means_no_recovery_number(self) -> None:
        rid = "T-X-C1-csv-short-C-REC3-120002"
        run_dir = self.root / "lh_runs" / rid
        write_round(run_dir, "round_001", "executor", [event(100, 0.001), event(200, 0.002)])
        row = cost_to_success.score_row(
            self._manifest(rid, metrics={"steps": 2, "cost": 0.003}))
        self.assertIn("no_owner_file", row["share_suspect"])
        self.assertIsNone(row["cost_recovery_usd"])
        self.assertEqual(row["recovery_publishable"], "")

    def test_provider_step_count_comes_from_events_not_the_manifest(self) -> None:
        """metrics.provider_steps is missing from 43 of 44 manifests; leaving the
        column blank would silently drop it from every downstream table."""
        rid = "T-PIPE-01F-A1-120003"
        write_round(self.root / "lh_runs" / rid, "round_001", "executor",
                    [event(100, 0.001), event(200, 0.002)])
        row = cost_to_success.score_row({
            "run_id": rid, "task_id": "T-PIPE-01F", "arm": "A1",
            "metrics": {"steps": 5, "cost": 0.003}})
        self.assertEqual(row["provider_steps"], 2)
        self.assertNotIn("n_at_detect", row)      # no sensor => no detection columns at all


if __name__ == "__main__":
    unittest.main(verbosity=2)
