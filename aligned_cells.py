#!/usr/bin/env python3
"""Aligned no-rewind cells: same task, same recipe, same budget, D against C-REC.

The cross-arm table cannot answer "is the intervention worth it" because arm A
never sees a drift. This groups runs by (task, rep) and keeps only cells where
both arms were given the same round budget, so the remaining difference is the
intervention itself. Offline: reads manifests, no provider call, no cost.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path

import stats

import sbr_paths
from pair_report import rep_of
from runner import is_provider_fatal

MAN = sbr_paths.MANIFESTS
OUT = sbr_paths.RESULTS / "aligned_cells.csv"


def loaded(man: Path) -> dict | None:
    try:
        d = json.loads(man.read_text(encoding="utf-8"))
    except Exception:
        return None
    met = d.get("metrics") or {}
    if int(met.get("steps") or 0) <= 0:
        return None
    if d.get("aborted"):
        return None
    # A run killed by the credential still has provider steps, and its red is the
    # injected defect left unfixed, not agent behaviour (defect #40).
    if is_provider_fatal(d.get("report") or {}):
        return None
    return d

# A cell is only comparable when the recovery arm was given the same number of
# rounds the no-rewind arm could spend. extra_rounds is the knob that differed in
# the first (unaligned) batch, so cells are grouped by it rather than trusted.
ALIGNED_EXTRA_ROUNDS = 2


def side(d: dict, arm: str) -> dict:
    met = d.get("metrics") or {}
    sen = d.get("sensor") or {}
    v = d.get("verdict_end_state") or {}
    rep = d.get("report") or {}
    return {
        "arm": arm,
        "run_id": d.get("run_id"),
        "steps": int(met.get("steps") or 0),
        "tokens": int(met.get("tokens") or 0),
        # Cache reads are priced far below fresh input, so the money follows the
        # non-cache share rather than the total. Record both or the ratio looks wrong.
        "cache_read": int(met.get("cache_read") or 0),
        "cost_usd": round(float(met.get("cost") or 0.0), 6),
        "rounds": int(met.get("rounds") or 0),
        "injected_at": d.get("injected_at_provider_step"),
        "detected_at": sen.get("provider_step"),
        "lag": sen.get("detected_lag_steps"),
        "broke_promise": sen.get("commitment_id"),
        "rewound": bool(d.get("rollback_ok")) if arm == "C-REC" else False,
        "ledger": v.get("ledger_verdict"),
        "pass": (v.get("latched_counts") or {}).get("pass"),
        "fail": (v.get("latched_counts") or {}).get("fail"),
        "na": (v.get("latched_counts") or {}).get("na"),
        "abort": rep.get("abort_reason") or rep.get("report_status") or "",
    }


def main() -> int:
    cells: dict[tuple, dict] = {}
    skipped = []
    for f in sorted(MAN.glob("*.json")):
        d = loaded(f)
        if not d:
            continue
        budget = d.get("budget") or {}
        if budget.get("extra_rounds") != ALIGNED_EXTRA_ROUNDS:
            continue
        arm = str(d.get("arm") or "")
        if arm not in ("D", "C-REC"):
            continue
        rid = str(d.get("run_id") or f.stem)
        task = d.get("task_id")
        # The model is part of the cell key: two generations of the same task/rep
        # are two cells, never one averaged number (per-million-token prices differ).
        model = str(d.get("model") or "?")
        # One ruler with the paired C-arm report: the rep lives in the arm segment
        # of the run id, not in the trailing timestamp.
        cells.setdefault((model, task, rep_of(rid), budget.get("max_rounds")), {})[arm] = side(d, arm)

    rows = []
    for (model, task, rep_tag, max_rounds), arms in sorted(cells.items(), key=lambda kv: [str(p) for p in kv[0]]):
        if "D" not in arms or "C-REC" not in arms:
            only = "D" if "D" in arms else "C-REC"
            skipped.append(f"{model} {task} rep {rep_tag}: only arm {only}")
            continue
        d, c = arms["D"], arms["C-REC"]
        d_nc, c_nc = d["tokens"] - d["cache_read"], c["tokens"] - c["cache_read"]
        # Cache is a dimension of its own: a hit is priced far below a miss, so a
        # saving can come from reusing cached prefix instead of doing less work. The
        # two arms' hit rates are printed next to the ratios to tell those apart.
        rows.append({
            "model": model, "task": task, "rep": rep_tag, "max_rounds": max_rounds,
            "d_steps": d["steps"], "rec_steps": c["steps"],
            "d_tokens": d["tokens"], "rec_tokens": c["tokens"],
            "d_cache_read": d["cache_read"], "rec_cache_read": c["cache_read"],
            "d_cache_hit_rate": round(d["cache_read"] / d["tokens"], 4) if d["tokens"] else "",
            "rec_cache_hit_rate": round(c["cache_read"] / c["tokens"], 4) if c["tokens"] else "",
            "d_noncache_tokens": d_nc, "rec_noncache_tokens": c_nc,
            "cache_ratio_rec_over_d": round(c["cache_read"] / d["cache_read"], 4) if d["cache_read"] else "",
            "noncache_ratio_rec_over_d": round(c_nc / d_nc, 4) if d_nc else "",
            "d_cost_usd": d["cost_usd"], "rec_cost_usd": c["cost_usd"],
            "cost_ratio_rec_over_d": round(c["cost_usd"] / d["cost_usd"], 4) if d["cost_usd"] else "",
            "token_ratio_rec_over_d": round(c["tokens"] / d["tokens"], 4) if d["tokens"] else "",
            "d_ledger": d["ledger"], "rec_ledger": c["ledger"],
            "d_lag": d["lag"], "rec_lag": c["lag"],
            "d_promise": d["broke_promise"], "rec_promise": c["broke_promise"],
            "rec_rewound": c["rewound"],
            "d_abort": d["abort"], "rec_abort": c["abort"],
            # a cell only speaks when both arms actually finished the ledger
            "both_satisfied": d["ledger"] == "satisfied" and c["ledger"] == "satisfied",
            "rec_cheaper": c["cost_usd"] < d["cost_usd"],
            "rec_cheaper_noncache": bool(d_nc) and c_nc < d_nc,
        })

    if not rows:
        print("no aligned cells on disk (need both arms at extra_rounds=%d)" % ALIGNED_EXTRA_ROUNDS)
        return 0

    hdr = list(rows[0])
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=hdr)
        w.writeheader()
        w.writerows(rows)

    # Cost and token numbers are only ever compared inside one model, so every
    # aggregate below is printed per generation instead of pooled across models.
    for model in sorted({str(r["model"]) for r in rows}):
        sub = [r for r in rows if r["model"] == model]
        print(f"\n== 对齐格对 model={model}（同任务同 rep，两臂同为 max_rounds 且 extra_rounds={ALIGNED_EXTRA_ROUNDS}）")
        for r in sub:
            print(f"  {r['task']} rep {r['rep']}: D {r['d_cost_usd']} USD / {r['d_tokens']} tok / {r['d_steps']} 步 / {r['d_ledger']}"
                  f"  |  C-REC {r['rec_cost_usd']} USD / {r['rec_tokens']} tok / {r['rec_steps']} 步 / {r['rec_ledger']}"
                  f"  =>  钱 x{r['cost_ratio_rec_over_d']}  token x{r['token_ratio_rec_over_d']}"
                  f"  非缓存 token x{r['noncache_ratio_rec_over_d']}"
                  f"  命中率 D/C={r['d_cache_hit_rate']}/{r['rec_cache_hit_rate']}"
                  f"  rewind={r['rec_rewound']} lag D/C={r['d_lag']}/{r['rec_lag']}")
        fired = [r for r in sub if r["rec_rewound"]]
        print(f"  干预未触发的格（C-REC 未回滚，两臂走同一条路）：{len(sub) - len(fired)}/{len(sub)}")
        both = [r for r in sub if r["both_satisfied"]]
        cheaper = sum(1 for r in both if r["rec_cheaper"])
        ratios = [float(r["cost_ratio_rec_over_d"]) for r in both if r["cost_ratio_rec_over_d"] != ""]
        tratios = [float(r["token_ratio_rec_over_d"]) for r in both if r["token_ratio_rec_over_d"] != ""]
        print(f"  格子数 {len(sub)}，两臂都做成 {len(both)}，其中 C-REC 更便宜 {cheaper}/{len(both)}")
        if ratios:
            print(f"  成本比(C-REC/D)：中位 {statistics.median(ratios):.4f}，范围 {min(ratios):.4f}..{max(ratios):.4f}")
        if tratios:
            print(f"  token 比(C-REC/D)：中位 {statistics.median(tratios):.4f}，范围 {min(tratios):.4f}..{max(tratios):.4f}")
        if both:
            dt = sum(r["d_tokens"] for r in both)
            ct = sum(r["rec_tokens"] for r in both)
            dc = round(sum(r["d_cost_usd"] for r in both), 6)
            cc = round(sum(r["rec_cost_usd"] for r in both), 6)
            dh = sum(r["d_cache_read"] for r in both)
            ch = sum(r["rec_cache_read"] for r in both)
            dn = sum(r["d_noncache_tokens"] for r in both)
            cn = sum(r["rec_noncache_tokens"] for r in both)
            if dt and dc:
                print(f"  合计（两臂都做成的格）：D {dt} tok / {dc} USD  vs  C-REC {ct} tok / {cc} USD"
                      f" => 总 token x{ct / dt:.4f} 钱 x{cc / dc:.4f}")
            # Hit and miss are their own lines because they are priced differently:
            # the money saving should track the miss share, and the hit rates have to
            # be close or the two arms did not start from the same caching condition.
            if dn and dh and dt and ct:
                hr_d, hr_c = dh / dt, ch / ct
                print(f"  命中（缓存读，单价低）：D {dh} tok（命中率 {hr_d:.1%}）vs C-REC {ch} tok（{hr_c:.1%}）=> x{ch / dh:.4f}")
                print(f"  未命中（按全价计的那部分）：D {dn} tok vs C-REC {cn} tok => x{cn / dn:.4f}"
                      f"；命中率差 {hr_d - hr_c:+.1%} ⇒ "
                      + ("两臂缓存条件可比，省下来的不是缓存运气"
                         if abs(hr_d - hr_c) < 0.05 else
                         "命中率差在 5 个点以上，两臂不满足同一缓存条件，比值不能当省钱讲"))
            nr = [float(r["noncache_ratio_rec_over_d"]) for r in both if r["noncache_ratio_rec_over_d"] != ""]
            if nr:
                print(f"  未命中 token 比：中位 {statistics.median(nr):.4f}，范围 {min(nr):.4f}..{max(nr):.4f}")
            ncheap = sum(1 for r in both if r["rec_cheaper_noncache"])
            print(f"  未命中口径下 C-REC 更省 {ncheap}/{len(both)}，符号检验 p = {stats.sign_test(ncheap, len(both) - ncheap):.4f}")
            print(f"  符号检验（钱口径）p = {stats.sign_test(cheaper, len(both) - cheaper):.4f}"
                  f"   # n={len(both)} 时全同号的下限就是 1/2^n")
    for s in skipped:
        print(f"  跳过：{s}")
    print(f"wrote {OUT} rows {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
