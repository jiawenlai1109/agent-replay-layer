"""cost_to_success from artifacts we already have. No new runs, no provider call.

The question the project is allowed to answer is not "is recovery cheap" but
"given drift at step k, what do you spend to reach a green ledger". Three ways to
get there are already on disk: rerun from scratch (arm A), upstream round-level
resume (arm B), rewind-to-step with or without a directive (arm C).

Two numbers come out of here:

  cost_recovery_usd      what the recovery attempt itself spent. Sound: the
                         resumed attempt's events are the newest ones on disk.
  share_after_detection  what was spent after the probe already knew. Sound only
                         where every step the probe counted is still on disk: a
                         resume rewrites the round trajectory wholesale, so the
                         interrupted segment is read back from the
                         `*_episodes/*/provider_stream.jsonl` mirror. That
                         reconciles on 11 of the 22 C samples; the other 11 stay
                         flagged and out of the metric.

Step attribution is by event millisecond timestamps, never by sequence position:
see tap.cost_timeline for why the file walk and the clock disagree.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import runner
import tap
import sbr_paths

MANIFESTS = sbr_paths.MANIFESTS
OUT = sbr_paths.RESULTS / "cost_to_success.csv"


def arm_of(run_id: str) -> str:
    for tag in ("C-ROLL", "C-REC", "-B1-", "-A1-"):
        if tag in run_id:
            return tag.strip("-")
    return "?"


def score_row(m: dict) -> dict:
    mt = m.get("metrics") or {}
    rid = m["run_id"]
    run_dir = runner.RUNS_ROOT / rid
    # metrics.provider_steps is present in 1 of the 44 manifests, so the step
    # count is taken from the event stream itself. The episode mirror is read too:
    # a resume rewrites the round file wholesale, so the interrupted attempt often
    # survives only under *_episodes/epNNN/provider_stream.jsonl.
    events = tap.cost_timeline(run_dir, with_episodes=True)
    row = {
        "run_id": rid, "task": m.get("task_id"), "arm": arm_of(rid),
        "cost_usd": round(mt.get("cost") or 0, 5),
        "norm_steps": mt.get("steps"),
        "provider_steps": len(events) or None,
        "episodes_only_steps": sum(1 for e in events if e["source"] == "episodes") or 0,
        "end_verdict": ((m.get("outcome") or {}).get("ledger_verdict")
                        or (m.get("verdict_end_state") or {}).get("ledger_verdict")
                        or ("satisfied" if (m.get("outcome") or {}).get("satisfied_by_ledger") else "unknown")),
    }
    sen = m.get("sensor") or {}
    if not (isinstance(sen.get("provider_step"), int) and m.get("injected_at_provider_step") is not None):
        return row

    n = sen["provider_step"]
    sp = tap.split_at_detection(events, n)
    bnd = tap.attempt_boundary(run_dir)
    att = tap.attempt_split(events, bnd)
    row["detect_after_inject"] = sen["detected_lag_steps"]
    row["n_at_detect"] = n
    row["events_total"] = sp["events_total"]
    row["cut_ts_ms"] = sp["cut_ts_ms"]
    row["tie_at_cut"] = sp["tie_at_cut"]
    row["no_ts_events"] = sp["no_ts_events"]
    row["no_ts_cost"] = sp["no_ts_cost"]
    row["cost_after_detection_usd"] = sp["cost_after_usd"]
    row["timeline_cost_usd"] = sp["timeline_cost_usd"]
    row["boundary_ts_ms"] = bnd["ts_ms"]
    row["resume_epoch"] = bnd["resume_epoch"]
    row["events_before_boundary"] = att["steps_before"]
    row["events_recovery"] = att["steps_recovery"]
    row["cost_recovery_usd"] = att["cost_recovery_usd"]
    row["attempt1_events_lost"] = None if att["steps_before"] is None else n - att["steps_before"]

    # Numerator and denominator must come from the same universe, so both shares
    # are taken against the timeline total. metrics.cost is computed independently
    # by runner.collect and printed beside it: if the two totals disagree, the
    # share is unfounded whatever the arithmetic says.
    denom = sp["timeline_cost_usd"]
    row["share_after_detection"] = (round(sp["cost_after_usd"] / denom, 3)
                                    if (sp["cost_after_usd"] is not None and denom) else None)
    row["share_recovery"] = (round(att["cost_recovery_usd"] / denom, 3)
                             if (att["cost_recovery_usd"] is not None and denom) else None)
    row["denom_metrics_usd"] = round(mt.get("cost") or 0, 6)
    # runner.collect sums the round path only, while this denominator is the union
    # with the episode mirror — a superset by design. So the check is one-sided:
    # the union may legitimately exceed metrics.cost (that excess is exactly the
    # events a resume erased from the round path), but it may never fall below it.
    row["denom_extra_pct"] = (None if not denom or mt.get("cost") is None
                              else round(100 * (denom - (mt.get("cost") or 0)) / denom, 2))
    flags = []
    if sp["error"]:
        flags.append(sp["error"])
    if row["share_after_detection"] is not None and not 0.0 <= row["share_after_detection"] <= 1.0:
        flags.append("impossible_share")
    if row["denom_extra_pct"] is not None and row["denom_extra_pct"] < -1.0:
        flags.append("denoms_inverted")
    if att["error"]:
        flags.append(att["error"])
    lost = row["attempt1_events_lost"]
    if lost is not None and lost > 0:
        # The cut refers to steps that are no longer on disk, so "spend after the
        # cut" quietly counts recovery work as waste. That is the failure mode
        # this file was rewritten to catch.
        flags.append("attempt1_events_missing_%d" % lost)
    elif lost is not None and lost < 0:
        flags.append("more_pre_halt_events_than_probe_counted")
    if not att["error"] and att["no_ts"]:
        flags.append("attempt_split_dropped_%d_no_ts" % att["no_ts"])
    row["share_suspect"] = ";".join(flags)
    # The driver now samples cost at the halt itself, which is the only version of
    # "what did we spend after we already knew" that doesn't depend on the
    # interrupted attempt having survived on disk. Where it exists it wins.
    ah = m.get("at_halt") or {}
    row["after_detect_source"] = "live" if ah.get("steps_after_detection") is not None else "offline"
    row["steps_after_detection_live"] = ah.get("steps_after_detection")
    row["cost_after_detection_live_usd"] = ah.get("cost_after_detection_usd")
    row["seconds_after_detection_live"] = ah.get("seconds_after_detection")
    row["mid_flight_step_cut"] = ah.get("mid_flight_step_cut")
    row["recovery_publishable"] = "" if (att["error"] or row["share_recovery"] is None) else "yes"
    # The after-detection number needs more than a computed value: it is only
    # meaningful when every step the probe counted is still on disk, otherwise the
    # "waste" is really the erased segment plus the recovery work.
    row["after_detect_publishable"] = "yes" if (
        lost == 0 and not sp["error"] and not att["error"]
        and row["share_after_detection"] is not None
        and not {"impossible_share", "denoms_inverted"} & set(flags)) else ""
    return row


KEYS = ["run_id", "task", "arm", "provider_steps", "norm_steps", "cost_usd",
        "end_verdict", "detect_after_inject", "n_at_detect", "events_total",
        "episodes_only_steps", "cut_ts_ms", "tie_at_cut", "no_ts_events", "no_ts_cost",
        "boundary_ts_ms", "resume_epoch", "events_before_boundary", "events_recovery",
        "attempt1_events_lost", "cost_after_detection_usd", "cost_recovery_usd",
        "after_detect_source", "steps_after_detection_live", "cost_after_detection_live_usd",
        "seconds_after_detection_live", "mid_flight_step_cut",
        "timeline_cost_usd", "denom_metrics_usd", "denom_extra_pct",
        "share_after_detection", "share_recovery",
        "recovery_publishable", "after_detect_publishable", "share_suspect"]


def main() -> int:
    per_task: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for p in sorted(MANIFESTS.glob("*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        if not (m.get("metrics") or {}).get("steps"):
            continue
        m.setdefault("run_id", p.stem)
        row = score_row(m)
        per_task[row["task"]][row["arm"]].append(row)

    rows = [r for t in per_task.values() for a in t.values() for r in a]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=KEYS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    det = [r for r in rows if r.get("n_at_detect") is not None]
    pub = [r for r in det if r.get("recovery_publishable") == "yes"]
    ad = [r for r in det if r.get("after_detect_publishable") == "yes"]
    print("%-30s %-7s %3s  %8s  %9s %9s %9s  %s"
          % ("task", "arm", "n", "med_usd", "recovery", "det_share", "lost_ev", "end verdicts"))
    for task in sorted(per_task):
        for arm in ("A1", "B1", "C-ROLL", "C-REC"):
            sub = per_task[task].get(arm) or []
            if not sub:
                continue

            def stat(key, gate, group=sub):
                vals = [float(r[key]) for r in group
                        if r.get(key) not in (None, "") and r.get(gate) == "yes"]
                return "—" if not vals else "%.4f" % statistics.median(vals)

            print("%-30s %-7s %3d  %8.4f  %9s %9s %9s  %s" % (
                task[:30], arm, len(sub),
                statistics.median([r["cost_usd"] for r in sub]),
                stat("cost_recovery_usd", "recovery_publishable"),
                stat("share_after_detection", "after_detect_publishable"),
                stat("attempt1_events_lost", "recovery_publishable").replace(".0000", ""),
                ",".join(sorted({str(r["end_verdict"])[:6] for r in sub}))))

    print("\nC 档带检出行 %d ｜ 恢复成本可发布 %d ｜ 检出后口径可发布 %d" % (len(det), len(pub), len(ad)))
    if ad:
        sh = [float(r["share_after_detection"]) for r in ad]
        co = [float(r["cost_after_detection_usd"]) for r in ad]
        print("检出后仍花的：中位 %.1f%%  范围 %.1f%%–%.1f%% ｜ 绝对值中位 %.4f USD ｜ n=%d（离线重算）"
              % (100 * statistics.median(sh), 100 * min(sh), 100 * max(sh),
                 statistics.median(co), len(ad)))
        recov = [float(r["share_recovery"]) for r in ad]
        print("  其中恢复尝试本身：中位 %.1f%% ⇒ 报警到停机之间**完成**的 provider 步为 0（计数与停手前事件数逐条相等）"
              % (100 * statistics.median(recov)))
    live = [r for r in det if r.get("after_detect_source") == "live"]
    if live:
        post = [int(r["steps_after_detection_live"]) for r in live]
        cl = [float(r["cost_after_detection_live_usd"] or 0) for r in live]
        cut = sum(1 for r in live if r.get("mid_flight_step_cut"))
        print("live 口径（停机时采样，n=%d）：报警→停机之间完成步 %s，成本差中位 %.4f USD，"
              "其中 %d/%d 停机时确有一步开着 ⇒ 该步开销在产物里不留痕"
              % (len(live), sorted(set(post)), statistics.median(cl), cut, len(live)))
    short = [int(r["attempt1_events_lost"]) for r in det if int(r.get("attempt1_events_lost") or 0) > 0]
    if short:
        print("记录不完整的 %d/%d 条：盘上停手前事件比探针计数少，中位缺 %d 步 ⇒ 该口径逐行留值但不发布"
              % (len(short), len(det), statistics.median(short)))
    bad = [r for r in det if r["share_suspect"]]
    for r in bad[:5]:
        print("  ! %-44s %s" % (r["run_id"][:44], r["share_suspect"][:62]))
    if len(bad) > 5:
        print("  ...另 %d 条同类" % (len(bad) - 5))
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
