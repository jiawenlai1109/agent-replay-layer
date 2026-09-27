"""Batch runner. Implements the single-entry rule: every arm starts at POST /api/runs.

CLI-created runs have an empty owner record, so a later resume silently falls
back to agent=codex and the server's default workspace, then appends a 20ms
no-op round to the ledger. Never mix entry points.

The completion gate parks a live worker in waiting_approval each time the
manager declares done, so approvals are resolved in a loop, not once.

usage:
  python3 runner.py --task T-PIPE-01 --arm A --rep 1
  python3 runner.py --task T-PIPE-01 --arm B --rep 1 --resume-after 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import sbr_paths

RUNS_ROOT = sbr_paths.RUNS_ROOT
WS_ROOT = sbr_paths.WS_ROOT
MANIFESTS = sbr_paths.MANIFESTS
RESULTS = sbr_paths.RESULTS
API = sbr_paths.API
MODEL = "deepseek/deepseek-flash"
AGENT = "opencode"


def call(method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 60) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw.decode("utf-8", "replace")[:300]


def announce(manifest: dict[str, Any], code: int) -> None:
    """Emit the abort reason. Called on the failure path, so it is exercised by a
    test — the first version of this crashed with NameError (missing `import sys`)
    and the crash was mistaken for a normal rc=1 run."""
    if not code:
        return
    print(
        "BATCH_UNSAFE rc=%s: provider_fatal=%s timed_out=%s steps=%s"
        % (
            code,
            is_provider_fatal(manifest.get("report") or {}),
            manifest.get("timed_out"),
            manifest.get("metrics", {}).get("steps"),
        ),
        file=sys.stderr,
    )


def ping() -> None:
    status, _ = call("GET", "/api/runs", timeout=10)
    if status != 200:
        raise SystemExit(
            f"control API unreachable at {API} (got {status}); "
            "start it with: lh-harness web --runs-root ~/lh_runs --workspace-root ~/sbr_ws --port 8800 --no-open"
        )


def pending_approval(run_id: str) -> str | None:
    path = RUNS_ROOT / run_id / "lh_harness" / "role_orchestration" / "approvals.jsonl"
    if not path.is_file():
        return None
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    last = json.loads(lines[-1])
    if last.get("resolved") or last.get("status") in ("accepted", "resolved"):
        return None
    return str(last.get("approval_id") or "") or None


def drive_to_terminal(run_id: str, *, max_minutes: int, settle_seconds: int = 90) -> dict[str, Any]:
    """Poll until the worker exits, resolving each completion gate with 'stop'."""
    deadline = time.time() + max_minutes * 60
    last_alive = False
    while time.time() < deadline:
        status, snap = call("GET", f"/api/runs/{run_id}/status")
        if status != 200:
            time.sleep(5)
            continue
        alive = bool(snap.get("alive"))
        last_alive = alive
        if alive:
            aid = pending_approval(run_id)
            if aid:
                call("POST", f"/api/runs/{run_id}/approvals/{aid}/resolve",
                     {"action": "stop", "reason": "batch runner: gate reached, ending attempt"})
        else:
            return snap
        time.sleep(5)
    return {**(snap if isinstance(snap, dict) else {}), "_timeout": True}


EXIT_OK, EXIT_PROVIDER, EXIT_NO_STEPS, EXIT_TIMEOUT = 0, 3, 4, 5


def exit_code_for(manifest: dict[str, Any]) -> int:
    """Pure so the abort path can be tested without spending a single call.

    Measured twice today: a guard that crashes while detecting a failure looks
    exactly like a run that succeeded. The detection must be testable offline.
    """
    if manifest.get("timed_out"):
        return EXIT_TIMEOUT
    if is_provider_fatal(manifest.get("report") or {}):
        return EXIT_PROVIDER
    if not manifest.get("metrics", {}).get("steps"):
        return EXIT_NO_STEPS
    return EXIT_OK


def start_run(task_id: str, run_id: str, max_rounds: int) -> dict[str, Any]:
    import taskset

    workspace = WS_ROOT / f"ws_{run_id}"
    task_path = taskset.materialize(task_id, workspace)
    task_text = task_path.read_text(encoding="utf-8")
    status, out = call(
        "POST",
        "/api/runs",
        {
            "task": task_text,
            "agent": AGENT,
            "model": MODEL,
            "workspace": str(workspace),
            "run_id": run_id,
            "max_rounds": max_rounds,
            "prompt_language": "zh",
        },
    )
    if status != 200:
        raise SystemExit(f"create failed ({status}): {out}")
    return {"workspace": str(workspace), "created": out.get("run", {})}


def resume_run(run_id: str, extra_rounds: int) -> dict[str, Any]:
    status, out = call("POST", f"/api/runs/{run_id}/resume", {"mode": "continue", "extra_rounds": extra_rounds})
    return {"http": status, "ok": bool(isinstance(out, dict) and out.get("ok")), "detail": str(out)[:200]}


def collect(run_id: str) -> dict[str, Any]:
    """Metrics we can compute from durable artifacts, no LLM involved."""
    run_dir = RUNS_ROOT / run_id
    rounds = run_dir / "lh_harness" / "role_orchestration" / "rounds.jsonl"
    out: dict[str, Any] = {"rounds": 0, "steps": 0, "tokens": 0, "cache_read": 0, "cost": 0.0}
    if rounds.is_file():
        out["rounds"] = sum(1 for ln in rounds.read_text(encoding="utf-8").splitlines() if ln.strip())
    import tap

    rows = tap.index_run(run_dir)
    out["steps"] = len(rows)
    for f in run_dir.glob("lh_harness/role_orchestration/rounds/round_*/*_raw_trajectory.jsonl"):
        for rec in tap.step_tokens(f):
            t = rec["tokens"]
            out["tokens"] += t.get("total", 0) or 0
            out["cache_read"] += (rec.get("cache") or {}).get("read", 0) or 0
            out["cost"] += rec.get("cost", 0) or 0
    return out


def audit_with_workspace(run_id: str, task_id: str, workspace: Path) -> dict[str, Any]:
    import audit as audit_mod

    alarms = audit_mod.audit_run(task_id, RUNS_ROOT / run_id, workspace)
    return {"alarms": alarms, "latched": audit_mod.latched(alarms)}


def read_report(run_id: str) -> dict[str, Any]:
    """The harness's own claim about completion. Kept separate from our verdict."""
    for path in (RUNS_ROOT / run_id / "lh_harness" / "report.json", RUNS_ROOT / run_id / "report.json"):
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                "report_status": data.get("status"),
                "completion_satisfied": data.get("completion_satisfied"),
                "rounds_run": data.get("rounds_run"),
                "abort_reason": data.get("abort_reason"),
                "failure_reason": data.get("failure_reason"),
            }
    return {}


PROVIDER_FATAL = ("provider_provider_error", "insufficient balance", "402", "quota")


def is_provider_fatal(report: dict[str, Any]) -> bool:
    """A dead credential looks exactly like a finished run unless you check.

    Measured: with the key out of balance, every run ended in ~4s with
    status=failed and the batch harness still reported rc=0, producing nine
    empty samples that looked like results.
    """
    blob = " ".join(str(report.get(k) or "") for k in ("abort_reason", "failure_reason")).lower()
    return any(t in blob for t in PROVIDER_FATAL)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--arm", required=True, choices=("A", "B", "C1", "C2"))
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--minutes", type=int, default=25)
    ap.add_argument("--resume-after", type=int, default=1, help="arm B: extra rounds granted by resume")
    args = ap.parse_args()

    ping()
    MANIFESTS.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%H%M%S")
    run_id = f"{args.task}-{args.arm}{args.rep}-{stamp}"

    manifest: dict[str, Any] = {
        "task_id": args.task,
        "arm": args.arm,
        "rep": args.rep,
        "entry": "api",
        "model": MODEL,
        "agent": AGENT,
        "run_id": run_id,
        "started_at": time.strftime("%F %T %Z"),
        "attempts": [],
    }

    info = start_run(args.task, run_id, args.max_rounds)
    manifest["workspace"] = info["workspace"]
    snap = drive_to_terminal(run_id, max_minutes=args.minutes)
    manifest["timed_out"] = bool(snap.get("_timeout"))
    manifest["attempts"].append({"kind": "initial", "status": snap.get("status")})

    if args.arm == "B":
        status, after = call("GET", f"/api/runs/{run_id}/status")
        if after.get("alive"):
            raise SystemExit("arm B expects a terminal run before resume")
        rec = resume_run(run_id, args.resume_after)
        manifest["attempts"].append({"kind": "resume", **rec})
        if rec["ok"]:
            snap = drive_to_terminal(run_id, max_minutes=args.minutes)
            manifest["timed_out"] = manifest.get("timed_out") or bool(snap.get("_timeout"))
            manifest["attempts"].append({"kind": "post-resume", "status": snap.get("status")})

    status, final = call("GET", f"/api/runs/{run_id}/status")
    owner = final.get("owner") or {}
    manifest["final"] = {
        "status": final.get("status"),
        "alive": final.get("alive"),
        "owner_agent": owner.get("agent"),
        "owner_model": owner.get("model"),
        "owner_workspace": owner.get("workspace"),
        "resume_epoch": owner.get("resume_epoch"),
    }
    manifest["metrics"] = collect(run_id)
    manifest["report"] = read_report(run_id)
    alarms = audit_with_workspace(run_id, args.task, Path(info["workspace"]))
    manifest["latched"] = alarms["latched"]
    counts = {s: sum(1 for a in alarms["alarms"] if a["status"] == s) for s in ("pass", "fail", "na")}
    # Verdict is per commitment on its LATCHED state, not per check: a round that
    # was na early (only one round recorded) and pass later must count as pass.
    # The lifecycle status is never consulted — the gate policy ends runs with
    # action=stop, so even a perfect run reports status=cancelled.
    latched_counts = {s: sum(1 for v in alarms["latched"].values() if v["status"] == s) for s in ("pass", "fail", "na")}
    manifest["outcome"] = {
        "ledger_verdict": audit_mod.verdict_of(alarms["latched"]),
        "check_counts": counts,
        "latched_counts": latched_counts,
        "violated": sorted(k for k, v in alarms["latched"].items() if v["status"] == "fail"),
        "unevaluated": sorted(k for k, v in alarms["latched"].items() if v["status"] == "na"),
        "satisfied_by_ledger": all(v["status"] == "pass" for v in alarms["latched"].values()),
        "claimed_by_harness": manifest["report"].get("completion_satisfied"),
    }
    manifest["provider_fatal"] = is_provider_fatal(manifest["report"])
    code = exit_code_for(manifest)
    (MANIFESTS / f"{run_id}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTS / f"{run_id}.alarms.json").write_text(json.dumps(alarms["alarms"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    announce(manifest, code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
