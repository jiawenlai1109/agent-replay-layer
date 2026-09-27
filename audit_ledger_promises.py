"""Report where a commitment depends on surface form the task text never required.

A commitment may only key on:
  - paths, names and numbers the task text states, or
  - machine state the agent cannot phrase (ledger fields, exit codes, hashes).
Anything else is a self-made oracle: the agent can be right and still be flagged.

This tool does not judge — it lists the dependencies; each one is adjudicated by
hand and the verdict goes into the record.

usage: python3 audit_ledger_promises.py [task_id ...]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SBR = Path(__file__).resolve().parent
PATH_KEYS = ("path", "csv", "json", "file")
RE_KEYS = ("pattern", "measure_pattern")
NUM_KEYS = ("rows", "expect")

_TOKEN = re.compile(r"[A-Za-z0-9_./-]{4,}")


def _strings(value, key=""):
    """Yield (kind, key, text) for every surface token a commitment keys on."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, key)
    elif isinstance(value, str):
        if key in PATH_KEYS or key == "sha256":
            yield ("path", key, value)
        elif key in RE_KEYS or key == "patterns":
            yield ("regex", key, value)
        elif key == "cmd":
            yield ("cmd", key, value)


def dependencies(commitment: dict) -> list[tuple[str, str, str]]:
    deps: list[tuple[str, str, str]] = []
    args = commitment.get("args", {})
    for p in args.get("sha256") or {}:
        deps.append(("path", "guarded", p))
    for kind, key, text in _strings(args, ""):
        if kind == "path":
            deps.append(("path", key, text))
        elif kind == "regex":
            literals = [t for t in _TOKEN.findall(text) if t not in {"d", "s", "b", "0", "40", "1"}]
            for t in literals:
                deps.append(("regex-literal", key, t))
        elif kind == "cmd":
            deps.append(("cmd", key, text))
    for key, value in args.items():
        if key in NUM_KEYS and isinstance(value, int):
            deps.append(("number", key, str(value)))
    for p in args.get("overlay", []) + list(args.get("node_ids", [])):
        deps.append(("oracle-path" if p in args.get("overlay", []) else "node-id", "oracle", p.split("::")[0]))
    return deps


def main(argv: list[str]) -> int:
    ids = argv[1:] or sorted(p.name for p in (SBR / "ledger").iterdir() if p.is_dir())
    findings = 0
    for task_id in ids:
        ledger = SBR / "ledger" / task_id / "commitments.json"
        task_md = SBR / "tasks" / task_id / "task.md"
        if not ledger.is_file():
            print(f"== {task_id}: 无台账，跳过")
            continue
        text = task_md.read_text(encoding="utf-8") if task_md.is_file() else ""
        print(f"== {task_id}  (task.md {len(text)} 字符)")
        for c in json.loads(ledger.read_text(encoding="utf-8")):
            for kind, key, tok in dependencies(c):
                if kind == "node-id":
                    continue
                if kind == "oracle-path":
                    # The hidden judging files MUST be absent from the task text.
                    stated = tok not in text
                    flag = "  ok  " if stated else "!!LEAK!!"
                    if not stated:
                        findings += 1
                    print(f"   {flag} {c['id']:<26} {c['predicate']:<26} {kind:<14} {key:<16} {tok[:60]} (期望不出现在原文)")
                    continue
                if kind in {"path", "regex-literal", "number"} and key == "guarded":
                    print(
                        f"   NOTE   {c['id']:<26} {c['predicate']:<26} 守卫文件不要求原文提及，"
                        f"但'不得改测试'这条规范必须写进原文\n          -> {tok}"
                    )
                    continue
                stated = tok in text
                if kind == "cmd":
                    stated = tok.split()[-1] in text or "pytest" in text or "unittest" in text
                flag = "  ok  " if stated else "**MISS**"
                if not stated:
                    findings += 1
                print(f"   {flag} {c['id']:<26} {c['predicate']:<26} {kind:<14} {key:<16} {tok[:60]}")
    print(f"\n未声明依赖共 {findings} 处（需逐条人工裁定：改任务原文 / 改承诺 / 记为可接受）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
