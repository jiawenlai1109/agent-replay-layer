"""G3 re-audit: how often does a sensor say `fail` when nothing is violated?

"False positive" needs a truth that is not the sensor, so the sample set splits
in two and the two get different metrics:

  U1  injected runs (C arms). The recipe declares the exact violation set
      (`root_cause` plus its `cascade`), so a red on a commitment outside that
      set is a *candidate* false positive. Each candidate is then checked against
      the artifact it names, which this script prints verbatim so the check is
      auditable rather than asserted.
  U2  runs with nothing injected (A/B arms, the pre-injection window of every C
      run, and C runs whose injection gate deferred). No independent truth exists
      here: the same ledger that raises the red also decides the run's terminal
      verdict, so self-consistency proves nothing. U2 is therefore reported as a
      list of reds for manual attribution plus a stability count, never as a
      rate.

Reds that a predicate cannot reproduce on the same state are device faults, not
agent faults (this is how the structlog flakiness was caught), so every red from a
command-bound predicate is re-run `--repeat` times and any disagreement recorded.
Pure-filesystem predicates are re-run zero times: their input does not change.

Offline only: reads durable artifacts, never starts a run, never calls a provider.

    python3 fp_audit.py [--arm C|AB|all] [--repeat 3] [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

import audit
import predicates
import sbr_paths
import stats
from taskset import load_commitments

# Predicates that shell out. Repeating them costs real seconds, so the stability
# pass runs them twice while the pure-filesystem ones run the full `--repeat`.
CMD_BOUND = {"tests_pass", "oracle_named_tests_pass", "differential_no_new_failures",
             "claim_matches_measurement"}

OUT = sbr_paths.RESULTS / "g3_fp_audit.csv"


def load_manifests() -> list[dict[str, Any]]:
    rows = []
    for f in sorted(sbr_paths.MANIFESTS.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        arm = d.get("arm") or ("C" if "-C-" in (d.get("run_id") or f.stem) else "?")
        inj = d.get("injection") or {}
        truth = set()
        if inj.get("root_cause"):
            truth.add(inj["root_cause"])
        truth.update(inj.get("cascade") or [])
        rows.append({
            "file": f.name,
            "run_id": d.get("run_id") or f.stem,
            "task_id": d.get("task_id") or d.get("task"),
            "arm": arm,
            "workspace": d.get("workspace"),
            "recipe": d.get("recipe"),
            "injected_at": d.get("injected_at_provider_step"),
            "truth": truth,
            "deferred": bool(d.get("injection_deferred")),
            "aborted": d.get("aborted"),
            "green_before_inject": d.get("green_before_inject"),
            "readings": d.get("readings") or [],
            "sensor": d.get("sensor"),
        })
    return rows


def reds_from_readings(m: dict[str, Any]) -> list[dict[str, Any]]:
    """What the online sensor saw, step by step, from the manifest itself."""
    out = []
    inject_at = m["injected_at"]
    for r in m["readings"]:
        step = r.get("step")
        for cid, v in r.items():
            if cid in ("step", "cost_usd", "t", "unclosed") or not isinstance(v, dict):
                continue
            if v.get("status") != "fail":
                continue
            window = "pre" if inject_at is None or step is None or step < inject_at else "post"
            out.append({
                "source": "reading",
                "step": step,
                "window": window,
                "commitment_id": cid,
                "predicate": None,
                "expected": v.get("expected"),
                "actual": v.get("actual"),
                "in_truth": (cid in m["truth"]) if m["truth"] else None,
            })
    return out


def recompute(m: dict[str, Any], repeat: int) -> dict[str, Any]:
    """Re-run the current ledger against the run's own durable artifacts."""
    res = {"alarms": [], "error": None}
    if not m["task_id"] or not m["workspace"]:
        res["error"] = "no_task_or_workspace"
        return res
    ws = Path(m["workspace"])
    if not ws.is_dir():
        res["error"] = "workspace_gone"
        return res
    try:
        res["alarms"] = audit.audit_run(m["task_id"], sbr_paths.RUNS_ROOT / m["run_id"], ws)
    except Exception as exc:  # a broken audit is not a green audit
        res["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return res
    ledger = {c["id"]: c for c in load_commitments(m["task_id"])}
    stab = {}
    for a in res["alarms"]:
        if a["status"] != "fail":
            continue
        # Only a command-bound predicate can disagree with itself: the pure
        # filesystem ones read a state that does not change between repeats, so
        # re-running them proves nothing and only burns the machine's 2 cores.
        if a["predicate"] not in CMD_BOUND:
            stab[f'{a["commitment_id"]}@{a["step"]}'] = {"n": 0, "statuses": {"deterministic": 1}}
            continue
        c = ledger.get(a["commitment_id"])
        if not c or c["applies_to"] != "workspace":
            stab[f'{a["commitment_id"]}@{a["step"]}'] = {"n": 0, "statuses": {"not_repeated": 1}}
            continue
        args = {**c.get("args", {}), "workspace": str(ws)}
        seen = Counter()
        for _ in range(max(1, repeat)):
            try:
                seen[predicates.REGISTRY[a["predicate"]](args)["status"]] += 1
            except Exception as exc:
                seen[f"error:{type(exc).__name__}"] += 1
        stab[f'{a["commitment_id"]}@{a["step"]}'] = {"n": repeat, "statuses": dict(seen)}
    res["stability"] = stab
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="all", choices=("C", "AB", "all"))
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="only the first N manifests (smoke)")
    args = ap.parse_args()

    rows = load_manifests()
    if args.arm == "C":
        rows = [r for r in rows if str(r["arm"]).startswith("C-")]
    elif args.arm == "AB":
        rows = [r for r in rows if r["arm"] in ("A", "B")]
    # An aborted manifest is not a sample: 8 of them are runs where the agent CLI
    # never started (`opencode: not found`, 18 ms, exit 127). Their alarms are
    # correct readings of a dead episode and counting them as sensor output
    # inflates both numerator and denominator -- the same folding as defect #16.
    dead = [r for r in rows if r["aborted"]]
    rows = [r for r in rows if not r["aborted"]]
    if args.limit:
        rows = rows[: args.limit]

    out_rows: list[dict[str, Any]] = []
    tally = Counter()
    candidates: list[dict[str, Any]] = []
    for m in rows:
        universe = "U1" if (m["truth"] and m["injected_at"] is not None) else "U2"
        tally[f'{universe}:{m["arm"]}'] += 1
        if m["aborted"]:
            tally[f'aborted:{m["aborted"]}'] += 1

        for red in reds_from_readings(m):
            if red["window"] == "post" and red["in_truth"] is False:
                kind = "candidate_fp"
            elif red["window"] == "post" and red["in_truth"] is True:
                kind = "true_positive"
            else:
                kind = "uninjected_red"
            tally[f"reading:{kind}"] += 1
            out_rows.append({"run_id": m["run_id"], "arm": m["arm"], "universe": universe,
                             "truth_set": "|".join(sorted(m["truth"])), **red, "kind": kind,
                             "n_repeat": "", "statuses": ""})
            if kind == "candidate_fp":
                candidates.append({**red, "run_id": m["run_id"], "task_id": m["task_id"],
                                   "workspace": m["workspace"]})

        rec = recompute(m, args.repeat)
        if rec["error"]:
            tally[f"recompute_error:{rec['error'][:40]}"] += 1
            continue
        tally["recomputed"] += 1
        checks = Counter(a["status"] for a in rec["alarms"])
        latched = audit.latched(rec["alarms"])
        out_rows.append({"run_id": m["run_id"], "arm": m["arm"], "universe": universe,
                         "truth_set": "|".join(sorted(m["truth"])), "source": "recompute_summary",
                         "step": "", "window": "", "commitment_id": "", "predicate": "",
                         "expected": "", "actual": "", "in_truth": "", "kind": "summary",
                         "n_repeat": "", "statuses": json.dumps(dict(checks), ensure_ascii=True),
                         "ledger_verdict": audit.verdict_of(latched),
                         "checks_total": sum(checks.values())})
        for key, s in (rec["stability"] or {}).items():
            cid, _, step = key.partition("@")
            alarm = next((a for a in rec["alarms"] if a["commitment_id"] == cid and str(a["step"]) == step), {})
            # stable only if every repeat says fail; n=0 means "not re-run by design"
            unstable = s["n"] > 0 and s["statuses"].get("fail", 0) != s["n"]
            tally["recompute_red"] += 1
            tally["red_not_repeated" if s["n"] == 0 else ("recompute_red_unstable" if unstable else "recompute_red_stable")] += 1
            out_rows.append({"run_id": m["run_id"], "arm": m["arm"], "universe": universe,
                             "truth_set": "|".join(sorted(m["truth"])), "source": "recompute",
                             "step": step, "window": "", "commitment_id": cid,
                             "predicate": alarm.get("predicate"), "expected": alarm.get("expected"),
                             "actual": alarm.get("actual"), "in_truth": None,
                             "kind": "unstable" if unstable else "stable_red",
                             "n_repeat": s["n"], "statuses": json.dumps(s["statuses"], ensure_ascii=True)})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    keys = ["run_id", "arm", "universe", "source", "step", "window", "commitment_id", "predicate",
            "kind", "in_truth", "expected", "actual", "n_repeat", "statuses", "ledger_verdict",
            "checks_total", "truth_set"]
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)

    u1 = [m for m in rows if m["truth"] and m["injected_at"] is not None]
    post = [x for x in out_rows if x["window"] == "post"]
    outside = [x for x in post if x["kind"] == "candidate_fp"]
    u1_runs_fp = {x["run_id"] for x in outside}
    with_readings = {m["run_id"] for m in rows if m["readings"]}
    pre_runs = {x["run_id"] for x in out_rows if x["kind"] == "uninjected_red"}

    print(f"manifests in scope: {len(rows)}   excluded aborted: {len(dead)}")
    for k, v in sorted(tally.items()):
        print(f"  {k:<46} {v}")
    print("\n== 率（只在有独立真值的地方给率）==")
    print(f"  注入后窗口: {len(u1)} 条注入 run，{len(post)} 条红灯，其中落在配方真值集合之外 {len(outside)} 条")
    if post:
        rate = len(outside) / len(post)
        print(f"    观测假阳性率(按红灯) = {rate:.4f}"
              + (f" ；0 例时的 95% 上界 = {stats.zero_success_upper_bound(len(post)):.4f}" if not outside else ""))
    if u1:
        print(f"    出现集合外红灯的 run = {len(u1_runs_fp)}/{len(u1)}"
              + (f" ；0 例时的 95% 上界 = {stats.zero_success_upper_bound(len(u1)):.4f}" if not u1_runs_fp else ""))
    print("== 阈值（没有独立真值，只有'这条红灯能不能当漂移'）==")
    if with_readings:
        print(f"  注入前就已有红灯的 run = {len(pre_runs)}/{len(with_readings)}"
              f" = {len(pre_runs) / len(with_readings):.1%} ；'任意红灯即掐'会在这 {len(pre_runs)} 条上误掐")
        print(f"  注入前红灯事件总数 = {sum(1 for x in out_rows if x['kind'] == 'uninjected_red')}"
              "（逐条读数，非锁存：见 CSV 的 window=pre 行）")
    print(f"\ncandidate false positives (red outside the recipe truth set): {len(outside)}")
    for c in candidates[:20]:
        print("  %-46s step=%-4s %-24s expected=%s actual=%s" % (
            c["run_id"][:46], c["step"], c["commitment_id"],
            str(c["expected"])[:40], str(c["actual"])[:60]))
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
