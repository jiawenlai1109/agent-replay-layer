"""Exact tests small enough to check by hand, because an ad-hoc p-value in this
project already escaped once: "Fisher p ~ 0.0013" for the dispatch comparison was
printed from a script whose hypergeometric sum was wrong by ~17x (real value
0.0227 for that table). Two rules follow from that and are enforced here:

  * p-values come from this module, never from a throwaway snippet;
  * every function is pinned to values a person can verify combinatorially.
"""

from __future__ import annotations

import math
from typing import Any


def fisher_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact P(X >= a) for the table [[a, b], [c, d]].

    Row 1 is treated as the sample drawn from a population with K = a + c
    successes out of n = a + b + c + d, so the null distribution of the first
    cell is hypergeometric. "greater" asks whether row 1 is enriched in
    successes, which is the direction every claim in this project points.
    """
    if min(a, b, c, d) < 0:
        raise ValueError("counts must be non-negative")
    n = a + b + c + d
    row1 = a + b
    k = a + c
    if n == 0 or row1 == 0 or k == 0:
        return 1.0
    denom = math.comb(n, row1)
    lo, hi = max(0, k - (n - row1)), min(k, row1)
    p = 0.0
    for x in range(a, hi + 1):
        if x < lo:
            continue
        p += math.comb(k, x) * math.comb(n - k, row1 - x)
    return min(1.0, p / denom)


def sign_test(plus: int, minus: int) -> float:
    """One-sided exact binomial on discordant pairs: does treatment A win more?

    Ties carry no information here and are excluded by construction, so n is the
    number of discordant pairs. With n=4 discordant pairs all in one direction
    the smallest achievable p is 1/16 = 0.0625 — worth remembering before anyone
    reads a direction as a result.
    """
    n = plus + minus
    if n == 0:
        return 1.0
    p = sum(math.comb(n, i) for i in range(plus, n + 1)) / 2 ** n
    return min(1.0, p)


def describe(rows: list[dict[str, Any]], arm: str, pred) -> tuple[int, int]:
    """(successes, n) for one arm — kept here so both arms go through one path."""
    sel = [r for r in rows if r.get("arm") == arm]
    return sum(1 for r in sel if pred(r)), len(sel)
