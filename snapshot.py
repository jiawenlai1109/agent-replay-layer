"""Rebuild the workspace as it looked at step k, by replaying the trajectory.

Offline tap has a trap: the filesystem only holds the *final* state, so
re-evaluating a workspace promise at every recorded step returns the same verdict
each time and looks like step-level localization without being it. The state at
step k has to be reconstructed from the seed plus the writes up to k.

`write` and `edit` are replayable from the normalized trajectory (opencode puts
`content` / `filePath` + `oldString` / `newString` in the tool input). `bash` is
not: a shell command's effect on the tree cannot be read off its name. Those
steps are recorded as opacity boundaries, and anything that needed them is
reported as `na` rather than guessed.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import tap
from taskset import materialize


def _rel(path_str: str, workspace: Path) -> Path | None:
    """Resolve a tool-supplied path against the snapshot, refusing escapes."""
    p = Path(path_str)
    try:
        rel = p.relative_to(workspace)
    except ValueError:
        if p.is_absolute():
            parts = p.parts
            for i, part in enumerate(parts):
                if part == workspace.name and i + 1 < len(parts):
                    rel = Path(*parts[i + 1:])
                    break
            else:
                return None
        else:
            rel = p
    rel = Path(str(rel).lstrip("/"))
    if ".." in rel.parts:
        return None
    return rel or Path(".")


class Snapshot:
    """A workspace reconstructed step by step."""

    def __init__(self, task_id: str, workspace: Path, root: Path | None = None):
        self.task_id = task_id
        self.workspace = Path(workspace)
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="sbrsnap_"))
        self.dir = self.root / f"ws_{task_id}"
        materialize(task_id, self.dir)
        self.applied_write = 0
        self.touched: set[str] = set()
        self.applied_edit = 0
        self.opaque: list[dict[str, Any]] = []
        self.divergences: list[dict[str, Any]] = []

    def close(self) -> None:
        if self.root and self.root.exists() and str(self.root).startswith(tempfile.gettempdir()):
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def apply(self, row: dict[str, Any]) -> None:
        """Mutate the tree to reflect this step, then let the caller evaluate."""
        if row.get("kind") != "tool_use":
            return
        name = (row.get("name") or "").lower()
        inp = row.get("input") if isinstance(row.get("input"), dict) else {}
        target = inp.get("filePath") or inp.get("path")
        if name in ("write", "create_file") and target and isinstance(inp.get("content"), str):
            rel = _rel(str(target), self.workspace)
            if rel is None:
                self.divergences.append({"step": row.get("ordinal"), "why": "path_outside_workspace"})
                return
            dest = self.dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(inp["content"], encoding="utf-8")
            self.applied_write += 1
            self.touched.add(str(rel))
            return
        if name in ("edit", "str_replace", "multiedit") and target:
            rel = _rel(str(target), self.workspace)
            old = inp.get("oldString") or inp.get("old_string")
            new = inp.get("newString") or inp.get("new_string")
            if rel is None or not isinstance(old, str) or not isinstance(new, str):
                self.divergences.append({"step": row.get("ordinal"), "why": "unparsable_edit"})
                return
            src = self.dir / rel
            if not src.is_file():
                self.divergences.append({"step": row.get("ordinal"), "why": "file_not_in_snapshot"})
                return
            text = src.read_text(encoding="utf-8")
            if old not in text:
                self.divergences.append({"step": row.get("ordinal"), "why": "oldstring_not_found"})
                return
            src.write_text(text.replace(old, new, 1), encoding="utf-8")
            self.applied_edit += 1
            self.touched.add(str(rel))
            return
        if name == "bash":
            # A shell command can touch anything, so after one the "not yet
            # applicable" gate can no longer be trusted and opens up.
            self.touched.add("*")
            self.opaque.append({"step": row.get("ordinal"), "cmd": (row.get("input_excerpt") or "")[:120]})

    def is_touched(self, rel: str) -> bool:
        return "*" in self.touched or rel in self.touched

    @property
    def complete(self) -> bool:
        """True while nothing has happened that replay cannot reproduce."""
        return not self.opaque and not self.divergences
