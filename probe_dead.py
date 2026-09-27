"""List manifests with their fatal/empty flags, so a throwaway run can be picked
for plumbing tests without touching a citable sample.

Arm B's resume path appends a round to rounds.jsonl, which is exactly what would
dirty an A-arm sample we have already put in a table. Only runs already excluded
as samples are safe targets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
import sbr_paths

MANIFESTS = sbr_paths.MANIFESTS
RUNS_ROOT = sbr_paths.RUNS_ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner  # noqa: E402


def main() -> int:
    only_dead = "--dead-only" in sys.argv
    rows = []
    for path in sorted(MANIFESTS.glob("*.json")):
        m = json.loads(path.read_text(encoding="utf-8"))
        run_id = m.get("run_id") or path.stem
        steps = (m.get("metrics") or {}).get("steps")
        fatal = runner.is_provider_fatal(m.get("report") or {})
        dead = fatal or not steps
        if only_dead and not dead:
            continue
        rows.append(
            {
                "run_id": run_id,
                "arm": m.get("arm"),
                "task": m.get("task_id"),
                "steps": steps,
                "rounds": (m.get("metrics") or {}).get("rounds"),
                "fatal": fatal,
                "dead": dead,
                "report_status": (m.get("report") or {}).get("report_status"),
                "resume_attempts": [a for a in m.get("attempts", []) if a.get("kind") != "initial"],
                "on_disk": (RUNS_ROOT / run_id).is_dir(),
            }
        )
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    print("# %d manifests, %d dead" % (len(rows), sum(1 for r in rows if r["dead"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
