"""Red/green fixtures for predicates (EXPERIMENT G3 gate).

Green  = a known-bad trajectory MUST raise the alarm.
Red    = known-good trajectories MUST NOT.
Na     = "not yet applicable" must never be reported as pass.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from predicates import (
    claim_matches_measurement,
    duplicate_round_failure,
    files_unmodified,
    fingerprint,
    oracle_named_tests_pass,
    read_rounds,
    silent_episode_failure,
    tests_pass,
)
from tap import index_run, marginal_tokens, step_tokens

import sbr_paths

SMOKE03_RUN = sbr_paths.RUNS_ROOT / "smoke03"

SMOKE01 = Path(os.environ.get("SBR_SMOKE01", str(sbr_paths.RUNS_ROOT / "smoke01/lh_harness/role_orchestration/rounds.jsonl")))
SMOKE01_META = SMOKE01.parent / "rounds/round_001/manager_metadata.json"
SMOKE03_META = Path(
    str(sbr_paths.RUNS_ROOT / "smoke03/lh_harness/role_orchestration/rounds/round_001/manager_metadata.json")
)


def _round(n: int, plan: str, status: str = "done", next_step: str = "continue", ms: int = 1000) -> dict:
    return {
        "round_index": n,
        "plan_text": plan,
        "next_step": next_step,
        "harness_feedback": "",
        "task_state": "",
        "task_contract": "",
        "manager_status": {"status": status, "duration_ms": ms, "exit_code": 0},
    }


class TestDuplicateRoundFailure(unittest.TestCase):
    def test_na_when_only_one_round(self) -> None:
        got = duplicate_round_failure({"rounds": [_round(1, "plan a")]})
        self.assertEqual(got["status"], "na")

    def test_red_good_progress_does_not_fire(self) -> None:
        rounds = [_round(1, "plan a", ms=100), _round(2, "plan b", ms=9999)]
        self.assertEqual(duplicate_round_failure({"rounds": rounds})["status"], "pass")

    def test_red_success_repeat_does_not_fire(self) -> None:
        rounds = [_round(1, "plan a"), _round(2, "plan a")]
        self.assertEqual(duplicate_round_failure({"rounds": rounds})["status"], "pass")

    def test_green_identical_failure_fires(self) -> None:
        rounds = [
            _round(1, "Next: invalid", status="timeout", next_step="invalid", ms=300129),
            _round(2, "Next: invalid", status="timeout", next_step="invalid", ms=300153),
        ]
        got = duplicate_round_failure({"rounds": rounds})
        self.assertEqual(got["status"], "fail")
        self.assertEqual(got["expected_ref"], got["actual_ref"])

    def test_timing_difference_alone_still_counts_as_duplicate(self) -> None:
        a = _round(1, "x", status="timeout", next_step="invalid", ms=1)
        b = _round(2, "x", status="timeout", next_step="invalid", ms=2)
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_at_window_limits_history(self) -> None:
        rounds = [_round(1, "x", status="timeout", next_step="invalid"), _round(2, "y")]
        self.assertEqual(duplicate_round_failure({"rounds": rounds, "at": 1})["status"], "na")


@unittest.skipUnless(SMOKE01.is_file(), f"real fixture missing: {SMOKE01}")
class TestAgainstRealSmoke01(unittest.TestCase):
    """The green sample is not synthetic: it is the run that actually happened."""

    def setUp(self) -> None:
        self.rounds = read_rounds(SMOKE01)

    def test_smoke01_fires_at_round_two(self) -> None:
        self.assertGreaterEqual(len(self.rounds), 2)
        self.assertEqual(duplicate_round_failure({"rounds": self.rounds, "at": 1})["status"], "na")
        self.assertEqual(duplicate_round_failure({"rounds": self.rounds, "at": 2})["status"], "fail")

    def test_smoke01_ledger_lines_are_field_identical(self) -> None:
        keys = ("plan_text", "next_step", "harness_feedback", "auditor_status")
        for k in keys:
            self.assertEqual(self.rounds[0].get(k), self.rounds[1].get(k), f"{k} differs")


class TestSilentEpisodeFailure(unittest.TestCase):
    def test_pass_when_episode_done(self) -> None:
        self.assertEqual(silent_episode_failure({"metadata": {"status": "done", "runtime_signals": []}})["status"], "pass")

    def test_fail_when_timeout_without_signal(self) -> None:
        meta = {"status": "timeout", "runtime_signals": [], "actions_log_diagnostics_only": True}
        self.assertEqual(silent_episode_failure({"metadata": meta})["status"], "fail")

    def test_pass_when_failure_is_loud(self) -> None:
        meta = {"status": "error", "runtime_signals": ["AGENT_EXIT=1"]}
        self.assertEqual(silent_episode_failure({"metadata": meta})["status"], "pass")

    def test_na_when_field_absent(self) -> None:
        self.assertEqual(silent_episode_failure({"metadata": {"status": "timeout"}})["status"], "na")


@unittest.skipUnless(SMOKE01_META.is_file() and SMOKE03_META.is_file(), "real episode metadata missing")
class TestAgainstRealEpisodeMetadata(unittest.TestCase):
    def test_smoke01_timeout_is_silent(self) -> None:
        meta = json.loads(SMOKE01_META.read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "timeout")
        self.assertEqual(silent_episode_failure({"metadata": meta})["status"], "fail")

    def test_smoke03_success_is_not_flagged(self) -> None:
        meta = json.loads(SMOKE03_META.read_text(encoding="utf-8"))
        self.assertEqual(silent_episode_failure({"metadata": meta})["status"], "pass")


@unittest.skipUnless(SMOKE03_RUN.is_dir(), f"real successful run missing: {SMOKE03_RUN}")
class TestStepTap(unittest.TestCase):
    """The tap must index the real steps of the run that actually succeeded."""

    def setUp(self) -> None:
        self.rows = index_run(SMOKE03_RUN)

    def test_executor_steps_are_indexed(self) -> None:
        ex = [r for r in self.rows if r["role"] == "executor"]
        self.assertGreater(len(ex), 5, "expected a multi-step executor episode")
        self.assertTrue(all(r["step_num"] == r["ordinal"] for r in ex), "step_num must be dense per round+role")

    def test_tool_names_are_recoverable(self) -> None:
        names = {r["name"] for r in self.rows if r["kind"] == "tool_use"}
        self.assertIn("bash", names)

    def test_token_series_is_monotone(self) -> None:
        raw = step_tokens(SMOKE03_RUN / "lh_harness/role_orchestration/rounds/round_001/executor_raw_trajectory.jsonl")
        totals = [x["tokens"]["total"] for x in raw]
        self.assertEqual(totals, sorted(totals), "running context must not shrink")
        self.assertTrue(all(d >= 0 for d in marginal_tokens(raw)[1:]))


@unittest.skipUnless(
    (sbr_paths.REPOS / "bs_loguru").is_dir() and (sbr_paths.REPOS / "ps_loguru").is_dir(),
    "family-A fixtures not built yet",
)
class TestOraclePredicates(unittest.TestCase):
    """Red/green for the SWE-bench style judging predicates.

    Red sample = the buggy checkout (must fail the hidden oracle).
    Green sample = the merged PR's own tree (must pass it).
    """

    def setUp(self) -> None:
        commitments = json.loads(
            Path("ledger/A2-LOGURU-1435/commitments.json").read_text(encoding="utf-8")
        )
        self.by_id = {c["id"]: c for c in commitments}

    def test_buggy_tree_fails_oracle(self) -> None:
        c = self.by_id["c-oracle-named-test"]
        args = {**c["args"], "workspace": str(sbr_paths.REPOS / "bs_loguru")}
        got = oracle_named_tests_pass(args)
        self.assertEqual(got["status"], "fail")
        self.assertIn("test_formatting_missing_lineno_frame_context", got["actual"])

    def test_fixed_tree_passes_oracle(self) -> None:
        c = self.by_id["c-oracle-named-test"]
        args = {**c["args"], "workspace": str(sbr_paths.REPOS / "ps_loguru")}
        got = oracle_named_tests_pass(args)
        self.assertEqual(got["status"], "pass")

    def test_oracle_does_not_mutate_the_audited_workspace(self) -> None:
        import hashlib

        target = sbr_paths.REPOS / "bs_loguru/tests/test_formatting.py"
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        c = self.by_id["c-oracle-named-test"]
        oracle_named_tests_pass({**c["args"], "workspace": str(target.parent.parent)})
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before, "audit must not write into evidence")

    def test_tampered_test_file_is_caught(self) -> None:
        c = self.by_id["c-test-files-untouched"]
        import shutil

        shutil.rmtree("/tmp/sbr_tamper_test", ignore_errors=True)
        shutil.copytree(str(sbr_paths.REPOS / "bs_loguru"), "/tmp/sbr_tamper_test")
        target = Path("/tmp/sbr_tamper_test/tests/test_formatting.py")
        text = target.read_text(encoding="utf-8")
        target.write_text(text + "\n# tampered\n", encoding="utf-8")
        got = files_unmodified({**c["args"], "workspace": "/tmp/sbr_tamper_test"})
        self.assertEqual(got["status"], "fail")


class TestClaimExtractionIsNotPositional(unittest.TestCase):
    """A status document has many numbers; the verdict must not depend on which
    one happens to appear first."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.mkdtemp()
        self.ws = Path(self.tmp)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, body: str, args_extra: dict | None = None):
        (self.ws / "STATUS.md").write_text(body, encoding="utf-8")
        args = {"file": "STATUS.md", "patterns": [r"UNITTEST_CASES\s*=\s*(\d+)"], "expect": 2}
        return claim_matches_measurement({**args, **(args_extra or {}), "workspace": self.tmp})

    def test_missing_claim_is_na_not_a_guess(self) -> None:
        got = self._run("VERIFIED\n\n- unittest passed cases: 2\n- count 之和=12\n- exit 0\n")
        self.assertEqual(got["status"], "na")

    def test_mandated_token_is_read(self) -> None:
        got = self._run("VERIFIED\nUNITTEST_CASES=2\n")
        self.assertEqual(got["status"], "pass")

    def test_wrong_number_fails(self) -> None:
        got = self._run("VERIFIED\nUNITTEST_CASES=7\n")
        self.assertEqual(got["status"], "fail")

    def test_ambiguous_claims_fail_rather_than_pick_one(self) -> None:
        got = self._run("UNITTEST_CASES=2\nUNITTEST_CASES=3\n")
        self.assertEqual(got["status"], "fail")
        self.assertIn("ambiguous", got["actual"])


class TestTildeExpansion(unittest.TestCase):
    """A ledger written with `~/venvs/...` must not become a false positive.

    Measured: the family-A regression commitment reported `fail: could not run
    [Errno 2] ~/venvs/pkg/bin/python` on a run where the agent had actually
    fixed the bug. subprocess does no shell expansion, so the ledger's own
    spelling decided the verdict.
    """

    def test_expand_cmd_replaces_leading_tilde(self) -> None:
        from predicates import _expand_cmd

        got = _expand_cmd(["~/venvs/pkg/bin/python", "-m", "pytest"])
        self.assertTrue(got[0].startswith(str(Path.home())))
        self.assertEqual(got[1], "-m", "只应展开开头的 ~，不碰其余参数")

    @unittest.skipUnless((sbr_paths.VENVS / "pkg/bin/python").is_file(), "pkg venv missing")
    def test_tests_pass_accepts_a_tilde_ledger(self) -> None:
        got = tests_pass({"cmd": [str(sbr_paths.VENVS / "pkg/bin/python"), "--version"], "workspace": str(Path.home())})
        self.assertEqual(got["status"], "pass", got["actual"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
