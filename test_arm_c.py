"""Offline tests for the C arms: the injection round-trip must be byte-exact,
and the breakpoint bookkeeping must not invent a step number.

Nothing here calls the provider or the control API.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest

LINUX = sys.platform.startswith("linux")   # predicates shell out to python3 and POSIX paths, so off-Linux these suites skip, not fail
from pathlib import Path
from unittest import mock

import arm_c
import predicates
import runner

CSV12 = "order_id,date,region,amount\n" + "".join(
    f"O{i:03d},2026-01-0{i % 3 + 1},{'east' if i % 2 else 'west'},{i * 10}.5\n" for i in range(12)
)
CARD = {"id": "c-orders-cardinality", "predicate": "csv_row_count", "applies_to": "workspace",
        "args": {"path": "data/orders.csv", "rows": 12}}
FILE = {"id": "c-orders-file", "predicate": "artifact_exists", "applies_to": "workspace",
        "args": {"path": "data/orders.csv"}}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="armctest_"))
        self.ws = self.tmp / "ws"
        (self.ws / "data").mkdir(parents=True)
        (self.ws / "data" / "orders.csv").write_text(CSV12, encoding="utf-8")
        self.real_cstate = arm_c.CSTATE
        arm_c.CSTATE = self.tmp / "cstate"

    def tearDown(self) -> None:
        arm_c.CSTATE = self.real_cstate
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBreakpoint(Base):
    def test_injection_then_restore_is_byte_exact(self) -> None:
        original = (self.ws / "data" / "orders.csv").read_text(encoding="utf-8")
        bp = arm_c.Breakpoint("C1-csv-short", self.ws, "runT")
        man = bp.apply()
        self.assertNotEqual(man["sha_before"], man["sha_after"])
        self.assertEqual(len([l for l in (self.ws / "data" / "orders.csv").read_text(encoding="utf-8").splitlines() if l.strip()]), 12)
        self.assertTrue(bp.restore())
        self.assertEqual((self.ws / "data" / "orders.csv").read_text(encoding="utf-8"), original)
        self.assertTrue((self.tmp / "cstate" / "runT" / "manifest.json").is_file())

    def test_injection_of_a_missing_file_refuses_instead_of_lying(self) -> None:
        bp = arm_c.Breakpoint("C2-stale-summary", self.ws, "runT2")
        with self.assertRaises(RuntimeError) as ctx:
            bp.apply()
        self.assertIn("does not exist yet", str(ctx.exception))

    def test_recipe_reaches_a_red_the_sensor_can_see(self) -> None:
        bp = arm_c.Breakpoint("C1-csv-short", self.ws, "runT3")
        bp.apply()
        tick = arm_c.sensor_tick([CARD, FILE], self.ws)
        self.assertEqual(tick["c-orders-cardinality"]["status"], "fail")
        self.assertIn("11", tick["c-orders-cardinality"]["actual"])
        self.assertEqual(tick["c-orders-file"]["status"], "pass")
        bp.restore()
        self.assertEqual(arm_c.sensor_tick([CARD], self.ws)["c-orders-cardinality"]["status"], "pass")


class TestBreakpointBookkeeping(Base):
    def setUp(self):
        super().setUp()
        green = {"step": 5, "cost_usd": 0.0030, "t": 1000.0, "unclosed": 0,
                 **{k: {"status": "pass", "expected": "e", "actual": "a"} for k in (CARD["id"], FILE["id"])}}
        red = {"step": 7, "cost_usd": 0.0055, "t": 1010.5, "unclosed": 1,
               **{CARD["id"]: {"status": "fail", "expected": "12 data rows", "actual": "11 data rows"},
                  FILE["id"]: {"status": "pass", "expected": "e", "actual": "a"}}}
        self.reading = [green, red]

    def test_first_red_carries_the_lag_and_the_promise(self) -> None:
        red = arm_c._first_new_red(self.reading, injected_at=5)
        self.assertEqual(red["commitment_id"], "c-orders-cardinality")
        self.assertEqual(red["provider_step"], 7)
        self.assertEqual(red["detected_lag_steps"], 2)
        self.assertEqual(red["actual"], "11 data rows")

    def test_last_green_is_the_rollback_target(self) -> None:
        red = arm_c._first_new_red(self.reading, injected_at=5)
        self.assertEqual(arm_c._last_green_before(self.reading, red["index"], red["commitment_id"]), 5)

    def test_a_red_that_was_never_green_is_not_the_finding(self) -> None:
        """Rule v1 (DESIGN #19): 96.7% of injected runs already read red before any
        drift existed. A red only counts once this same run has seen that promise
        pass, and that no longer requires knowing where the defect was planted.
        """
        only_baseline_red = [{"step": 3, "c-a": {"status": "fail", "expected": "e", "actual": "x"}},
                             {"step": 5, "c-a": {"status": "pass", "expected": "e", "actual": "ok"}}]
        self.assertIsNone(arm_c._first_new_red(only_baseline_red, injected_at=None))
        broken = {"step": 3, "c-x": {"status": "fail", "expected": "e", "actual": "already red"}}
        # the step-3 red has no green in front of it, so the finding is step 7 even
        # when the injection predates it -- this is the assertion that changed
        self.assertEqual(arm_c._first_new_red([broken] + self.reading, injected_at=2)["provider_step"], 7)
        self.assertEqual(arm_c._first_new_red([broken] + self.reading, injected_at=5)["provider_step"], 7)

    def test_no_red_returns_none_rather_than_a_guess(self) -> None:
        all_green = [{"step": 1, "c-a": {"status": "pass", "expected": "", "actual": ""}}]
        self.assertIsNone(arm_c._first_new_red(all_green, injected_at=1))
        self.assertIsNone(arm_c._last_green_before(all_green, 0, "c-a"))

    def test_the_progress_scalars_are_not_mistaken_for_promises(self) -> None:
        """The reading row now carries cost/time/unclosed next to the per-promise
        dicts. A loop that keys on "anything that is not `step`" would turn the
        sampler's own fields into findings."""
        red = arm_c._first_new_red(self.reading, injected_at=5)
        self.assertEqual(red["commitment_id"], CARD["id"])
        self.assertEqual(red["cost_usd_at_detect"], 0.0055)
        self.assertEqual(red["t_at_detect"], 1010.5)
        self.assertEqual(red["unclosed_at_detect"], 1)


class TestHaltGap(unittest.TestCase):
    """The live half of the cost story: what the run spent between the sensor
    knowing and the run lying still. Offline this is unrecoverable once the
    resume rewrites the round files."""

    RED = {"provider_step": 7, "cost_usd_at_detect": 0.0055, "t_at_detect": 1010.5}

    def test_deltas_are_against_the_detection_sample(self) -> None:
        halt = {"steps": 9, "cost_usd": 0.0080, "unclosed_steps": 0,
                "last_event_ts": 1099000.0, "last_event_type": "step_finish"}
        gap = arm_c.halt_gap(self.RED, halt, t_now=1014.0)
        self.assertEqual(gap["steps_after_detection"], 2)
        self.assertAlmostEqual(gap["cost_after_detection_usd"], 0.0025, places=9)
        self.assertAlmostEqual(gap["seconds_after_detection"], 3.5, places=1)
        self.assertFalse(gap["mid_flight_step_cut"])
        self.assertEqual(gap["last_event_type"], "step_finish", "halt 采样本身要留在记录里")

    def test_a_cut_step_with_no_cost_delta_is_recorded_as_such(self) -> None:
        """The exact case the artifacts cannot show afterwards: a step opened, the
        halt landed, nothing was billed to a `step_finish`. Reporting only the
        zero delta would read as 'the halt was free'."""
        halt = {"steps": 7, "cost_usd": 0.0055, "unclosed_steps": 1,
                "last_event_ts": 1099000.0, "last_event_type": "step_start"}
        gap = arm_c.halt_gap(self.RED, halt, t_now=1011.5)
        self.assertEqual(gap["steps_after_detection"], 0)
        self.assertEqual(gap["cost_after_detection_usd"], 0.0)
        self.assertTrue(gap["mid_flight_step_cut"])

    def test_missing_detection_sample_gives_none_not_zero(self) -> None:
        halt = {"steps": 8, "cost_usd": 0.009, "unclosed_steps": 0,
                "last_event_ts": None, "last_event_type": None}
        gap = arm_c.halt_gap({"provider_step": 6}, halt)
        self.assertIsNone(gap["cost_after_detection_usd"])
        self.assertIsNone(gap["seconds_after_detection"])
        self.assertEqual(gap["steps_after_detection"], 2)


class TestDirectiveFile(Base):
    def test_recovery_md_names_the_step_and_the_measurements(self) -> None:
        lines = [
            "# RECOVERY.md — 机器生成的断点说明",
            "- 探针在第 7 个 provider 步发现不变量被破坏（出处 `rule:c-orders-cardinality@step7`）。",
            "- 实测：11 data rows",
            "- 要求：12 data rows",
            "- 上一个全部成立的步：第 5 步。之前的产物视为可信。",
        ]
        path = arm_c.write_recovery_md(self.ws, lines)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(path.name, "RECOVERY.md")
        self.assertIn("rule:c-orders-cardinality@step7", text)
        self.assertIn("11 data rows", text)
        self.assertNotIn("请重新完成任务", text)


class TestPredicateStillRegistered(Base):
    def test_registry_has_what_the_arms_call(self) -> None:
        for name in ("csv_row_count", "artifact_exists", "csv_unique_key", "json_matches_csv_aggregate"):
            self.assertIn(name, predicates.REGISTRY)


class _Args:
    task = "T-PIPE-01F-C1-csv-short"
    arm = "C-ROLL"
    recipe = "C1-csv-short"
    inject_after = 6
    minutes = 0
    extra_rounds = 1


class _NullBP:
    manifest = {}

    def apply(self) -> None:
        raise AssertionError("the deadline is zero, so nothing should be injected")

    def restore(self) -> bool:
        return True


class TestInjectGate(unittest.TestCase):
    """Injection must wait until the promise it is about to break reads green.
    Otherwise the "detection" measures the seed's own defect, and lag comes out
    flattering at 0-1 steps (measured: 3 of 26 valid samples)."""

    def _reading(self, status):
        return [{"step": 9, "c-orders-cardinality": {"status": status, "expected": "12", "actual": "11"}}]

    def test_gate_reads_the_recipes_own_root_cause(self) -> None:
        root, state = arm_c.inject_gate_state(self._reading("pass"), "C1-csv-short")
        self.assertEqual(root, "c-orders-cardinality")
        self.assertEqual(state, "pass")
        self.assertEqual(arm_c.inject_gate_state(self._reading("fail"), "C1-csv-short")[1], "fail")

    def test_missing_evidence_is_none_and_none_blocks(self) -> None:
        self.assertEqual(arm_c.inject_gate_state([{"step": 1}], "C1-csv-short"),
                         ("c-orders-cardinality", None))
        self.assertEqual(arm_c.inject_gate_state([], "C1-csv-short"), ("c-orders-cardinality", None))

    def test_only_the_newest_reading_counts(self) -> None:
        rd = self._reading("pass") + self._reading("fail")
        self.assertEqual(arm_c.inject_gate_state(rd, "C1-csv-short")[1], "fail")


@unittest.skipUnless(LINUX, "drives taskset/predicates that assume a POSIX layout")
class TestDriverWiring(unittest.TestCase):
    """The 8-run rep-12 batch died in 25 seconds with `NameError: rec` and spent
    nothing: `main()` passed a `rec` it never defined, because the refactor that
    moved the body into `_run_body` left the record being created *there*. Unit
    tests of the pure helpers could not see it — only calling `main()` can.

    This test fakes the control API and gives the poll loop a deadline of zero,
    so it exercises create → body → cleanup → manifest without a provider call.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "ws").mkdir()
        (root / "runs").mkdir()
        (root / "manifests").mkdir()
        self.patches = [
            mock.patch.object(runner, "ping", lambda *a, **k: None),
            mock.patch.object(runner, "WS_ROOT", root / "ws"),
            mock.patch.object(runner, "RUNS_ROOT", root / "runs"),
            mock.patch.object(runner, "MANIFESTS", root / "manifests"),
            mock.patch.object(arm_c.runner, "call", self._fake_call),
            mock.patch.object(arm_c, "load_commitments", lambda task: []),
            mock.patch.object(arm_c.time, "strftime", lambda *a, **k: "1970-01-01 00:00:00"),
        ]

    @staticmethod
    def _fake_call(method, path, body=None):
        if path == "/api/runs":
            return 200, {"run_id": body["run_id"]}
        return 200, {"alive": False, "status": "cancelled"}

    def tearDown(self):
        shutil.rmtree(self._tmp.name, ignore_errors=True)

    def _run(self, argv):
        for p in self.patches:
            p.start()
        try:
            with mock.patch.object(sys, "argv", ["arm_c.py"] + argv):
                code = arm_c.main()
            return code, sorted((runner.MANIFESTS).glob("*.json"))
        finally:
            for p in self.patches:
                p.stop()

    def test_main_writes_a_manifest_and_returns_a_contract_code(self) -> None:
        code, manifests = self._run(["--task", "T-PIPE-01F-C1-csv-short", "--recipe", "C1-csv-short",
                                     "--arm", "C-ROLL", "--rep", "99", "--minutes", "0"])
        self.assertEqual(len(manifests), 1, "崩溃也必须留下 manifest，否则这条 run 在数据里消失")
        rec = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(rec["task_id"], "T-PIPE-01F-C1-csv-short")
        self.assertEqual(rec["arm"], "C-ROLL")
        self.assertIn("aborted", rec, "没检出就该写明为什么没检出")
        self.assertIn(code, (arm_c.EXIT_PROVIDER_DEAD, arm_c.EXIT_NO_DETECT, 0))

    def test_run_body_fills_the_callers_record_instead_of_rebinding_it(self) -> None:
        """A `_run_body` that assigns its own `rec` leaves main writing an empty
        shell — the record would look like a run that did nothing."""
        for p in self.patches:
            p.start()
        try:
            rec = {"run_id": "r-1", "attempts": []}
            with mock.patch.object(arm_c.time, "time", lambda: 0.0):
                arm_c._run_body(_Args(), rec, "r-1", Path("/tmp"), _NullBP())
            self.assertEqual(rec["run_id"], "r-1", "_run_body 覆写了调用方的记录")
            self.assertIn("readings", rec)
        finally:
            for p in self.patches:
                p.stop()


class TestNewRedTrigger(unittest.TestCase):
    """DESIGN #19: the trigger may no longer lean on `injected_at`.

    G3 measured that 29 of 30 injected runs already carried a red reading before
    any drift was planted, because the family seeds ship broken. The old rule
    dodged them only by knowing where the defect had been put, which a production
    harness does not know. Every case below passes `injected_at=None`.
    """

    @staticmethod
    def _row(step: int, **states: str) -> dict:
        row = {"step": step, "cost_usd": 0.0, "t": float(step), "unclosed": 0}
        for cid, st in states.items():
            row[cid] = {"status": st, "expected": "e", "actual": "a"}
        return row

    def test_baseline_red_never_fires(self) -> None:
        reading = [self._row(1, c_a="fail"), self._row(2, c_a="fail"), self._row(3, c_a="fail")]
        self.assertIsNone(arm_c._first_new_red(reading, None))

    def test_na_is_not_green(self) -> None:
        reading = [self._row(1, c_a="na"), self._row(2, c_a="fail")]
        self.assertIsNone(arm_c._first_new_red(reading, None))

    def test_green_then_red_fires_with_no_injection_knowledge(self) -> None:
        reading = [self._row(3, c_a="pass"), self._row(5, c_a="fail")]
        got = arm_c._first_new_red(reading, None)
        self.assertEqual(got["commitment_id"], "c_a")
        self.assertEqual(got["green_at_provider_step"], 3)
        self.assertIsNone(got["detected_lag_steps"])

    def test_lag_is_still_measured_when_the_experiment_injects(self) -> None:
        reading = [self._row(3, c_a="pass"), self._row(7, c_a="fail")]
        got = arm_c._first_new_red(reading, 6)
        self.assertEqual(got["detected_lag_steps"], 1)

    def test_one_green_then_many_reds_fires_once_at_the_first(self) -> None:
        reading = [self._row(2, c_a="pass"), self._row(4, c_a="fail"), self._row(6, c_a="fail")]
        got = arm_c._first_new_red(reading, None)
        self.assertEqual(got["provider_step"], 4)


class TestInPlayGate(Base):
    """A promise is judged only once its own subject has entered the arena."""

    CSV11 = "order_id,date,region,amount\n" + "".join(
        f"O{i:03d},2026-01-01,east,{i}.5\n" for i in range(11))

    def setUp(self) -> None:
        super().setUp()
        self.seed = self.tmp / "seed"
        shutil.copytree(self.ws, self.seed)

    def test_untouched_promise_reads_na_not_a_red(self) -> None:
        (self.ws / "data" / "orders.csv").write_text(self.CSV11, encoding="utf-8")
        (self.seed / "data" / "orders.csv").write_text(self.CSV11, encoding="utf-8")
        got = arm_c.sensor_tick([CARD], self.ws, self.seed)
        self.assertEqual(got["c-orders-cardinality"]["status"], "na")
        self.assertTrue(got["c-orders-cardinality"]["actual"].startswith("not_in_play"))

    def test_modified_bytes_put_the_promise_in_play(self) -> None:
        got = arm_c.sensor_tick([CARD], self.ws, self.seed)   # ws identical to seed here
        self.assertEqual(got["c-orders-cardinality"]["status"], "na")
        (self.ws / "data" / "orders.csv").write_text(self.CSV11, encoding="utf-8")
        live: set[str] = set()
        got = arm_c.sensor_tick([CARD], self.ws, self.seed, live)
        self.assertEqual(got["c-orders-cardinality"]["status"], "fail")
        self.assertIn("c-orders-cardinality", live)

    def test_a_new_file_counts_as_in_play(self) -> None:
        card = {"id": "c-extra", "predicate": "artifact_exists", "applies_to": "workspace",
                "args": {"path": "data/extra.csv"}}
        self.assertEqual(arm_c.sensor_tick([card], self.ws, self.seed)[
            "c-extra"]["status"], "na")
        (self.ws / "data" / "extra.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        self.assertEqual(arm_c.sensor_tick([card], self.ws, self.seed)[
            "c-extra"]["status"], "pass")

    def test_in_play_is_sticky_when_the_agent_restores_the_seed_bytes(self) -> None:
        """Returning a file to its exact seed content must not erase its history."""
        (self.ws / "data" / "orders.csv").write_text(self.CSV11, encoding="utf-8")
        live: set[str] = set()
        arm_c.sensor_tick([CARD], self.ws, self.seed, live)
        shutil.copyfile(self.seed / "data" / "orders.csv", self.ws / "data" / "orders.csv")
        got = arm_c.sensor_tick([CARD], self.ws, self.seed, live)
        self.assertNotEqual(got["c-orders-cardinality"]["status"], "na")

    def test_a_promise_without_paths_is_always_judged(self) -> None:
        self.assertTrue(arm_c.in_play(self.seed, self.ws, {"args": {}}))
        self.assertTrue(arm_c.in_play(None, self.ws, CARD))


class TestTriggerEndToEnd(unittest.TestCase):
    """The rule change only counts if the real loop takes the new path: an agent
    that breaks a promise it had itself repaired, with no injection at all, must
    exit on its own code and never reach the rollback."""

    CSV11 = "order_id,date,region,amount\n" + "".join(
        f"O{i:03d},2026-01-01,east,{i}.5\n" for i in range(11))

    def test_agent_drift_before_any_injection_returns_its_own_code(self) -> None:
        import argparse

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        ws, seed = root / "ws", root / "T" / "seed"
        for base in (ws, seed):
            (base / "data").mkdir(parents=True)
            (base / "data" / "orders.csv").write_text(self.CSV11, encoding="utf-8")
        (root / "runs").mkdir()
        state = {"steps": 0}

        def fake_call(method, path, body=None):
            state["steps"] += 1
            if state["steps"] == 2:
                (ws / "data" / "orders.csv").write_text(CSV12, encoding="utf-8")   # agent repairs
            if state["steps"] == 4:
                (ws / "data" / "orders.csv").write_text(self.CSV11, encoding="utf-8")   # agent regresses
            return 200, {"alive": True, "status": "running"}

        class NeverInject:
            manifest: dict = {}

            def apply(self) -> None:
                raise AssertionError("the injection must not be reached in this test")

        args = argparse.Namespace(task="T", arm="C-ROLL", recipe="C1-csv-short", rep=1,
                                  minutes=1, max_rounds=2, inject_after=99, extra_rounds=1)
        rec: dict = {"readings": []}
        patches = [
            mock.patch.object(arm_c.runner, "call", fake_call),
            mock.patch.object(arm_c.runner, "RUNS_ROOT", root / "runs"),
            mock.patch.object(arm_c, "load_commitments", lambda task: [CARD]),
            mock.patch.object(arm_c.sbr_paths, "TASKS", root),
            mock.patch.object(arm_c.tap, "live_progress",
                              lambda d: {"steps": state["steps"], "cost_usd": 0.0,
                                         "unclosed_steps": 0, "last_event_ts": None,
                                         "last_event_type": None}),
            mock.patch.object(arm_c, "POLL_SECONDS", 0),
        ]
        for p in patches:
            p.start()
        try:
            code = arm_c._run_body(args, rec, "T-C-ROLL1-000000", ws, NeverInject())
        finally:
            for p in patches:
                p.stop()
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(code, arm_c.EXIT_AGENT_DRIFT)
        self.assertEqual(rec["aborted"], "agent_drift_before_injection")
        self.assertEqual(rec["sensor"]["drift_source"], "agent")
        self.assertIsNone(rec["sensor"]["detected_lag_steps"])
        self.assertNotIn("injected_at_provider_step", rec)
        self.assertEqual(rec["trigger_rule"], "new_red_after_green_v1")


class TestDArm(unittest.TestCase):
    """D is the control: same drift, same detector, and then *nothing* intervenes.

    The assertions that matter are the absences. A stop call, a rollback or a
    RECOVERY.md anywhere in a D run would silently turn the control into the
    treatment and the whole cost comparison would be reading its own artifact.
    """

    CSV11 = "order_id,date,region,amount\n" + "".join(
        f"O{i:03d},2026-01-01,east,{i}.5\n" for i in range(11))

    def test_d_detects_but_never_stops_or_rewinds(self) -> None:
        import argparse

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        ws, seed = root / "ws", root / "T" / "seed"
        for base in (ws, seed):
            (base / "data").mkdir(parents=True)
            (base / "data" / "orders.csv").write_text(self.CSV11, encoding="utf-8")
        (root / "runs").mkdir()
        calls, state = [], {"steps": 0}

        def fake_call(method, path, body=None):
            calls.append((method, path))
            if path == "/api/runs":
                return 200, {"run_id": "T-D1-000000"}
            state["steps"] += 1
            if state["steps"] == 2:
                (ws / "data" / "orders.csv").write_text(CSV12, encoding="utf-8")   # agent repairs
            alive = state["steps"] < 4
            return 200, {"alive": alive, "status": "running" if alive else "completed"}

        class FakeBreakpoint:
            manifest = {"recipe": "C1-csv-short", "root_cause": "c-orders-cardinality"}
            restored = False
            broken = self.CSV11
            fixed = CSV12

            def apply(self):
                (ws / "data" / "orders.csv").write_text(self.broken, encoding="utf-8")   # the drift

            def restore(self):
                FakeBreakpoint.restored = True
                return True

        driven: list = []

        def fake_drive(rid, max_minutes=1, settle_seconds=90):
            driven.append(rid)
            return {"status": "completed"}

        # The pilot crashed here seven times: `files` is keyed by path *strings*,
        # and inject.sha wants a Path. An empty manifest in the fake let the bug
        # through, so this fixture must carry a real target file.
        FakeBreakpoint.manifest = {"recipe": "C1-csv-short",
                                   "root_cause": "c-orders-cardinality",
                                   "cascade": ["c-orders-cardinality"],
                                   "files": {str(ws / "data" / "orders.csv"):
                                             str(root / "backup" / "orders.csv")}}
        args = argparse.Namespace(task="T", arm="D", recipe="C1-csv-short", rep=1,
                                  minutes=1, max_rounds=3, inject_after=1, extra_rounds=1)
        rec: dict = {"readings": [], "attempts": []}
        written: list = []
        patches = [
            mock.patch.object(arm_c.runner, "call", fake_call),
            mock.patch.object(arm_c.runner, "RUNS_ROOT", root / "runs"),
            mock.patch.object(arm_c, "load_commitments", lambda task: [CARD]),
            mock.patch.object(arm_c.sbr_paths, "TASKS", root),
            mock.patch.object(arm_c, "Breakpoint", FakeBreakpoint),
            mock.patch.object(arm_c.tap, "live_progress",
                              lambda d: {"steps": state["steps"], "cost_usd": 0.01,
                                         "unclosed_steps": 0, "last_event_ts": None,
                                         "last_event_type": None}),
            mock.patch.object(arm_c, "POLL_SECONDS", 0),
            mock.patch.object(arm_c.runner, "drive_to_terminal", fake_drive),
            # The pilot's stall: a gate nobody answers freezes the step counter
            # while the driver keeps sampling green. One pending gate per poll.
            mock.patch.object(arm_c.runner, "pending_approval",
                              lambda rid: "ap1" if state["steps"] == 2 else None),
            mock.patch.object(arm_c.runner, "collect", lambda rid: {"steps": 40, "cost": 0.02}),
            mock.patch.object(arm_c.runner, "read_report", lambda rid: {"report_status": "complete"}),
            mock.patch.object(arm_c.runner, "exit_code_for", lambda m: 0),
            mock.patch.object(arm_c, "terminal_verdict",
                              lambda t, rid, w: {"ledger_verdict": "satisfied"}),
            mock.patch.object(arm_c, "_write", lambda r, rid: written.append(rid)),
        ]
        for p in patches:
            p.start()
        try:
            code = arm_c._run_body(args, rec, "T-D1-000000", ws, FakeBreakpoint())
        finally:
            for p in patches:
                p.stop()
            shutil.rmtree(root, ignore_errors=True)

        self.assertEqual(code, 0)
        self.assertGreaterEqual(rec["sensor"]["provider_step"], 3, "the detector must still fire")
        self.assertGreaterEqual(rec["sensor"]["detected_lag_steps"], 1)
        self.assertTrue(rec["no_rewind"])
        self.assertEqual(FakeBreakpoint.restored, False, "D must never roll the workspace back")
        self.assertNotIn("stopped_before_resume", rec)
        self.assertFalse((ws / "RECOVERY.md").exists())
        self.assertEqual([c for c in calls if c[1].endswith("/stop")], [], "D must never stop the run")
        self.assertEqual(written, ["T-D1-000000"])
        self.assertEqual(rec["attempts"][-1]["kind"], "post-drift-uninterrupted")
        # The pilot died on this: the poll loop does not resolve completion gates,
        # so a D run that is left alone sits in waiting_approval until the deadline.
        # Handing the rest to drive_to_terminal is what keeps it moving.
        self.assertEqual(driven, ["T-D1-000000"], "D must hand the remaining rounds to drive_to_terminal")
        self.assertEqual(rec["gates_answered"], 1, "the pending gate must be answered while polling")
        resolved = [c for c in calls if c[1].endswith("/resolve")]
        self.assertEqual(len(resolved), 1)
        # Both instruments the pilot lacked: was the drift visible the instant after
        # we wrote it, and did the bytes survive until the next poll?
        self.assertEqual(rec["injection_visible"], {"c-orders-cardinality": "fail"})
        hashes = rec.get("drift_file_hashes") or []
        self.assertTrue(hashes, "the injected file must be hashed per poll after injection")
        self.assertTrue(all(len(h.get("sha", "")) == 64 for h in hashes if "sha" in h))
        self.assertIn("sha", hashes[0], "the first sample is taken at injection time")

    def test_resolve_gate_payload(self) -> None:
        seen = []
        with mock.patch.object(arm_c.runner, "pending_approval", lambda rid: "ap9"), \
                mock.patch.object(arm_c.runner, "call",
                                  lambda m, p, b=None: seen.append((m, p, b)) or (200, {})):
            self.assertEqual(arm_c.resolve_gate("r1"), "ap9")
        method, path, body = seen[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/runs/r1/approvals/ap9/resolve")
        self.assertEqual(body["action"], "stop", "same gate policy as the batch runner")
        self.assertIn("arm D", body["reason"], "the artifact must say who answered")
        with mock.patch.object(arm_c.runner, "pending_approval", lambda rid: None):
            self.assertIsNone(arm_c.resolve_gate("r1"))


class TestCRollNeverAnswersGatesInLoop(unittest.TestCase):
    """The gate-answering fix is D-only: a C arm reaches its gate by stopping the
    run itself, so answering it from the loop would erase the thing being measured."""

    def test_loop_guard_is_d_only(self) -> None:
        import inspect

        src = inspect.getsource(arm_c._run_body)
        self.assertIn('if args.arm == "D" and snap.get("alive") and resolve_gate(run_id):', src)
        self.assertEqual(src.count("resolve_gate(run_id)"), 1, "exactly one in-loop gate answer")


class TestBudgetIsRecorded(unittest.TestCase):
    """A cheaper arm that was given less room is not a saving, so the room has to
    travel with the record."""

    def test_flags_exist_for_every_field_the_comparer_reads(self) -> None:
        import inspect

        src = inspect.getsource(arm_c.main)
        for flag in ("--arm", "--recipe", "--rep", "--inject-after", "--max-rounds",
                     "--minutes", "--extra-rounds"):
            self.assertIn(flag, src)
        self.assertIn('"D"', src, "the D arm must be selectable from the CLI")
        body = inspect.getsource(arm_c.main)
        for key in ("max_rounds", "minutes", "extra_rounds", "inject_after"):
            self.assertIn(f'"{key}"', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
