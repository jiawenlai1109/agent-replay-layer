"""Known-answer tests for the statistics helper.

Every expected value here is either derivable by hand from a counting argument or
reproduced by a second, independently written enumeration. That is the point: the
number this module exists to replace (p=0.0013, printed by a throwaway script) was
wrong in a way no one could see without doing the arithmetic twice.
"""

from __future__ import annotations

import math
import unittest

import stats


def fisher_reference(a, b, c, d):
    """Second implementation: build the whole hypergeometric pmf, then tail it."""
    n, row1, k = a + b + c + d, a + b, a + c
    pmf = {x: math.comb(k, x) * math.comb(n - k, row1 - x) / math.comb(n, row1)
           for x in range(max(0, k - (n - row1)), min(k, row1) + 1)}
    assert abs(sum(pmf.values()) - 1.0) < 1e-12, "pmf must normalise"
    return sum(v for x, v in pmf.items() if x >= a)


class TestFisherExact(unittest.TestCase):
    def test_tiny_tables_are_hand_checkable(self) -> None:
        # one success in one of two slots: a coin flip
        self.assertAlmostEqual(stats.fisher_greater(1, 0, 0, 1), 0.5, places=12)
        # both successes in the first row of a 4-item split: 1 / C(4,2)
        self.assertAlmostEqual(stats.fisher_greater(2, 0, 0, 2), 1 / 6, places=12)
        # 1 / C(8,4)
        self.assertAlmostEqual(stats.fisher_greater(4, 0, 0, 4), 1 / 70, places=12)

    def test_matches_the_independent_enumeration(self) -> None:
        for t in [(13, 0, 9, 7), (10, 0, 6, 5), (6, 7, 2, 14), (0, 5, 5, 0), (3, 3, 3, 3)]:
            self.assertAlmostEqual(stats.fisher_greater(*t), fisher_reference(*t),
                                   places=12, msg=str(t))

    def test_the_corrected_dispatch_numbers(self) -> None:
        """The published 0.0013 was not reproducible under any of these tables."""
        self.assertAlmostEqual(stats.fisher_greater(13, 0, 9, 7), 0.0073, places=4)
        self.assertAlmostEqual(stats.fisher_greater(10, 0, 6, 5), 0.0227, places=4)
        self.assertGreater(stats.fisher_greater(10, 0, 6, 5), 0.0013 * 10)

    def test_no_enrichment_gives_a_large_p(self) -> None:
        self.assertGreater(stats.fisher_greater(6, 7, 6, 10), 0.4)
        self.assertEqual(stats.fisher_greater(0, 5, 5, 0), 1.0)

    def test_degenerate_inputs_do_not_crash(self) -> None:
        self.assertEqual(stats.fisher_greater(0, 0, 0, 0), 1.0)
        with self.assertRaises(ValueError):
            stats.fisher_greater(-1, 2, 3, 4)


class TestSignTest(unittest.TestCase):
    def test_four_discordant_pairs_all_one_way(self) -> None:
        # 1/16: the floor for n=4, which is why "4 wins, 0 losses" is not yet a result
        self.assertAlmostEqual(stats.sign_test(4, 0), 1 / 16, places=12)
        self.assertAlmostEqual(stats.sign_test(3, 1), 5 / 16, places=12)
        # 2-2 is not "no effect = p 1": the tail still carries P(3)+P(4) = 11/16
        self.assertAlmostEqual(stats.sign_test(2, 2), 11 / 16, places=12)
        self.assertEqual(stats.sign_test(0, 0), 1.0)


class TestDescribe(unittest.TestCase):
    def test_counts_only_the_named_arm(self) -> None:
        rows = [{"arm": "C-REC", "ok": True}, {"arm": "C-REC", "ok": False},
                {"arm": "C-ROLL", "ok": True}]
        self.assertEqual(stats.describe(rows, "C-REC", lambda r: r["ok"]), (1, 2))
        self.assertEqual(stats.describe(rows, "C-ROLL", lambda r: r["ok"]), (1, 1))
        self.assertEqual(stats.describe(rows, "C-B1", lambda r: r["ok"]), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
