"""Same-task cost comparison across arms, with the confounds printed next to it.

The trap this exists to avoid: comparing arm medians computed over *different task
sets* and calling the difference a saving. A cheaper run can just mean a shorter
run, so every line here prints what the run was actually given (`rounds_run`, the
abort reason) and what it achieved (the authoritative recomputed ledger verdict
from `g3_fp_audit.csv`, not the verdict the run wrote about itself).

`cost` and `tokens` are opencode's own fields on the run; cost is USD.

    python3 cost_compare.py            # needs results/g3_fp_audit.csv from fp_audit.py
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from typing import Any

import sbr_paths

OUT = sbr_paths.RESULTS / "cost_compare.csv"


def verdicts() -> dict[str, str]:
    f = sbr_paths.RESULTS / "g3_fp_audit.csv"
    if not f.is_file():
        raise SystemExit("run fp_audit.py first: verdicts come from the offline recompute")
    return {r["run_id"]: r["ledger_verdict"]
            for r in csv.DictReader(f.open(encoding="utf-8"))
            if r["source"] == "recompute_summary"}


def load(vd: dict[str, str]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for f in sorted(sbr_paths.MANIFESTS.glob("*.json")):
        m = json.loads(f.read_text(encoding="utf-8"))
        met = m.get("metrics") or {}
        steps = int(met.get("steps") or 0)
        if steps <= 0:
            continue                       # aborted manifests are not samples
        rep = m.get("report") or {}
        run_id = m.get("run_id") or f.stem
        groups[(m.get("task_id") or "", str(m.get("arm") or "?"))].append({
            "run_id": run_id,
            "cost": float(met.get("cost") or 0.0),
            "steps": steps,
            "tokens": int(met.get("tokens") or 0),
            "cache_read": int(met.get("cache_read") or 0),
            "rounds": int(met.get("rounds") or 0),
            "rounds_run": int(rep.get("rounds_run") or 0),
            "abort": rep.get("abort_reason") or rep.get("report_status") or "",
            "verdict": vd.get(run_id, "not_recomputed"),
            "rule": str(m.get("trigger_rule") or "v0_red_after_injection"),
            "model": str(m.get("model") or "?"),
        })
    return groups


def agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cost = sorted(r["cost"] for r in rows)
    steps = sorted(r["steps"] for r in rows)
    tok = sorted(r["tokens"] for r in rows)
    ok = sum(1 for r in rows if r["verdict"] == "satisfied")
    return {
        "n": len(rows),
        "cost_sum_usd": round(sum(cost), 6),
        "cost_median_usd": round(statistics.median(cost), 6),
        "cost_range_usd": f"{cost[0]:.6f}..{cost[-1]:.6f}" if len(cost) > 1 else "",
        "steps_median": statistics.median(steps),
        "tokens_median": int(statistics.median(tok)),
        "rounds_given_or_run": "/".join(str(r["rounds_run"] or r["rounds"]) for r in rows),
        "abort": "|".join(sorted({r["abort"] for r in rows})),
        "satisfied": ok,
        "verdicts": "|".join(sorted({r["verdict"] for r in rows})),
        # v0 (detection leaned on injected_at) and v1 (new red after green) are
        # different instruments; a row that mixes them says so.
        "rules": "|".join(sorted({r["rule"] for r in rows})),
        # Prices differ per model, so a row mixing models mixes cost scales.
        "models": "|".join(sorted({r["model"] for r in rows})),
        # cost per achieved success: failures are charged, that is the point
        "usd_per_success": round(sum(cost) / ok, 6) if ok else "",
        "tokens_per_success": int(sum(r["tokens"] for r in rows) / ok) if ok else "",
    }


def main() -> int:
    vd = verdicts()
    groups = load(vd)
    tasks = sorted({t for t, _ in groups})
    rows_out = []
    for t in tasks:
        arms = {a: g for (tt, a), g in groups.items() if tt == t}
        if len(arms) < 2:
            print(f"skip {t or '(blank task)'}: only {list(arms)} on disk, no cross-arm comparison")
            continue
        print(f"\n== {t} ==")
        # Baseline for the ratio: the from-scratch arm when this task has one,
        # otherwise the rewind-only arm. Stated per row so a reader cannot
        # mistake which denominator a ratio uses.
        base_arm = "A" if "A" in arms else ("C-ROLL" if "C-ROLL" in arms else None)
        base = agg(arms[base_arm]) if base_arm else None
        for arm in ("A", "B", "C-ROLL", "C-REC", "D"):
            if arm not in arms:
                continue
            a = agg(arms[arm])
            a["arm"] = arm
            a["task"] = t
            a["baseline_arm"] = base_arm or ""
            if base and a["cost_median_usd"] and base["cost_median_usd"]:
                a["cost_ratio_vs_baseline"] = round(a["cost_median_usd"] / base["cost_median_usd"], 3)
                a["token_ratio_vs_baseline"] = round(a["tokens_median"] / base["tokens_median"], 3) if base["tokens_median"] else ""
            rows_out.append(a)
            print("  %-7s %s" % (arm, json.dumps({k: v for k, v in a.items()
                                                  if k not in ("task",)}, ensure_ascii=True)))
    if rows_out:
        OUT.parent.mkdir(exist_ok=True)
        keys = ["task", "arm", "n", "cost_sum_usd", "cost_median_usd", "cost_range_usd",
                "steps_median", "tokens_median", "rounds_given_or_run", "abort", "satisfied",
                "verdicts", "usd_per_success", "tokens_per_success", "rules", "models",
                "baseline_arm", "cost_ratio_vs_baseline", "token_ratio_vs_baseline"]
        with OUT.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_out)
        print("\nwrote", OUT, "rows", len(rows_out))
    return 0


if __name__ == "__main__":
    main()
