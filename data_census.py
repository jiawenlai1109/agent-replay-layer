"""One-pass census of every number this project has measured, from artifacts only.

Nothing here starts a run or calls a provider: the whole file is derived from
`manifests/*.json`, the run directories, and the four result CSVs. It prints
`key = value` lines on purpose, so a report can quote a number and point at the
line that produced it instead of at a terminal scrollback.

Definitions that change the number, and are therefore stated in the key:
  valid     a manifest with at least one provider step (an aborted manifest is
            not a sample; scoring one folds an instrument fault into the
            subject's error rate -- defect #33)
  cost      the opencode `cost` field, which is USD, not CNY
  step      a provider `step_finish` event, the authoritative clock
  wall      first-to-last provider event timestamp inside the run directory, a
            proxy: it excludes startup before the first event and the halt after
            the last one

    python3 data_census.py
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import sbr_paths
import stats

MAN = sbr_paths.MANIFESTS
RES = sbr_paths.RESULTS


def num(key: str, value) -> None:
    print(f"{key} = {value}")


def load() -> list[dict]:
    rows = []
    for f in sorted(MAN.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        d["_file"] = f.name
        d.setdefault("run_id", f.stem)
        d.setdefault("task_id", d.get("task") or "")
        rows.append(d)
    return rows


def cost_of(m: dict) -> float:
    return float((m.get("metrics") or {}).get("cost") or 0.0)


def steps_of(m: dict) -> int:
    return int((m.get("metrics") or {}).get("steps") or 0)


def rounds_of(m: dict) -> int:
    return int((m.get("metrics") or {}).get("rounds") or 0)


def tokens_of(m: dict) -> int:
    return int((m.get("metrics") or {}).get("tokens") or 0)


def wall_seconds(run_id: str) -> float | None:
    d = sbr_paths.RUNS_ROOT / run_id
    if not d.is_dir():
        return None
    stamps = []
    for f in list(d.glob("lh_harness/role_orchestration/rounds/round_*/*_raw_trajectory.jsonl")) + \
             list(d.glob("lh_harness/*_episodes/*/provider_stream.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts = ev.get("ts") or ev.get("timestamp")
            if isinstance(ts, (int, float)) and ts > 1e12:
                stamps.append(ts / 1000.0)
            elif isinstance(ts, str):
                try:
                    stamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    pass
    if len(stamps) < 2:
        return None
    return max(stamps) - min(stamps)


def main() -> int:
    ms = load()
    valid = [m for m in ms if steps_of(m) > 0]
    aborted = [m for m in ms if steps_of(m) == 0]

    num("manifests_total", len(ms))
    num("manifests_valid", len(valid))
    num("manifests_aborted", len(aborted))
    num("manifests_abort_reasons", dict(Counter(str(m.get("aborted") or m.get("provider_fatal") or "no_flag") for m in aborted)))

    by_arm_valid: dict[str, list[dict]] = defaultdict(list)
    for m in valid:
        by_arm_valid[str(m.get("arm") or "?")].append(m)
    for arm, rows in sorted(by_arm_valid.items()):
        c = [cost_of(x) for x in rows]
        s = [steps_of(x) for x in rows]
        r = [rounds_of(x) for x in rows]
        t = [tokens_of(x) for x in rows]
        num(f"n[{arm}]", len(rows))
        num(f"cost_usd_sum[{arm}]", round(sum(c), 6))
        num(f"cost_usd_median[{arm}]", round(statistics.median(c), 6))
        num(f"cost_usd_minmax[{arm}]", f"{min(c):.6f}..{max(c):.6f}")
        num(f"provider_steps_sum[{arm}]", sum(s))
        num(f"provider_steps_median[{arm}]", statistics.median(s))
        num(f"provider_steps_minmax[{arm}]", f"{min(s)}..{max(s)}")
        num(f"rounds_median[{arm}]", statistics.median(r))
        num(f"tokens_sum[{arm}]", sum(t))
        num(f"tokens_median[{arm}]", int(statistics.median(t)))

    num("cost_usd_total_valid", round(sum(cost_of(m) for m in valid), 6))
    num("cost_cny_total_valid_at_7.1", round(sum(cost_of(m) for m in valid) * 7.1, 4))
    num("provider_steps_total_valid", sum(steps_of(m) for m in valid))
    num("distinct_tasks_with_valid_samples", len({m["task_id"] for m in valid}))
    num("distinct_recipes_injected", len({m.get("recipe") for m in valid if m.get("recipe")}))

    walls = [(m["run_id"], wall_seconds(m["run_id"])) for m in valid]
    ok = [(rid, w) for rid, w in walls if w]
    num(f"wall_proxy_runs_measured", len(ok))
    if ok:
        ws = sorted(w for _, w in ok)
        num("wall_proxy_median_s", round(statistics.median(ws), 1))
        num("wall_proxy_minmax_s", f"{ws[0]:.1f}..{ws[-1]:.1f}")
        num("wall_proxy_sum_min", round(sum(ws) / 60, 1))

    # --- paired C-arm report ---
    # The filter below is the one pair_report.py prints its headline with: n counts
    # only runs that actually resumed (`resume_epoch` present). It is written out
    # here rather than imported so the two paths have to agree; if they ever
    # disagree, the printed table is the one to trust and this census is wrong.
    pr = RES / "pair_report.csv"
    if pr.is_file():
        rows = list(csv.DictReader(pr.open(encoding="utf-8")))
        num("pair_report_rows", len(rows))
        num("pair_report_arms", dict(Counter(r["arm"] for r in rows)))
        resumed = [r for r in rows if r["resume_epoch"]]
        num("pair_report_resumed_n", len(resumed))
        head = {}
        for arm in ("C-REC", "C-ROLL"):
            sel = [r for r in resumed if r["arm"] == arm]
            dispatch = sum(1 for r in sel if int(float(r["post_resume_executor_steps"] or 0)) > 0)
            satisfied = sum(1 for r in sel if r["end_verdict"] == "satisfied")
            head[arm] = (dispatch, len(sel), satisfied)
            num(f"pair_dispatch[{arm}]", f"{dispatch}/{len(sel)}")
            num(f"pair_satisfied[{arm}]", f"{satisfied}/{len(sel)}")
        (a, n_a, sa), (c, n_c, sc) = head["C-REC"], head["C-ROLL"]
        num("pair_dispatch_fisher_p", round(stats.fisher_greater(a, n_a - a, c, n_c - c), 6))
        num("pair_satisfied_fisher_p", round(stats.fisher_greater(sa, n_a - sa, sc, n_c - sc), 6))
        att = [int(float(r["lag"])) for r in rows if r["attributable"] == "True" and r["lag"]]
        num("lag_attributable_n", len(att))
        if att:
            num("lag_median", statistics.median(att))
            num("lag_minmax", f"{min(att)}..{max(att)}")
        num("lag_not_attributable_n", sum(1 for r in rows if r["attributable"] != "True"))
        pairs = defaultdict(dict)
        for r in rows:
            if r["arm"] in ("C-REC", "C-ROLL"):
                pairs[(r["task"], r["rep"])][r["arm"]] = r
        complete = {k: v for k, v in pairs.items() if len(v) == 2}
        num("pair_complete_pairs", len(complete))
        won_roll = sum(1 for v in complete.values()
                       if int(float(v["C-ROLL"]["post_resume_executor_steps"] or 0)) == 0
                       and int(float(v["C-REC"]["post_resume_executor_steps"] or 0)) > 0)
        won_rec = sum(1 for v in complete.values()
                      if int(float(v["C-REC"]["post_resume_executor_steps"] or 0)) == 0
                      and int(float(v["C-ROLL"]["post_resume_executor_steps"] or 0)) > 0)
        num("pair_directive_only_wins", won_roll)
        num("pair_reverse_wins", won_rec)
        num("pair_sign_test_p", round(stats.sign_test(won_roll, won_rec), 6))
        num("pair_hit_root_cause", f"{sum(1 for r in rows if r['hit_root_cause'] == 'True')}/{len(rows)}")
        num("pair_rollback_ok", f"{sum(1 for r in rows if r['rollback_ok'] == 'True')}/{len(rows)}")
        num("pair_cost_usd_sum", round(sum(float(r["cost_usd"] or 0) for r in rows), 6))

    # --- cost attribution ---
    cc = RES / "cost_to_success.csv"
    if cc.is_file():
        rows = list(csv.DictReader(cc.open(encoding="utf-8")))
        num("cost_to_success_rows", len(rows))
        num("cost_to_success_arms", dict(Counter(r["arm"] for r in rows)))
        live = [r for r in rows if r["after_detect_source"] == "live"]
        num("cost_attribution_live_n", len(live))
        if live:
            num("cost_attribution_live_steps_after_detection",
                dict(Counter(r["steps_after_detection_live"] for r in live)))
            num("cost_attribution_live_cost_after_detection_usd",
                dict(Counter(r["cost_after_detection_live_usd"] for r in live)))
            num("cost_attribution_live_mid_flight_cut",
                f"{sum(1 for r in live if r['mid_flight_step_cut'] == 'True')}/{len(live)}")
        pub_ad = [r for r in rows if r["after_detect_publishable"] == "yes"]
        num("cost_attribution_offline_publishable_n", len(pub_ad))
        shares = sorted(float(r["share_after_detection"]) * 100 for r in pub_ad)
        if shares:
            num("after_detect_share_median_pct", round(statistics.median(shares), 2))
            num("after_detect_share_minmax_pct", f"{shares[0]:.2f}..{shares[-1]:.2f}")
            num("after_detect_share_n", len(shares))
        rec_pub = [r for r in rows if r["recovery_publishable"] == "yes"]
        num("recovery_publishable_n", len(rec_pub))
        rs = sorted(float(r["share_recovery"]) * 100 for r in rec_pub)
        if rs:
            num("recovery_share_median_pct", round(statistics.median(rs), 2))
            num("recovery_share_minmax_pct", f"{rs[0]:.2f}..{rs[-1]:.2f}")
        rcost = sorted(float(r["cost_recovery_usd"]) for r in rec_pub if r.get("cost_recovery_usd"))
        if rcost:
            num("recovery_cost_usd_median", round(statistics.median(rcost), 6))
            num("recovery_cost_usd_minmax", f"{rcost[0]:.6f}..{rcost[-1]:.6f}")
            num("recovery_cost_n", len(rcost))
        suspect = [r["share_suspect"] for r in rows if r.get("share_suspect")]
        num("cost_rows_flagged_suspect", len(suspect))
        num("cost_suspect_kinds", dict(Counter(s.split("_events")[0] if "attempt1" in s else s for s in suspect)))
        deficits = sorted(float(r["denom_extra_pct"]) for r in rows if r.get("denom_extra_pct"))
        if deficits:
            num("missing_events_median_steps", round(statistics.median(deficits), 2))

    # --- G3 false-positive audit ---
    g3 = RES / "g3_fp_audit.csv"
    if g3.is_file():
        rows = list(csv.DictReader(g3.open(encoding="utf-8")))
        num("g3_rows", len(rows))
        kinds = Counter(r["kind"] for r in rows)
        num("g3_kinds", dict(kinds))
        post = [r for r in rows if r["window"] == "post"]
        outside = [r for r in post if r["kind"] == "candidate_fp"]
        u1_runs = {r["run_id"] for r in rows if r["universe"] == "U1"}
        num("g3_post_reds", len(post))
        num("g3_post_reds_outside_truth", len(outside))
        num("g3_u1_runs", len(u1_runs))
        num("g3_observed_fp_rate_events", round(len(outside) / len(post), 6) if post else "no post reds")
        if post and not outside:
            num("g3_fp_upper_95_events", round(stats.zero_success_upper_bound(len(post)), 6))
        if u1_runs and not outside:
            num("g3_fp_upper_95_runs", round(stats.zero_success_upper_bound(len(u1_runs)), 6))
        pre = [r for r in rows if r["kind"] == "uninjected_red"]
        num("g3_pre_injection_reds", len(pre))
        runs_with_readings = {r["run_id"] for r in rows if r["source"] == "reading"}
        num("g3_runs_with_readings", len(runs_with_readings))
        num("g3_runs_red_before_injection", len({r["run_id"] for r in pre}))
        num("g3_pre_by_commitment", dict(Counter(r["commitment_id"] for r in pre)))
        rec = [r for r in rows if r["source"] == "recompute"]
        num("g3_recompute_reds_valid_runs", len(rec))
        num("g3_recompute_red_by_predicate", dict(Counter(r["predicate"] for r in rec)))

    # --- ledger and task inventory (code, not measurement) ---
    import predicates
    num("predicates_registered", len(predicates.REGISTRY))
    led = list(sbr_paths.LEDGER.glob("*/commitments.json")) if sbr_paths.LEDGER.is_dir() else []
    promises = 0
    pred_used = Counter()
    for f in led:
        for c in json.loads(f.read_text(encoding="utf-8")):
            promises += 1
            pred_used[c["predicate"]] += 1
    num("ledger_files", len(led))
    num("commitments_total", promises)
    per_task = sorted(sum(1 for _ in json.loads(f.read_text(encoding="utf-8"))) for f in led)
    if per_task:
        num("commitments_per_task_min_med_max", f"{per_task[0]}/{statistics.median(per_task)}/{per_task[-1]}")
    num("predicates_used_by_ledgers", f"{len(pred_used)} of {len(predicates.REGISTRY)}")
    num("predicate_usage", dict(pred_used.most_common()))
    num("predicates_in_no_commitment", sorted(set(predicates.REGISTRY) - set(pred_used)))
    num("task_dirs", len([p for p in sbr_paths.TASKS.iterdir() if p.is_dir()]) if sbr_paths.TASKS.is_dir() else 0)
    num("run_dirs_on_disk", len([p for p in sbr_paths.RUNS_ROOT.iterdir() if p.is_dir()]))
    num("workspaces_on_disk", len([p for p in sbr_paths.WS_ROOT.iterdir() if p.is_dir()]))
    num("results_files", len(list(RES.glob("*"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
