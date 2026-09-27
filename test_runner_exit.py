"""The abort path must be tested, because an untested guard crashed with
NameError and the resulting rc=1 was indistinguishable from a normal failure —
so the batch ran six more dead tasks after detecting the credential was dead.
"""

from __future__ import annotations

import glob
import json
import pathlib
import unittest

import runner
import sbr_paths


class TestExitCodes(unittest.TestCase):
    def test_clean_run_is_zero(self) -> None:
        m = {"report": {"abort_reason": ""}, "metrics": {"steps": 40}}
        self.assertEqual(runner.exit_code_for(m), runner.EXIT_OK)

    def test_provider_error_is_three(self) -> None:
        m = {"report": {"abort_reason": "provider_provider_error"}, "metrics": {"steps": 0}}
        self.assertEqual(runner.exit_code_for(m), runner.EXIT_PROVIDER)

    def test_insufficient_balance_text_is_three(self) -> None:
        m = {"report": {"failure_reason": "Insufficient Balance (request_id: x)"}, "metrics": {"steps": 5}}
        self.assertEqual(runner.exit_code_for(m), runner.EXIT_PROVIDER)

    def test_empty_run_is_four(self) -> None:
        m = {"report": {"abort_reason": ""}, "metrics": {"steps": 0}}
        self.assertEqual(runner.exit_code_for(m), runner.EXIT_NO_STEPS)

    def test_timeout_is_five_and_beats_no_steps(self) -> None:
        m = {"report": {}, "metrics": {"steps": 0}, "timed_out": True}
        self.assertEqual(runner.exit_code_for(m), runner.EXIT_TIMEOUT)


class TestAnnounceDoesNotCrash(unittest.TestCase):
    def test_announce_on_every_nonzero_code(self) -> None:
        for code in (runner.EXIT_PROVIDER, runner.EXIT_NO_STEPS, runner.EXIT_TIMEOUT):
            runner.announce({"report": {"abort_reason": "provider_provider_error"}, "metrics": {"steps": 0}}, code)


@unittest.skipUnless(glob.glob(str(sbr_paths.MANIFESTS / "*-19*.json")), "no incident manifests")
class TestAgainstTodayIncidents(unittest.TestCase):
    def test_the_dead_credential_runs_are_flagged(self) -> None:
        flagged = 0
        for p in glob.glob(str(sbr_paths.MANIFESTS / "*.json")):
            d = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
            if "report" not in d:
                continue
            if runner.is_provider_fatal(d["report"]):
                flagged += 1
                self.assertEqual(runner.exit_code_for(d), runner.EXIT_PROVIDER, d.get("run_id"))
        self.assertGreater(flagged, 0, "今天的 402 样本没被识别出来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
