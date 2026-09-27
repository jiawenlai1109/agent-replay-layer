"""Where everything lives. One file owns it so no module hardcodes a home directory.

Every value is an environment override with a default rooted at $HOME, which is
what the lab box uses. Nothing here creates directories: creating a run root
belongs to whoever starts a run, not to an import.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


HOME = _env("SBR_HOME", Path.home() / "sbr")

#: Harness run directories (`lh_runs/<run_id>/`), written by lh-harness itself.
RUNS_ROOT = _env("LH_RUNS_ROOT", Path.home() / "lh_runs")
#: One workspace per run, the tree the agent edits and the sensor reads.
WS_ROOT = _env("LH_WORKSPACE_ROOT", Path.home() / "sbr_ws")
#: One JSON record per dispatched run: parameters, ledger verdicts, timings.
MANIFESTS = HOME / "manifests"
#: Generated CSVs (pair_report, cost_to_success, verdict rescore).
RESULTS = HOME / "results"
#: Per-run backup of the artifact a breakpoint injects into, so restore is exact.
CSTATE = HOME / "cstate"
#: Task cards and the commitment ledger, shipped with the code rather than in $HOME.
REPO_ROOT = Path(__file__).resolve().parent
TASKS = REPO_ROOT / "tasks"
LEDGER = REPO_ROOT / "ledger"

#: Checkout roots the family-A tasks are built from (base tree + PR tree), and
#: the per-repo virtualenvs their predicates run in. Optional: the tests that need
#: them skip when they are absent.
REPOS = _env("SBR_REPOS", Path.home() / "tasks")
VENVS = _env("SBR_VENVS", Path.home() / "venvs")

#: Control-plane API of `lh-harness web`, which binds loopback only.
API = os.environ.get("LH_API", "http://127.0.0.1:8800")
