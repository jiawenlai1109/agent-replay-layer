"""Four-way check for every family-A task, as a regression test.

The three assertions per task:
  buggy tree  -> oracle FAILS   (the judgement is discriminating)
  fixed tree  -> oracle PASSES  (the judgement is satisfiable at all)
  seed tree   -> test files untouched
The middle one is the one that caught the src/ layout import trap; a red/green
pair alone does NOT prove the oracle works.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

LINUX = sys.platform.startswith("linux")   # predicates shell out to python3 and POSIX paths, so off-Linux these suites skip, not fail

import predicates

HOME = pathlib.Path.home()
SBR = HOME / "sbr"
WORK = HOME / "tasks"

TASKS = {
    "A1-PACKAGING-577": ("bs_packaging", "ps_packaging"),
    "A2-LOGURU-1435": ("bs_loguru", "ps_loguru"),
    "A3-ISORT-1815": ("bs_isort", "ps_isort"),
    "A4-STRUCTLOG-710": ("bs_structlog", "ps_structlog"),
}


def _commitments(task_id: str) -> dict:
    path = SBR / "ledger" / task_id / "commitments.json"
    return {c["id"]: c for c in json.loads(path.read_text(encoding="utf-8"))}


def _seed(task_id: str) -> pathlib.Path:
    return SBR / "tasks" / task_id / "seed"


@unittest.skipUnless(LINUX, "predicates run the upstream test suite as a subprocess")
class TestFamilyA(unittest.TestCase):
    def _skip_unless_ready(self, task_id: str, buggy: str, fixed: str) -> dict | None:
        cs_path = SBR / "ledger" / task_id / "commitments.json"
        if not cs_path.is_file() or not (WORK / buggy).is_dir() or not (WORK / fixed).is_dir():
            self.skipTest(f"{task_id} or its fixtures not built")
        return _commitments(task_id)

    def test_a1_packaging(self) -> None:
        self._run("A1-PACKAGING-577", *TASKS["A1-PACKAGING-577"])

    def test_a2_loguru(self) -> None:
        self._run("A2-LOGURU-1435", *TASKS["A2-LOGURU-1435"])

    def test_a3_isort(self) -> None:
        self._run("A3-ISORT-1815", *TASKS["A3-ISORT-1815"])

    def test_a4_structlog(self) -> None:
        self._run("A4-STRUCTLOG-710", *TASKS["A4-STRUCTLOG-710"])

    def _run(self, task_id: str, buggy: str, fixed: str) -> None:
        cs = self._skip_unless_ready(task_id, buggy, fixed)
        if cs is None:
            return
        oracle = cs["c-oracle-named-test"]["args"]
        guard = cs["c-test-files-untouched"]["args"]

        self.assertTrue(oracle["node_ids"], f"{task_id}: 台账里没有具名用例")
        prov = json.loads((SBR / "ledger" / task_id / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(oracle["node_ids"]),
            sorted(prov["measured_failing_node_ids"]),
            f"{task_id}: 台账具名用例与实测记录不一致",
        )

        seed = _seed(task_id)
        self.assertTrue(seed.is_dir(), f"{task_id}: seed 缺失")
        for rel in guard["sha256"]:
            self.assertEqual(
                predicates.files_unmodified({**guard, "workspace": str(seed)})["status"],
                "pass",
                f"{task_id}: seed 里的判据文件与台账哈希不符 {rel}",
            )

        buggy_verdict = predicates.oracle_named_tests_pass({**oracle, "workspace": str(WORK / buggy)})
        self.assertEqual(buggy_verdict["status"], "fail", f"{task_id}: buggy 树竟然过了判据 → 判据无区分力")

        fixed_verdict = predicates.oracle_named_tests_pass({**oracle, "workspace": str(WORK / fixed)})
        self.assertEqual(
            fixed_verdict["status"],
            "pass",
            f"{task_id}: 修好的树过不了自己的判据 → 判据不可满足（查 src/ 布局与 PYTHONPATH）：{fixed_verdict['actual'][:200]}",
        )

    def test_oracle_files_absent_from_task_text(self) -> None:
        """The file carrying each named judgement must differ between seed and oracle.

        Overlay entries the PR never touched (e.g. a conftest carried along for
        fixtures) are legitimately byte-identical to the seed — requiring every
        overlay file to differ would fail on those without proving anything.
        """
        for task_id in TASKS:
            seed = _seed(task_id)
            if not seed.is_dir():
                self.skipTest(f"{task_id} seed not built")
                return
            oracle_dir = SBR / "ledger" / task_id / "oracle"
            cs = _commitments(task_id)
            args = cs["c-oracle-named-test"]["args"]
            carriers = sorted({node.split("::", 1)[0] for node in args["node_ids"]})
            self.assertTrue(carriers, f"{task_id}: 无具名用例")
            for rel in carriers:
                seed_file, oracle_file = seed / rel, oracle_dir / rel
                self.assertTrue(seed_file.is_file() and oracle_file.is_file(), f"{task_id}: {rel} 缺失")
                self.assertNotEqual(
                    seed_file.read_bytes(),
                    oracle_file.read_bytes(),
                    f"{task_id}: 判据文件 {rel} 的 seed 版本与隐藏判据相同 → 判据已泄露给 agent",
                )


@unittest.skipUnless(LINUX, "predicates run the upstream test suite as a subprocess")
class TestDifferentialRegression(unittest.TestCase):
    """Whole-file checks are only sound against a repeatedly-measured baseline.

    Measured: structlog's test_processors.py reports a different failing set on
    every run (18 red per run, 28 distinct across three), while packaging /
    loguru / isort are deterministic (union == intersection). A single-run diff
    on structlog invents regressions.
    """

    def _args(self, tid):
        cs = _commitments(tid)
        c = cs.get("c-differential-no-new-failure")
        if c is None:
            self.skipTest(f"{tid} has no differential commitment")
        return c["args"]

    def test_no_agent_workspace_shows_new_failures(self) -> None:
        import predicates

        for tid in TASKS:
            try:
                args = self._args(tid)
            except Exception:
                continue
            for run in sorted((SBR / "manifests").glob("%s-A1-*.json" % tid)):
                d = json.loads(run.read_text(encoding="utf-8"))
                ws = pathlib.Path(d.get("workspace", ""))
                if not ws.is_dir():
                    continue
                got = predicates.differential_no_new_failures({**args, "workspace": str(ws)})
                self.assertIn(got["status"], {"pass", "na"}, f"{tid}: {got['actual']}")

    def test_structlog_baseline_is_recorded_as_flaky(self) -> None:
        args = self._args("A4-STRUCTLOG-710")
        stable, union = set(args["baseline_stable"]), set(args["baseline_union"])
        self.assertGreater(len(union), len(stable),
                           "structlog 的抖动是本次结论的前提，基线若被记成稳定说明台账被覆盖过")

    def test_deterministic_repos_have_union_equal_stable(self) -> None:
        for tid in ("A1-PACKAGING-577", "A2-LOGURU-1435", "A3-ISORT-1815"):
            args = self._args(tid)
            self.assertEqual(len(set(args["baseline_union"])), len(set(args["baseline_stable"])),
                             f"{tid} 被测出抖动，与实测不符")


if __name__ == "__main__":
    unittest.main(verbosity=2)
