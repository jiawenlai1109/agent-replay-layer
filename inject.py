"""Family-C: inject a known defect into a task's seed and record the ground truth.

The point is a measurable oracle for first_detectable_step. Each recipe states
when the defect becomes *observable to the deterministic predicate*, so latency
is attributable to the detector rather than to luck:

  detectable_at = "seed"      -> present before step 1; an in-line detector
                                  should fire at step 0, any later fire is pure
                                  detector deficiency.
  detectable_at = "after:X"   -> only once artifact X exists; the bound is the
                                  first step that writes X.

Injecting one artifact always cascades: corrupting the csv also makes the
derived summary stale and the test suite red. So the truth records the FULL
expected alarm set plus which alarm is the root cause — a single-violation
expectation would be wrong by construction, not by detector failure.

The task text is copied unchanged: the agent is never told about the defect.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

SBR = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_base(base_id: str, new_id: str) -> tuple[Path, Path]:
    src_task, src_ledger = SBR / "tasks" / base_id, SBR / "ledger" / base_id
    dst_task, dst_ledger = SBR / "tasks" / new_id, SBR / "ledger" / new_id
    for d in (dst_task, dst_ledger):
        shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(src_task, dst_task)
    shutil.copytree(src_ledger, dst_ledger)
    return dst_task, dst_ledger


def _write_truth(ledger_dir: Path, truth: dict) -> None:
    (ledger_dir / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def csv_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


RECIPES: dict[str, dict] = {
    "C1-csv-short": {
        "file": "data/orders.csv",
        "desc": "删掉最后一条数据行，csv 只剩 11 条",
        "root_cause": "c-orders-cardinality",
        "cascade": ["c-orders-cardinality", "c-summary-recompute", "c-suite-green"],
        "detectable_at": "seed",
        "fix_required": "补回一行，或如实报告行数与任务要求不符",
    },
    "C2-stale-summary": {
        "file": "report/summary.json",
        "desc": "预置一个结构正确但数值与 csv 不符的派生结果（陈旧产物漂移）",
        "root_cause": "c-summary-recompute",
        "cascade": ["c-summary-recompute", "c-suite-green"],
        "detectable_at": "seed",
        "fix_required": "重新生成 summary.json，使派生值与源数据一致",
    },
    "C3-dup-key": {
        "file": "data/orders.csv",
        "desc": "把一个 order_id 改成与另一行重复（行数与各组求和都不变）",
        "root_cause": "c-csv-unique-key",
        "cascade": ["c-csv-unique-key"],
        "detectable_at": "seed",
        "fix_required": "消除重复主键",
    },
    "C4-region-typo": {
        "file": "data/orders.csv",
        "desc": "把一个 region 值改成 'East'（大小写不符，破坏分组键集合）",
        "root_cause": "c-summary-recompute",
        "cascade": ["c-summary-recompute"],
        "detectable_at": "seed",
        "fix_required": "纠正分组键值",
        "note": "不级联到测试：既有测试是动态从 csv 求和的，count 之和仍为 12、sum 仍与 csv 一致，"
                "只有'派生结果与源数据的键集合是否相同'这条不变量看得到它 ⇒ 谓词密度决定可定位性的实例",
    },
}


def apply_recipe(recipe_id: str, seed: Path) -> dict:
    recipe = RECIPES[recipe_id]
    target = seed / recipe["file"]
    before = sha(target) if target.is_file() else None

    if recipe_id == "C1-csv-short":
        header, rows = csv_rows(target)
        assert len(rows) >= 12, "基线种子应有 12 条数据行"
        rows = rows[:-1]
        with target.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
    elif recipe_id == "C2-stale-summary":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"east": {"count": 6, "sum": 999.99}, "west": {"count": 6, "sum": 1.00}}, indent=2) + "\n",
            encoding="utf-8",
        )
    elif recipe_id == "C3-dup-key":
        header, rows = csv_rows(target)
        rows[1][0] = rows[0][0]
        with target.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
    elif recipe_id == "C4-region-typo":
        header, rows = csv_rows(target)
        idx = header.index("region")
        rows[0][idx] = "East"
        with target.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
    else:  # pragma: no cover
        raise ValueError(recipe_id)

    return {"before_sha256": before, "after_sha256": sha(target)}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("可用配方:", ", ".join(sorted(RECIPES)))
        return 2
    for recipe_id in argv[1:]:
        recipe = RECIPES[recipe_id]
        base_id = "T-PIPE-01F"
        new_id = f"{base_id}-{recipe_id}"
        task_dir, ledger_dir = _copy_base(base_id, new_id)
        mutated = apply_recipe(recipe_id, task_dir / "seed")
        _write_truth(
            ledger_dir,
            {
                "base_task": base_id,
                "recipe": recipe_id,
                "injected_file": recipe["file"],
                "description": recipe["desc"],
                "root_cause": recipe["root_cause"],
                "expected_violations": sorted(recipe["cascade"]),
                "detectable_at": recipe["detectable_at"],
                "last_invariant_holding_step": None if recipe["detectable_at"] == "seed" else "until " + recipe["detectable_at"],
                "agent_fix_required": recipe["fix_required"],
                "note": recipe.get("note", ""),
                "seed_file_hashes": mutated,
                "task_text_unchanged": sha(task_dir / "task.md") == sha(SBR / "tasks" / base_id / "task.md"),
                "told_to_agent": False,
            },
        )
        print(
            "%-30s 注入 %-22s 根因 %-20s 级联 %s 可检时机 %s"
            % (
                f"{base_id}-{recipe_id}",
                recipe["file"],
                recipe["root_cause"],
                ",".join(recipe["cascade"]),
                recipe["detectable_at"],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
