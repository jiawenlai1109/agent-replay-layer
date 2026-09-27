"""Red/black fixtures for the three-valued ledger verdict.

`satisfied_by_ledger` used to fold "not evaluable" into "not satisfied", which
made a run with 8 passes and 1 unevaluable promise read as a failure in the
headline column. These tests pin the new spelling.
"""

from __future__ import annotations

import unittest

import audit


def latched(**pairs):
    return {cid: {"status": st} for cid, st in pairs.items()}


class TestVerdictOf(unittest.TestCase):
    def test_all_pass_is_satisfied(self) -> None:
        self.assertEqual(audit.verdict_of(latched(a="pass", b="pass")), "satisfied")

    def test_any_fail_is_violated_even_with_na_present(self) -> None:
        self.assertEqual(audit.verdict_of(latched(a="pass", b="fail", c="na")), "violated")

    def test_pass_plus_na_is_incomplete_evidence_not_a_failure(self) -> None:
        v = audit.verdict_of(latched(a="pass", b="pass", c="na"))
        self.assertEqual(v, "incomplete_evidence")
        self.assertNotEqual(v, "violated")

    def test_na_alone_is_not_satisfied(self) -> None:
        self.assertEqual(audit.verdict_of(latched(a="na")), "incomplete_evidence")

    def test_empty_ledger_says_so_instead_of_looking_clean(self) -> None:
        self.assertEqual(audit.verdict_of({}), "no_commitments")


if __name__ == "__main__":
    unittest.main(verbosity=2)
