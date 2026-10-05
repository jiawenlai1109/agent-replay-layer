#!/usr/bin/env python3
"""Replay steps and wall clock per run, derived offline from manifests.

The three-week plan asks for "恢复到成功所需的平均重放步数". The driver cannot give
that number exactly: `readings` and `status_flips` stop at the halt, so no sampling
happens after the resume and the step at which the ledger turns green again is not
recorded. What IS recoverable from existing artifacts is the distance from the reuse
point to the end of the run, which bounds the replay from above. It is printed under
that name, not as the exact metric.

Wall clock comes from `data_census.wall_seconds` (first-to-last provider event in the
run directory) so both scripts share one ruler.

No provider call, no cost.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path

import sbr_paths
from data_census import wall_seconds

MAN = sbr_paths.MANIFESTS
OUT = sbr_paths.RESULTS / "replay_steps.csv"
RECOVERY_ARMS = ("C-REC", "C-ROLL")
HDR = ["run_id", "task", "arm", "model", "steps_total", "wall_s", "cost_usd", "tokens",
       "ledger", "rewind_target", "detect_step", "detect_lag", "promise",
       "steps_after_rewind", "steps_after_detect", "cost_after_detect_usd"]


def loaded(man: Path) -> dict | None:
    try:
        d = json.loads(man.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int((d.get("metrics") or {}).get("steps") or 0) <= 0:
        return None
    return d


def row(f: Path) -> dict | None:
    d = loaded(f)
    if not d:
        return None
    met = d.get("metrics") or {}
    sen = d.get("sensor") or {}
    halt = d.get("at_halt") or {}
    arm = str(d.get("arm") or "")
    total = int(met.get("steps") or 0)
    rid = str(d.get("run_id") or f.stem)
    target = d.get("last_invariant_holding_provider_step")
    detect = sen.get("provider_step")
    out = {
        "run_id": rid,
        "task": d.get("task_id"),
        "arm": arm,
        "model": str(d.get("model") or "?"),
        "steps_total": total,
        "wall_s": wall_seconds(rid),
        "cost_usd": round(float(met.get("cost") or 0.0), 6),
        "tokens": int(met.get("tokens") or 0),
        "ledger": (d.get("verdict_end_state") or {}).get("ledger_verdict"),
        "rewind_target": target,
        "detect_step": detect,
        "detect_lag": sen.get("detected_lag_steps"),
        "promise": sen.get("commitment_id"),
        "cost_after_detect_usd": halt.get("cost_after_detection_usd"),
    }
    if isinstance(detect, int):
        out["steps_after_detect"] = total - detect
    if arm in RECOVERY_ARMS and d.get("rollback_ok") and isinstance(target, int):
        out["steps_after_rewind"] = total - target
    return out


def block(vals: list[str], label: str) -> str:
    if not vals:
        return f"  {label}: n=0"
    return (f"  {label}: n={len(vals)}  中位 {statistics.median(vals):.1f}  "
            f"均值 {statistics.mean(vals):.1f}  范围 {min(vals):.0f}..{max(vals):.0f}")


def main() -> int:
    rows = [r for r in (row(f) for f in sorted(MAN.glob("*.json"))) if r]
    if not rows:
        print("no manifests")
        return 0
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HDR, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print("== 墙钟（run 目录内首末 provider 事件时间戳差，秒；口径同 data_census）")
    # Wall clock is a property of the model as much as of the arm, so it is grouped
    # by arm and model rather than pooled across generations.
    for arm in ("A", "B", "C-ROLL", "C-REC", "D"):
        for model in sorted({str(r["model"]) for r in rows if r["arm"] == arm}):
            sub = [r["wall_s"] for r in rows if r["arm"] == arm and r["model"] == model and r["wall_s"]]
            if sub:
                print(f"  {arm:6s} @ {model}: n={len(sub):3d}  中位 {statistics.median(sub):7.1f}s  "
                      f"均值 {statistics.mean(sub):7.1f}s  合计 {round(sum(sub) / 60, 1):6.1f}min")

    print("\n== 复用点后执行步数（上界口径，见文件头：resume 后不再逐拍记账）")
    for arm in RECOVERY_ARMS:
        for model in sorted({r["model"] for r in rows if r["arm"] == arm}):
            allv = [r["steps_after_rewind"] for r in rows
                    if r["arm"] == arm and r["model"] == model and r.get("steps_after_rewind") is not None]
            okv = [r["steps_after_rewind"] for r in rows
                   if r["arm"] == arm and r["model"] == model and r["ledger"] == "satisfied"
                   and r.get("steps_after_rewind") is not None]
            key = f"{arm} @ {model}"
            print(block(allv, f"{key} 全部有回滚的 run"))
            print(block(okv, f"{key} 结局 satisfied"))

    print("\n== D 档对照：检测点之后继续烧掉的步数（没有重放，只有继续跑）")
    for model in sorted({r["model"] for r in rows if r["arm"] == "D"}):
        sub = [r["steps_after_detect"] for r in rows
               if r["arm"] == "D" and r["model"] == model and r.get("steps_after_detect") is not None]
        print(block(sub, f"D @ {model}"))
    print(f"wrote {OUT} rows {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
