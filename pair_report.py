"""Paired C-arm report: same task, same injection recipe, directive vs none.

Offline. It reads each arm_c record plus the durable trajectory, and attributes
post-resume work by event timestamp (never by directory mtime, which is the last
write and mislabels a round that spans a restart).

Definitions, so the table cannot be read loosely later:
  detected      the sensor's first red strictly after our own injection
  lag           detection step minus injection step, in provider steps
  post_work     executor provider steps whose events are timestamped after the
                resumed epoch began — "did the extra round do anything"
  end_satisfied 3-valued ledger verdict at the end of the whole run
  recovered     end_satisfied == satisfied AND the ledger was red at detection
                time, i.e. the promise that broke is the one that ends green

Usage: python3 pair_report.py [glob-filter]
"""

from __future__ import annotations

import csv
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import audit
import runner
import stats
import tap
import sbr_paths

MANIFESTS = sbr_paths.MANIFESTS
OUT = sbr_paths.RESULTS / "pair_report.csv"


def rep_of(run_id: str) -> str:
    """The rep lives in the arm segment (`...-C-ROLL10-172033` -> "10"), not in
    the trailing timestamp, which is what first made every pair miss."""
    m = re.search(r"-(?:C-ROLL|C-REC|D|B|A)(\d+)-\d{6}$", run_id)
    return m.group(1) if m else "?"


def rows(pattern: str) -> list[dict]:
    out = []
    for p in sorted(MANIFESTS.glob(pattern)):
        m = json.loads(p.read_text(encoding="utf-8"))
        if "sensor" not in m and "injected_at_provider_step" not in m:
            continue
        # A run that never reached the provider also never reached the sensor.
        # Counting those as "the sensor missed the drift" would sink the
        # detection rate with nothing but a dead credential.
        if not (m.get("metrics") or {}).get("steps"):
            continue
        out.append(m)
    return out


def attributable(m: dict) -> bool | None:
    """Was the promise this recipe breaks green in the last reading before the
    injection? If not, the detection measures the seed's own defect, not ours,
    and the lag for that sample must not enter the latency statistic.
    """
    root = (m.get("injection") or {}).get("root_cause")
    inj = m.get("injected_at_provider_step")
    if not root or inj is None:
        return None
    pre = [r for r in (m.get("readings") or [])
           if isinstance(r.get("step"), int) and r["step"] <= inj and isinstance(r.get(root), dict)]
    if not pre:
        return None
    return pre[-1][root]["status"] == "pass"


def score(m: dict) -> dict:
    rid = m["run_id"]
    st, snap = runner.call("GET", f"/api/runs/{rid}/status")
    owner = (snap or {}).get("owner") or {}
    epoch = owner.get("resume_epoch")
    start = owner.get("started_at")
    sen = m.get("sensor") or {}
    split = tap.epoch_split(runner.RUNS_ROOT / rid, start) if start else {}
    post = sum(r.get("executor", {}).get("after", 0) for r in (split.get("rounds") or {}).values())
    ws = Path(m.get("workspace") or "")
    verdict = "run_missing" if st != 200 else "workspace_missing"
    if ws.is_dir():
        lat = audit.latched(audit.audit_run(m["task_id"], runner.RUNS_ROOT / rid, ws))
        verdict = audit.verdict_of(lat)
    inj = m.get("injected_at_provider_step")
    return {
        "run_id": rid,
        "task": m["task_id"],
        "arm": m.get("arm"),
        # A run detected before DESIGN #19 needed `injected_at` to avoid cutting a
        # run whose seed was already red; a v1 run does not. The two are different
        # instruments and must never share one table.
        "trigger_rule": str(m.get("trigger_rule") or "v0_red_after_injection"),
        # Per-million-token price differs by model, so cost columns are only
        # comparable within one model.
        "model": str(m.get("model") or "?"),
        "rep": rep_of(rid),
        "injected_at": inj,
        "detected_at": sen.get("provider_step"),
        "lag": sen.get("detected_lag_steps"),
        "broke_promise": sen.get("commitment_id"),
        "root_cause_promise": (m.get("injection") or {}).get("root_cause"),
        "hit_root_cause": bool(sen.get("commitment_id")) and sen.get("commitment_id") == (m.get("injection") or {}).get("root_cause"),
        "attributable": attributable(m),
        "rollback_ok": m.get("rollback_ok"),
        "stopped_before_resume": m.get("stopped_before_resume"),
        "resume_epoch": epoch,
        "post_resume_executor_steps": post if epoch else 0,
        "end_verdict": verdict,
        "recovered": verdict == "satisfied" and bool(epoch),
        "cost_usd": round((m.get("metrics") or {}).get("cost") or 0, 5),
    }


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    rule = None
    model = None
    for i, a in enumerate(sys.argv):
        if a == "--rule" and i + 1 < len(sys.argv):
            rule = sys.argv[i + 1]
            argv = [x for x in argv if x != rule]
        if a == "--model" and i + 1 < len(sys.argv):
            model = sys.argv[i + 1]
            argv = [x for x in argv if x != model]
    pat = argv[0] if argv else "*C-R*"
    recs = [score(m) for m in rows(pat)]
    recs = [r for r in recs if r["end_verdict"] != "run_missing"]
    if rule:
        recs = [r for r in recs if r["trigger_rule"].startswith(rule) or rule == "all"]
        print(f"-- 只保留触发规则 {rule!r}：{len(recs)} 条")
    if model:
        recs = [r for r in recs if r["model"] == model or model == "all"]
        print(f"-- 只保留模型 {model!r}：{len(recs)} 条")
    if not recs:
        print("no samples for that pattern/rule; nothing to report")
        return 0
    rules = sorted({r["trigger_rule"] for r in recs})
    if len(rules) > 1:
        print("!! 同一张表里混了触发规则:",
              {k: sum(1 for r in recs if r["trigger_rule"] == k) for k in rules},
              "—— v0(靠 injected_at) 与 v1(新增红灯) 不可合并引用，请用 [glob] 分开跑")
    models = sorted({r["model"] for r in recs})
    if len(models) > 1:
        print("!! 同一张表里混了模型:",
              {k: sum(1 for r in recs if r["model"] == k) for k in models},
              "—— 每百万 token 单价不同，成本数字不可跨模型合并，请用 --model 分开跑")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(recs[0]) if recs else ["task"])
        w.writeheader()
        w.writerows(recs)

    by: dict[tuple, dict] = defaultdict(dict)
    for r in recs:
        by[(r["task"], r["rep"])][r["arm"]] = r

    print("样本:", len(recs), "| USD:", round(sum(r["cost_usd"] for r in recs), 4))
    for arm in ("C-ROLL", "C-REC"):
        sub = [r for r in recs if r["arm"] == arm and r["resume_epoch"]]
        if not sub:
            continue
        worked = sum(1 for r in sub if r["post_resume_executor_steps"] > 0)
        att = [r for r in sub if r["attributable"] is True]
        lags = [r["lag"] for r in att if isinstance(r["lag"], int)]
        hits = sum(1 for r in sub if r["hit_root_cause"])
        print("  %-7s n=%2d 恢复后派活 %d/%d | 可归因 lag 中位 %s 范围 %s (n=%d) | 检出即根因 %d/%d | 终态满足 %d/%d" % (
            arm, len(sub), worked, len(sub),
            sorted(lags)[len(lags) // 2] if lags else "—",
            (min(lags), max(lags)) if lags else "—", len(att),
            hits, len(sub),
            sum(1 for r in sub if r["end_verdict"] == "satisfied"), len(sub)))
        if r_not := [r for r in sub if r["attributable"] is not True]:
            print("        其中 %d 条不计入 lag：注入前该承诺已红或读数缺失（%s）" % (
                len(r_not), ", ".join(r["run_id"][-18:] for r in r_not[:4])))

    pairs = [(k, v) for k, v in sorted(by.items()) if "C-ROLL" in v and "C-REC" in v]
    print("\n配对（同任务同注入，只差 RECOVERY.md）: %d 对" % len(pairs))
    print("  %-30s %-4s | ROLL 后派活/终态 | REC 后派活/终态" % ("task", "rep"))
    for (task, rep), v in pairs:
        a, b = v["C-ROLL"], v["C-REC"]
        print("  %-30s %-4s | %2d %-18s | %2d %s" % (
            task[:30], rep, a["post_resume_executor_steps"], a["end_verdict"],
            b["post_resume_executor_steps"], b["end_verdict"]))
    won_roll = sum(1 for _, v in pairs if v["C-ROLL"]["post_resume_executor_steps"] == 0 and v["C-REC"]["post_resume_executor_steps"] > 0)
    won_rec = sum(1 for _, v in pairs if v["C-REC"]["post_resume_executor_steps"] == 0 and v["C-ROLL"]["post_resume_executor_steps"] > 0)
    print("  指令独占派活的对: %d | 反向（无指令才派活）: %d | 总对数: %d" % (won_roll, won_rec, len(pairs)))

    # Significance comes from stats.py, never from a throwaway snippet: an ad-hoc
    # hypergeometric sum printed p=0.0013 for the dispatch comparison when the
    # same table gives 0.0227 (defect #28).
    resumed = [r for r in recs if r["resume_epoch"]]
    def cell(arm, pred):
        sub = [r for r in resumed if r["arm"] == arm]
        return sum(1 for r in sub if pred(r)), len(sub) - sum(1 for r in sub if pred(r))
    rd, rn = cell("C-REC", lambda r: r["post_resume_executor_steps"] > 0)
    ld, ln = cell("C-ROLL", lambda r: r["post_resume_executor_steps"] > 0)
    rs, _ = cell("C-REC", lambda r: r["end_verdict"] == "satisfied")
    ls, _ = cell("C-ROLL", lambda r: r["end_verdict"] == "satisfied")
    n_rs = sum(1 for r in resumed if r["arm"] == "C-REC")
    n_ls = sum(1 for r in resumed if r["arm"] == "C-ROLL")
    print("\n显著性（单尾精确检验，n 只算真正恢复过的 run）")
    print("  恢复后派活   %d/%d vs %d/%d  Fisher p=%.4f" % (rd, n_rs, ld, n_ls, stats.fisher_greater(rd, rn, ld, ln)))
    print("  终态 satisfied %d/%d vs %d/%d  Fisher p=%.4f" % (rs, n_rs, ls, n_ls, stats.fisher_greater(rs, n_rs - rs, ls, n_ls - ls)))
    print("  配对符号检验 独占 %d / 反向 %d  n=%d  p=%.4f（n=4 时下限就是 0.0625）" % (
        won_roll, won_rec, won_roll + won_rec, stats.sign_test(won_roll, won_rec)))
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
