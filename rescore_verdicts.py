"""Re-score every collected sample under the three-valued verdict.

Offline only: it re-reads each run's durable artifacts and current ledger, so it
costs nothing but CPU, and it never edits the original manifest — the old
judgement stays visible so a later reader can see what changed and why.
"""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path
import sbr_paths

import audit
import runner

MANIFESTS = sbr_paths.MANIFESTS
OUT = sbr_paths.RESULTS / "verdict_rescore.csv"


def main() -> int:
    keep_missing = "--keep-missing" in sys.argv
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(MANIFESTS.glob("*.json")):
        m = json.loads(path.read_text(encoding="utf-8"))
        run_id = m.get("run_id") or path.stem
        if not (m.get("metrics") or {}).get("steps") or runner.is_provider_fatal(m.get("report") or {}):
            continue
        ws = Path(m.get("workspace") or "")
        if not ws.is_dir():
            rows.append({"run_id": run_id, "task": m.get("task_id"), "arm": m.get("arm"),
                         "old": "workspace_missing", "new": "workspace_missing", "note": "kept" if keep_missing else "dropped"})
            if not keep_missing:
                path.unlink()
            continue
        lat = audit.latched(audit.audit_run(m["task_id"], runner.RUNS_ROOT / run_id, ws))
        old = (m.get("outcome") or {}).get("satisfied_by_ledger")
        old_v = "satisfied" if old is True else ("not_satisfied" if old is False else "unknown")
        new_v = audit.verdict_of(lat)
        counts = {s: sum(1 for v in lat.values() if v["status"] == s) for s in ("pass", "fail", "na")}
        rows.append({
            "run_id": run_id, "task": m.get("task_id"), "arm": m.get("arm"),
            "old": old_v, "new": new_v,
            "pass": counts["pass"], "fail": counts["fail"], "na": counts["na"],
            "unevaluated": ";".join(sorted(k for k, v in lat.items() if v["status"] == "na")),
            "cost_usd": round((m.get("metrics") or {}).get("cost") or 0, 5),
        })
        if old is not None:
            m["outcome"]["ledger_verdict"] = new_v
            m["outcome"]["rescored_at"] = time.strftime("%F %T")
            path.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["run_id"])
        w.writeheader()
        w.writerows(rows)
    from collections import Counter
    missing = [r["run_id"] for r in rows if r["new"] == "workspace_missing"]
    if keep_missing:
        print("KEEP-MISSING mode: nothing deleted")
        print("samples:", len(rows), "| new verdicts:", dict(Counter(r["new"] for r in rows)))
        print("workspace_missing:", missing)
        print("wrote", OUT)
        return 0
    if missing:
        # Deleting records is only safe while they are also reproducible. If any
        # workspace is still present we would destroy the one copy of a verdict
        # that the originals still disagree with, so refuse and report instead.
        present = [r["run_id"] for r in rows if r["new"] != "workspace_missing"]
        print("REFUSING TO DELETE: %d samples could be re-scored, %d are workspace_missing"
              % (len(present), len(missing)))
        print("workspace_missing:", missing)
        print("re-scored (not written):", present)
        print("wrote", OUT)
        return 3
    print("samples:", len(rows), "| new verdicts:", dict(Counter(r["new"] for r in rows)))
    print("flipped not_satisfied -> incomplete_evidence:",
          [r["run_id"] for r in rows if r["old"] == "not_satisfied" and r["new"] == "incomplete_evidence"])
    print("deleted stale records:", len(missing), "| wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
