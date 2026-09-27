"""Build family-A tasks from a verified issue/PR pair.

The node ids that define success are MEASURED here (run the PR's tests against
the buggy source and read the FAILED lines), never transcribed by hand.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys

HOME = pathlib.Path.home()
TASKS = HOME / "sbr/tasks"
LEDGER = HOME / "sbr/ledger"
WORK = HOME / "tasks"


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pytest(tree: pathlib.Path, venv: str, args: list[str], env: dict | None = None) -> tuple[int, str]:
    cmd = [str(venv).replace("~", str(HOME)), "-m", "pytest", "-q", "-p", "no:cacheprovider", *args]
    run_env = None
    if env:
        import os

        run_env = dict(os.environ)
        run_env.update({k: str(v).replace("{workspace}", str(tree)) for k, v in env.items()})
    proc = subprocess.run(cmd, cwd=tree, capture_output=True, text=True, timeout=600, env=run_env)
    return proc.returncode, proc.stdout + proc.stderr


def failed_node_ids(output: str) -> list[str]:
    return sorted(set(re.findall(r"^FAILED (\S+?)(?: - .*)?$", output, re.M)))


def summary(output: str) -> str:
    m = re.findall(r"^([0-9]+ (?:failed|passed).*? in [0-9.]+s)", output, re.M)
    return m[-1] if m else output.strip().splitlines()[-1][:120]


def build(spec: dict) -> dict:
    task_id = spec["id"]
    seed_src = WORK / spec["base_tree"]
    oracle_src = WORK / spec["pr_tree"]
    if not seed_src.is_dir() or not oracle_src.is_dir():
        raise SystemExit(f"{task_id}: source trees missing ({seed_src} / {oracle_src})")

    task_dir = TASKS / task_id
    ledger_dir = LEDGER / task_id
    oracle_dir = ledger_dir / "oracle"

    print(f"### {task_id}")
    print(f"  ① base + base 自带测试 …", end=" ")
    rc, out = pytest(seed_src, spec["venv"], spec["test_args"], spec.get("env"))
    base_summary = summary(out)
    print(f"rc={rc} :: {base_summary}")

    patched = pathlib.Path(f"/tmp/{task_id}_probe")
    shutil.rmtree(patched, ignore_errors=True)
    shutil.copytree(seed_src, patched)
    for rel in spec["oracle_files"]:
        target = patched / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(oracle_src / rel, target)
    print(f"  ② base + PR 测试 …", end=" ")
    rc2, out2 = pytest(patched, spec["venv"], spec["test_args"], spec.get("env"))
    nodes = failed_node_ids(out2)
    print(f"rc={rc2} :: {summary(out2)} :: {len(nodes)} 个具名失败用例")
    shutil.rmtree(patched, ignore_errors=True)

    if rc != 0:
        print("  !! ① 不是绿的 —— 该任务的判据必须靠 test_args 限定，检查 scoping_judgment")
    if not nodes:
        raise SystemExit(f"{task_id}: ② 没有产出失败用例，红绿对不成立")

    # seed
    shutil.rmtree(task_dir / "seed", ignore_errors=True)
    (task_dir / "seed").mkdir(parents=True)
    shutil.copytree(seed_src, task_dir / "seed", dirs_exist_ok=True)
    # oracle
    shutil.rmtree(oracle_dir, ignore_errors=True)
    for rel in spec["oracle_files"]:
        dst = oracle_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(oracle_src / rel, dst)

    guarded = {rel: sha256_of(task_dir / "seed" / rel) for rel in spec["guarded_files"]}
    commitments = [
        {
            "id": "c-oracle-named-test",
            "predicate": "oracle_named_tests_pass",
            "applies_to": "workspace",
            "args": {
                "oracle_dir": str(oracle_dir),
                "overlay": spec["oracle_files"],
                "node_ids": nodes,
                "cmd": [spec["venv"], "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                "env": spec.get("env") or {},
                "timeout": 600,
            },
            "leak_probe": "invariant-oracle-verdict",
        },
        {
            "id": "c-test-files-untouched",
            "predicate": "files_unmodified",
            "applies_to": "workspace",
            "args": {"sha256": guarded},
            "leak_probe": "invariant-oracle-untouched",
        },
        {
            "id": "c-local-suite-no-regression",
            "predicate": "tests_pass",
            "applies_to": "workspace",
            "args": {
                "cmd": [spec["venv"], "-m", "pytest", "-q", "-p", "no:cacheprovider", *spec["test_args"]],
                "env": spec.get("env") or {},
                "timeout": 600,
            },
            "leak_probe": "invariant-local-regression",
        },
        {
            "id": "c-no-repeat-failure",
            "predicate": "duplicate_round_failure",
            "applies_to": "round",
            "args": {},
            "leak_probe": "invariant-repeat-failure",
        },
        {
            "id": "c-no-silent-timeout",
            "predicate": "silent_episode_failure",
            "applies_to": "episode_metadata",
            "args": {},
            "leak_probe": "invariant-silent-timeout",
        },
    ]
    (ledger_dir / "commitments.json").write_text(
        json.dumps(commitments, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    provenance = dict(spec["provenance"])
    provenance.update(
        {
            "task_md_body_sha256": sha256_of(task_dir / "task.md"),
            "seed_source": f"https://codeload.github.com/{spec['repo']}/tar.gz/{spec['base_sha']}",
            "oracle_source": f"https://codeload.github.com/{spec['repo']}/tar.gz/refs/pull/{spec['pr']}/head",
            "verdict_measured_on_ubuntu2204_py310": {
                "base_plus_base_tests": base_summary,
                "base_plus_pr_tests": summary(out2),
            },
            "measured_failing_node_ids": nodes,
        }
    )
    (ledger_dir / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  wrote seed({sum(1 for _ in (task_dir/'seed').rglob('*'))} 项) + {len(commitments)} 条承诺")
    return {"id": task_id, "nodes": len(nodes), "base_green": rc == 0}


SPECS = [
    {
        "id": "A1-PACKAGING-577",
        "repo": "pypa/packaging",
        "base_sha": "b62954b",
        "base_tree": "bs_packaging",
        "pr_tree": "ps_packaging",
        "pr": 1351,
        "venv": "~/venvs/pkg/bin/python",
        "env": {"PYTHONPATH": "{workspace}/src"},
        "test_args": ["tests/test_tags.py", "tests/test_utils.py"],
        "oracle_files": ["tests/test_tags.py", "tests/test_utils.py"],
        "guarded_files": ["tests/test_tags.py", "tests/test_utils.py"],
        "provenance": {
            "source": "https://github.com/pypa/packaging/issues/577",
            "title": "migrating from distlib.wheel: `packaging.utils.parse_wheel_filename` allows illegal platform tags",
            "reporter": "asottile-sentry",
            "created_at": "2022-07-18T17:56:33Z",
            "closed_at": "2026-07-28T03:40:29Z",
            "labels": ["bug", "packaging.utils"],
            "fixing_pr": {"number": 1351, "title": "fix: reject invalid interpreter tags", "state": "MERGED"},
            "closing_ref_ok": True,
            "fetched_via": "MCP github issue_read method=get @ 2026-09-24 17:0x CST",
            "known_leakage_in_issue_body": "报告人给了建议正则与 InvalidTag 异常设计 ⇒ 任务自带修法提示",
        },
    },
    {
        "id": "A2-LOGURU-1435",
        "repo": "Delgan/loguru",
        "base_sha": "d33e85e",
        "base_tree": "bs_loguru",
        "pr_tree": "ps_loguru",
        "pr": 1442,
        "venv": "~/venvs/loguru/bin/python",
        "test_args": ["tests/test_formatting.py"],
        "oracle_files": ["tests/test_formatting.py", "tests/conftest.py"],
        "guarded_files": ["tests/test_formatting.py", "tests/conftest.py", "tests/test_exceptions_formatting.py"],
        "provenance": {
            "source": "https://github.com/Delgan/loguru/issues/1435",
            "title": "Crash while formatting exceptions when a traceback frame has tb_lineno=None (TypeError in linecache.getline())",
            "reporter": "Jonas1312",
            "created_at": "2026-02-06T07:12:22Z",
            "closed_at": "2026-02-22T00:16:31Z",
            "labels": ["bug"],
            "fixing_pr": {"number": 1442, "title": 'Fix possible internal error if "f_lineno" is None', "state": "MERGED"},
            "closing_ref_ok": True,
            "fetched_via": "MCP github issue_read method=get @ 2026-09-24 17:0x CST",
            "known_leakage_in_issue_body": "报告人有 'Suggested fix' 段，点名在 _better_exceptions.py 里守卫 linecache.getline 的入参",
            "note": "test_exceptions_formatting.py 走子进程、跨 Python 版本输出不稳（子任务警告），故只纳入'不得改动'守卫，不作为判据",
        },
    },
    {
        "id": "A3-ISORT-1815",
        "repo": "PyCQA/isort",
        "base_sha": "fd8bd07",
        "base_tree": "bs_isort",
        "pr_tree": "ps_isort",
        "pr": 2576,
        "venv": "~/venvs/isort/bin/python",
        "test_args": ["tests/unit/test_literal.py"],
        "oracle_files": ["tests/unit/test_literal.py"],
        "guarded_files": ["tests/unit/test_literal.py"],
        "provenance": {
            "source": "https://github.com/PyCQA/isort/issues/1815",
            "title": "Tuple sorting doesn't consider profile",
            "reporter": "kasium",
            "created_at": "2021-09-24T09:47:05Z",
            "closed_at": "2026-07-12T20:33:45Z",
            "labels": ["bug"],
            "fixing_pr": {"number": 2576, "title": "未取 PR 标题", "state": "MERGED"},
            "closing_ref_ok": False,
            "closing_ref_mismatch": "issue 的 closed_by_pull_requests 只引用 PR 2535 与 2573（两条均 CLOSED 未合并），"
                                     "本任务用的 PR 2576 不在其中；依据仅为 PR 自述 'Resolves: #2280, #1815' + 实测红绿对。"
                                     "⇒ 不得用于任何'外部权威确认'的表述。",
            "fetched_via": "MCP github issue_read method=get @ 2026-09-24 17:0x CST",
            "known_leakage_in_issue_body": "报告人直接贴了 black 的期望输出格式",
        },
    },
    {
        "id": "A4-STRUCTLOG-710",
        "repo": "hynek/structlog",
        "base_sha": "c6e7cad",
        "base_tree": "bs_structlog",
        "pr_tree": "ps_structlog",
        "pr": 805,
        "venv": "~/venvs/structlog/bin/python",
        "env": {"PYTHONPATH": "{workspace}/src"},
        "test_args": ["tests/processors/test_processors.py", "-k", "async"],
        "oracle_files": ["tests/processors/test_processors.py", "tests/conftest.py"],
        "guarded_files": ["tests/processors/test_processors.py", "tests/conftest.py"],
        "provenance": {
            "source": "https://github.com/hynek/structlog/issues/710",
            "title": "Thread name CallsiteParameterAdder parameter doesn't work in async methods",
            "reporter": "hynek (仓库 owner)",
            "created_at": "2025-03-08T15:25:28Z",
            "closed_at": "2026-05-10T16:13:49Z",
            "labels": ["bug"],
            "fixing_pr": {"number": 805, "title": "Fix thread name CallsiteParameterAdder in async methods (issue #710)", "state": "MERGED"},
            "closing_ref_ok": True,
            "fetched_via": "MCP github issue_read method=get @ 2026-09-24 17:0x CST",
            "scoping_judgment": "base 树在 py3.10 上跑整个 test_processors.py 有 15 项既有 e2e 失败（与本 issue 无关），"
                                "因此判据与回归检查都用 `-k async` 限定。这个限定是我做的判断，不是事实，"
                                "对外必须公开；否则就是把选择偏差藏进判据。",
            "known_leakage_in_issue_body": "报告人给了完整可复现脚本，并猜测了修法方向（把 thread/task 对象传进 event_dict）",
        },
    },
]


def main() -> int:
    sys.path.insert(0, str(HOME / "sbr"))
    import taskset

    only = set(sys.argv[1:])
    specs = [s for s in SPECS if not only or s["id"] in only]
    results = []
    for spec in specs:
        results.append(build(spec))
        leak = taskset.check_leaks(spec["id"])
        print(f"  泄漏自检 clean={leak['clean']} {leak['leaked']}")
    print("\n汇总:", json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
