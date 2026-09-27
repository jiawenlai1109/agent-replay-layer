"""Deterministic step/round predicates. No LLM, no network, stdlib only.

Every predicate returns the same four-symbol verdict used in DESIGN §4:
  pass  the checked invariant holds
  fail  the checked invariant is violated  -> an Alarm is raised
  na    the predicate does not apply yet at this position (NOT a pass)
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import sbr_paths
from typing import Any

VERDICTS = ("pass", "fail", "na")

def _repo_relative(value: str) -> Path:
    """Ledger paths are stored relative to this repo so a clone elsewhere resolves.

    Absolute values still work, which keeps the lab box's existing ledger usable.
    """
    p = Path(value).expanduser()
    return p if p.is_absolute() else sbr_paths.REPO_ROOT / p



def fingerprint(record: dict[str, Any]) -> str:
    """Stable hash of everything a round records except timing noise."""
    payload = {
        k: record.get(k)
        for k in ("plan_text", "next_step", "harness_feedback", "task_state", "task_contract")
    }
    payload["manager_status"] = _strip_timing(record.get("manager_status") or {})
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _strip_timing(status: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in status.items() if k not in ("duration_ms",)}


def read_rounds(rounds_jsonl: Path) -> list[dict[str, Any]]:
    rounds = []
    for line in Path(rounds_jsonl).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # rounds.jsonl is append-only; a torn tail must not hide earlier evidence.
            continue
        if isinstance(record, dict):
            rounds.append(record)
    return rounds


def duplicate_round_failure(args: dict[str, Any]) -> dict[str, Any]:
    """Fire when a round records the same non-success outcome as its predecessor.

    Upstream keeps the round ledger append-only and re-enters the loop with no
    comparison between adjacent rounds, so an identical failure is charged to
    the budget again. This predicate is the cheapest possible detector: one
    hash comparison per adjacent pair.
    """
    rounds = args["rounds"]
    at = int(args.get("at", len(rounds)))
    window = rounds[:at]
    cmd_line = f"duplicate_round_failure(rounds[:{at}])"

    if len(window) < 2:
        return _verdict("na", ">=2 rounds", f"{len(window)} rounds", cmd_line)

    prev, cur = window[-2], window[-1]
    if _is_success(cur):
        return _verdict("pass", "no repeated failure", "round succeeded", cmd_line)

    same = fingerprint(prev) == fingerprint(cur)
    return _verdict(
        "fail" if same else "pass",
        "adjacent rounds differ",
        "identical non-success round" if same else "rounds differ",
        cmd_line,
        expected_ref=fingerprint(prev)[:12],
        actual_ref=fingerprint(cur)[:12],
    )


def _is_success(record: dict[str, Any]) -> bool:
    status = record.get("manager_status") or {}
    return status.get("status") == "done" and record.get("next_step") != "invalid"


def _verdict(
    status: str,
    expected: str,
    actual: str,
    cmd_line: str,
    *,
    expected_ref: str = "",
    actual_ref: str = "",
) -> dict[str, Any]:
    if status not in VERDICTS:
        raise ValueError(f"unknown verdict {status!r}")
    out: dict[str, Any] = {"status": status, "expected": expected, "actual": actual, "cmd_line": cmd_line}
    if expected_ref:
        out["expected_ref"] = expected_ref
    if actual_ref:
        out["actual_ref"] = actual_ref
    return out


def silent_episode_failure(args: dict[str, Any]) -> dict[str, Any]:
    """Fire when an episode ended badly but produced no hard runtime signal.

    Measured on smoke01: a manager episode that hit its 900s/300s budget recorded
    ``runtime_signals: []`` and ``actions_log_diagnostics_only: true`` — the only
    evidence was a 28-character stderr tail. Upstream classifies failures from
    aggregated log text, so a timeout is a state field rather than a signal, and
    nothing in the loop reads it as one.
    """
    meta = args["metadata"]
    status = str(meta.get("status") or "")
    cmd_line = f"silent_episode_failure(status={status or 'unknown'})"

    if status == "done":
        return _verdict("pass", "no silent failure", "episode done", cmd_line)
    if not status:
        return _verdict("na", "episode metadata has a status", "status missing", cmd_line)

    signals = meta.get("runtime_signals")
    if signals is None:
        inner = meta.get("metadata") or {}
        signals = inner.get("runtime_signals")
    if signals is None:
        return _verdict("na", "runtime_signals present", "field absent", cmd_line)

    loud = bool(signals)
    return _verdict(
        "pass" if loud else "fail",
        ">=1 hard signal for a failed episode",
        f"{len(signals)} signal(s)" if loud else "0 signals, diagnostics-only log",
        cmd_line,
    )


def artifact_exists(args: dict[str, Any]) -> dict[str, Any]:
    ws, rel = Path(args["workspace"]), args["path"]
    found = (ws / rel).is_file()
    return _verdict("pass" if found else "fail", rel, "present" if found else "absent", f"test -f {ws}/{rel}")


def csv_row_count(args: dict[str, Any]) -> dict[str, Any]:
    ws, rel, want = Path(args["workspace"]), args["path"], int(args["rows"])
    path = ws / rel
    cmd_line = f"csv_row_count({rel}) == {want}"
    if not path.is_file():
        return _verdict("na", f"{rel} exists", "file absent", cmd_line)
    with path.open(encoding="utf-8", newline="") as fh:
        got = sum(1 for line in fh if line.strip()) - 1  # minus header
    return _verdict("pass" if got == want else "fail", f"{want} data rows", f"{got} data rows", cmd_line)


def json_matches_csv_aggregate(args: dict[str, Any]) -> dict[str, Any]:
    """The derived artifact must equal a fresh recomputation from its source.

    This is the invariant a long pipeline breaks first: the report drifts away
    from the data it was generated from (stale file, partial rewrite, wrong
    rounding), and nothing in the loop re-derives it.
    """
    ws = Path(args["workspace"])
    csv_path, json_path = ws / args["csv"], ws / args["json"]
    group_col, value_col = args["group_by"], args["value"]
    cmd_line = f"recompute({args['csv']} grouped by {group_col}) == {args['json']}"
    if not csv_path.is_file() or not json_path.is_file():
        return _verdict("na", "both source and derived exist", "missing input", cmd_line)

    import csv as _csv

    want: dict[str, dict[str, float]] = {}
    with csv_path.open(encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            key = row[group_col]
            slot = want.setdefault(key, {"count": 0, "sum": 0.0})
            slot["count"] += 1
            slot["sum"] += float(row[value_col])
    for slot in want.values():
        slot["sum"] = round(slot["sum"], 2)

    try:
        got = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _verdict("fail", "valid JSON", f"unparseable: {exc.msg}", cmd_line)

    ok = isinstance(got, dict) and set(got) == set(want) and all(
        got.get(k, {}).get("count") == want[k]["count"] and abs(got.get(k, {}).get("sum", 1e9) - want[k]["sum"]) < 1e-6
        for k in want
    )
    return _verdict(
        "pass" if ok else "fail",
        json.dumps(want, sort_keys=True),
        json.dumps(got, sort_keys=True)[:400],
        cmd_line,
    )


def tests_pass(args: dict[str, Any]) -> dict[str, Any]:
    ws = Path(args["workspace"])
    cmd = _expand_cmd(args.get("cmd") or ["python3", "-m", "unittest", "discover", "-s", "tests"])
    import subprocess

    cmd_line = " ".join(cmd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=int(args.get("timeout", 120)),
            env=_merge_env(None, _expand_env(args, ws)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _verdict("fail", "exit 0", f"could not run: {exc}", cmd_line)
    return _verdict(
        "pass" if proc.returncode == 0 else "fail",
        "exit 0",
        f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-200:]}",
        cmd_line,
    )


def claim_matches_measurement(args: dict[str, Any]) -> dict[str, Any]:
    """A mandated machine-readable claim must match what we measure.

    The claim is only ever read from `patterns`, in order. If none match, the
    verdict is na rather than a guess: a status document legitimately contains
    many numbers, so picking one by position makes the verdict a coin flip.
    Expectation comes from a measurement command so the ledger never hardcodes a
    number the task did not require.
    """
    import subprocess

    ws = Path(args["workspace"])
    path = ws / args["file"]
    patterns = args.get("patterns") or [args["pattern"]]
    cmd_line = f"claim_matches_measurement({args['file']}, {len(patterns)} pattern(s))"
    if not path.is_file():
        return _verdict("na", f"{args['file']} exists", "file absent", cmd_line)

    hits: list[str] = []
    used = None
    for pat in patterns:
        hits = re.findall(pat, path.read_text(encoding="utf-8"))
        if hits:
            used = pat
            break
    if not used:
        return _verdict("na", f"a line matching {patterns[0]}", "no machine-readable claim", cmd_line)
    if len(set(hits)) > 1:
        return _verdict("fail", "exactly one claimed value", f"ambiguous: {sorted(set(hits))}", cmd_line)
    claimed = int(hits[0])

    if "measure_cmd" not in args:
        want = int(args["expect"])
        cmd_line += f" == {want}"
    else:
        mcmd = _expand_cmd(args["measure_cmd"])
        cmd_line += f" vs `{' '.join(mcmd)}`"
        try:
            proc = subprocess.run(
                mcmd, cwd=ws, capture_output=True, text=True,
                timeout=int(args.get("timeout", 120)), env=_merge_env(None, _expand_env(args, ws)),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _verdict("na", "measurement runs", f"cannot measure: {exc}", cmd_line)
        measured = re.findall(args["measure_pattern"], proc.stdout + proc.stderr)
        if not measured:
            return _verdict("na", f"measurement matches {args['measure_pattern']}", "no measurement", cmd_line)
        want = int(measured[0])

    return _verdict("pass" if claimed == want else "fail", str(want), str(claimed), cmd_line)


def _expand_cmd(cmd: list[str]) -> list[str]:
    """Expand a leading/embedded ~ in command tokens.

    subprocess does no shell expansion, so a ledger written with `~/venvs/...`
    would otherwise fail with ENOENT and be reported as a violated commitment —
    a false positive caused by the ledger's own spelling.
    """
    home = str(Path.home())
    return [str(c).replace("~", home, 1) if str(c).startswith("~") else str(c) for c in cmd]


def _expand_env(args: dict[str, Any], ws: Path) -> dict[str, str] | None:
    """{workspace} is substituted so a ledger entry can pin imports to the audited copy.

    Matters for src/ layout repos: without it, `import pkg` resolves to whatever
    the venv installed earlier, so the agent's edits and the oracle's verdict
    would both be measured against the wrong code.
    """
    env = args.get("env")
    if not env:
        return None
    return {k: str(v).replace("{workspace}", str(ws)) for k, v in env.items()}


def _merge_env(base: dict[str, str] | None, extra: dict[str, str] | None) -> dict[str, str] | None:
    if not extra:
        return base
    merged = dict(base if base is not None else __import__("os").environ)
    merged.update(extra)
    return merged


def csv_unique_key(args: dict[str, Any]) -> dict[str, Any]:
    """A declared key column must have no duplicates.

    Added because the family-C review found the ledger could not see a duplicated
    order_id at all: row count and per-group sums both stay correct.
    """
    import csv as _csv

    ws = Path(args["workspace"])
    path = ws / args["path"]
    column = args["column"]
    cmd_line = f"csv_unique_key({args['path']}, {column})"
    if not path.is_file():
        return _verdict("na", f"{args['path']} exists", "file absent", cmd_line)
    with path.open(encoding="utf-8", newline="") as fh:
        values = [row[column] for row in _csv.DictReader(fh)]
    dupes = sorted({v for v in values if values.count(v) > 1})
    return _verdict(
        "pass" if not dupes else "fail",
        f"{len(values)} distinct {column} values",
        f"duplicated: {dupes}" if dupes else "no duplicates",
        cmd_line,
    )


def files_unmodified(args: dict[str, Any]) -> dict[str, Any]:
    """Guard the oracle: the files that define success must be untouched.

    Without this, an agent (or a recovery that replays a bad write) can pass a
    commitment by editing the tests instead of the source.
    """
    ws = Path(args["workspace"])
    expected = args["sha256"]
    changed = []
    for rel, want in expected.items():
        path = ws / rel
        if not path.is_file():
            changed.append(f"{rel}: missing")
            continue
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != want:
            changed.append(f"{rel}: {got[:12]} != {want[:12]}")
    cmd_line = f"sha256sum {' '.join(expected)}"
    return _verdict(
        "pass" if not changed else "fail",
        f"{len(expected)} oracle file(s) unchanged",
        "; ".join(changed) if changed else "all unchanged",
        cmd_line,
    )


def oracle_named_tests_pass(args: dict[str, Any]) -> dict[str, Any]:
    """Apply the hidden judging tests to a COPY of the workspace and run them.

    SWE-bench style on purpose: the agent never sees these files, so it cannot
    code to the test. The workspace is copied, never mutated, because the
    artifacts under audit are evidence and must stay byte-identical.
    """
    import shutil
    import subprocess
    import tempfile

    ws = Path(args["workspace"])
    oracle = _repo_relative(args["oracle_dir"])
    overlay = args["overlay"]
    node_ids = args["node_ids"]
    cmd = _expand_cmd(args["cmd"])
    full = cmd + list(node_ids)
    cmd_line = " ".join(full)

    for rel in overlay:
        if not (oracle / rel).is_file():
            return _verdict("na", f"oracle file {rel} present", "oracle file missing", cmd_line)

    with tempfile.TemporaryDirectory(prefix="sbr_oracle_") as tmp:
        dst = Path(tmp) / "ws"
        shutil.copytree(ws, dst, symlinks=True)
        for rel in overlay:
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(oracle / rel, target)
        try:
            proc = subprocess.run(
                full,
                cwd=dst,
                capture_output=True,
                text=True,
                timeout=int(args.get("timeout", 300)),
                env=_merge_env(None, _expand_env(args, dst)),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _verdict("fail", "oracle run completes", f"cannot run: {exc}", cmd_line)
        tail = (proc.stdout or proc.stderr).strip().splitlines()[-3:]
        return _verdict(
            "pass" if proc.returncode == 0 else "fail",
            f"{len(node_ids)} named oracle test(s) pass",
            f"exit {proc.returncode}: {' / '.join(tail)}"[:300],
            cmd_line,
        )


def differential_no_new_failures(args: dict[str, Any]) -> dict[str, Any]:
    """Whole-suite regression, judged only against a repeatedly-measured baseline.

    Measured on hynek/structlog under python3.10: the same test file reports a
    different failing set on every run (18 red per run, but 28 distinct across
    three runs). So 'zero failures' is unsatisfiable and a single-run diff
    invents regressions. This predicate repeats the run `repeats` times, keeps
    only the intersection (what is reliably red), and compares it against the
    baseline's *union* — the most generous reading of "the source already
    failed this". Anything red in the candidate that the baseline was never red
    in is a real, reproducible new failure.
    """
    import shutil
    import subprocess
    import tempfile

    ws = Path(args["workspace"])
    oracle = _repo_relative(args["oracle_dir"])
    files = args["files"]
    full = _expand_cmd(args["cmd"]) + list(files)
    repeats = int(args.get("repeats", 3))
    cmd_line = f"{len(files)} file(s) x{repeats} vs baseline(stable={len(args['baseline_stable'])},union={len(args['baseline_union'])})"

    env = _merge_env(None, _expand_env(args, ws))
    per_run: list[set[str]] = []
    for _ in range(repeats):
        with tempfile.TemporaryDirectory(prefix="sbr_diff_") as tmp:
            dst = Path(tmp) / "ws"
            shutil.copytree(ws, dst)
            for rel in args.get("overlay", []):
                target = dst / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(oracle / rel, target)
            run_env = _merge_env(None, {k: str(v).replace("{workspace}", str(dst)) for k, v in (args.get("env") or {}).items()})
            try:
                proc = subprocess.run(full, cwd=dst, capture_output=True, text=True,
                                      timeout=int(args.get("timeout", 600)), env=run_env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return _verdict("na", "suite runs to completion", f"cannot run: {exc}", cmd_line)
            out = proc.stdout + proc.stderr
            per_run.append({l.split(" ")[1].split(" - ")[0] for l in out.splitlines() if l.startswith("FAILED")})

    stable_red = set.intersection(*per_run) if per_run else set()
    new_failures = sorted(stable_red - set(args["baseline_union"]))
    flaky = len(set.union(*per_run) - stable_red) if per_run else 0
    return _verdict(
        "pass" if not new_failures else "fail",
        "no failure the repeated baseline was ever free of",
        f"stable_red={len(stable_red)} flaky={flaky} new={new_failures[:4]}" if new_failures
        else f"stable_red={len(stable_red)} flaky={flaky} new=none",
        cmd_line,
    )


REGISTRY = {
    "duplicate_round_failure": duplicate_round_failure,
    "silent_episode_failure": silent_episode_failure,
    "artifact_exists": artifact_exists,
    "csv_row_count": csv_row_count,
    "csv_unique_key": csv_unique_key,
    "json_matches_csv_aggregate": json_matches_csv_aggregate,
    "tests_pass": tests_pass,
    "claim_matches_measurement": claim_matches_measurement,
    "files_unmodified": files_unmodified,
    "oracle_named_tests_pass": oracle_named_tests_pass,
    "differential_no_new_failures": differential_no_new_failures,
}
