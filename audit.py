"""Run a task's commitment ledger against a workspace and/or a finished run.

Offline (tap) mode only: it reads durable artifacts after the fact and changes
nothing. In-line veto stays disabled until the false-positive audit (G3) passes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import predicates
from taskset import LEDGER, load_commitments


def _alarm(commitment: dict[str, Any], verdict: dict[str, Any], step: Any) -> dict[str, Any]:
    return {
        "commitment_id": commitment["id"],
        "predicate": commitment["predicate"],
        "status": verdict["status"],
        "expected": verdict["expected"],
        "actual": verdict["actual"],
        "trigger_cmd": verdict["cmd_line"],
        "step": step,
        "provenance": "tap",
    }


def audit_workspace(task_id: str, workspace: Path) -> list[dict[str, Any]]:
    alarms = []
    for c in _commitments(task_id, "workspace"):
        args = {**c.get("args", {}), "workspace": str(workspace)}
        fn = predicates.REGISTRY[c["predicate"]]
        alarms.append(_alarm(c, fn(args), step=None))
    return alarms


def _commitments(task_id: str, applies_to: str) -> list[dict[str, Any]]:
    return [c for c in load_commitments(task_id) if c["applies_to"] == applies_to]


def audit_run(task_id: str, run_dir: Path, workspace: Path | None = None) -> list[dict[str, Any]]:
    """Add the round-level sensors that need the ledger, not the filesystem."""
    alarms = audit_workspace(task_id, workspace) if workspace else []
    rounds_path = Path(run_dir) / "lh_harness" / "role_orchestration" / "rounds.jsonl"
    if not rounds_path.is_file():
        return alarms
    rounds = predicates.read_rounds(rounds_path)
    for c in _commitments(task_id, "round"):
        fn = predicates.REGISTRY[c["predicate"]]
        for at in range(1, len(rounds) + 1):
            alarms.append(_alarm(c, fn({"rounds": rounds, "at": at}), step=f"round_{at:03d}"))
    for c in _commitments(task_id, "episode_metadata"):
        fn = predicates.REGISTRY[c["predicate"]]
        for meta_path in sorted(Path(run_dir).glob("lh_harness/role_orchestration/rounds/round_*/*_metadata.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            alarms.append(_alarm(c, fn({"metadata": meta}), step=meta_path.parent.name + "/" + meta_path.name))
    return alarms


_RANK = {"fail": 3, "pass": 2, "na": 1}


def verdict_of(latched: dict[str, dict[str, Any]]) -> str:
    """Three values, because "not evaluable" is not the same claim as "violated".

    Folding na into satisfied_by_ledger made a run with 8 passes and one
    unevaluable promise read as a failure in the headline column.
    """
    statuses = [v["status"] for v in latched.values()]
    if not statuses:
        return "no_commitments"
    if any(s == "fail" for s in statuses):
        return "violated"
    if any(s == "na" for s in statuses):
        return "incomplete_evidence"
    return "satisfied"



def latched(alarms: list[dict[str, Any]]) -> dict[str, Any]:
    """Sticky verdict per commitment.

    fail wins and keeps its earliest step (the sticky red of DESIGN §4); a later
    pass must not be masked by an earlier na, because na means "not yet
    evaluable here", not "holds".
    """
    out: dict[str, Any] = {}
    for a in alarms:
        cur = out.get(a["commitment_id"])
        if cur is None:
            out[a["commitment_id"]] = {"status": a["status"], "first_step": a["step"], "seen": 1}
            continue
        cur["seen"] += 1
        if _RANK[a["status"]] > _RANK[cur["status"]]:
            out[a["commitment_id"]] = {"status": a["status"], "first_step": a["step"], "seen": cur["seen"]}
    return out


def main(run_dir: str, task_id: str, workspace: str | None = None) -> int:
    alarms = audit_run(task_id, Path(run_dir), Path(workspace) if workspace else None)
    for a in alarms:
        if a["status"] != "pass":
            print(f"[{a['status']:<4}] {a['commitment_id']:<28} {a['predicate']:<28} step={a['step']} :: {str(a['actual'])[:90]}")
    counts = {s: sum(1 for a in alarms if a["status"] == s) for s in ("pass", "fail", "na")}
    print(f"-- checks: {counts}")
    print("-- latched:", json.dumps(latched(alarms), ensure_ascii=False))
    return 1 if any(a["status"] == "fail" for a in alarms) else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
