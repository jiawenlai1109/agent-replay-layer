"""Task set and commitment ledger.

Layout is deliberate:

    tasks/<id>/task.md          what the agent sees (external task text only)
    tasks/<id>/seed/            initial workspace contents handed to the agent
    ledger/<id>/commitments.json   the machine-checkable promises (agent never sees)
    ledger/<id>/truth.json         injected-drift ground truth (never in the task text)

Commitments are the oracle for the detector, so any of their wording leaking
into task.md turns the experiment into the agent restating our checklist.
check_leaks() is the guard against that.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
import sbr_paths
from typing import Any

TASKS = sbr_paths.TASKS
LEDGER = sbr_paths.LEDGER


def task_text_path(task_id: str) -> Path:
    return TASKS / task_id / "task.md"


def materialize(task_id: str, workspace: Path) -> Path:
    """Fill a fresh workspace from the task's seed and return the task text path."""
    seed = TASKS / task_id / "seed"
    workspace = Path(workspace)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    if seed.is_dir():
        shutil.copytree(seed, workspace, dirs_exist_ok=True)
    return task_text_path(task_id)


def load_commitments(task_id: str) -> list[dict[str, Any]]:
    path = LEDGER / task_id / "commitments.json"
    items = json.loads(path.read_text(encoding="utf-8"))
    for c in items:
        for key in ("id", "predicate", "applies_to"):
            if key not in c:
                raise ValueError(f"{path}: commitment missing '{key}'")
    return items


def load_truth(task_id: str) -> dict[str, Any] | None:
    path = LEDGER / task_id / "truth.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


_TOKEN_RE = re.compile(r"[A-Za-z_]{6,}")


def check_leaks(task_id: str) -> dict[str, Any]:
    """Report commitment vocabulary that also appears in the agent-visible task text."""
    text = task_text_path(task_id).read_text(encoding="utf-8").lower()
    leaked: list[dict[str, str]] = []
    for c in load_commitments(task_id):
        probe = str(c.get("leak_probe", c["id"]))
        for token in _TOKEN_RE.findall(probe.lower()):
            if token in text:
                leaked.append({"commitment": c["id"], "token": token})
    return {"task_id": task_id, "clean": not leaked, "leaked": leaked}
