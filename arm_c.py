"""Arms C-roll and C-rec: break the workspace mid-run, then recover from the
step the sensor says it broke — with and without a directive.

Why this file exists separately from runner.py: an in-flight batch re-invokes
runner.py per task, so editing it mid-batch would spend money on a half-tested
dispatch. It also does not need the snapshot replay from recover.py: while a run
is live the workspace *is* the current state, so the sensor can read it directly.
Replay was only ever needed to reconstruct state after the fact.

The three quantities this arm must keep apart:
  C-rec - C-roll  = what the directive alone contributes
  C-roll - B      = what rewinding to a step (vs a round boundary) contributes
Usage:
  python3 arm_c.py --task T-PIPE-01F-C1-csv-short --recipe C1-csv-short \
      --inject-after 6 --arm C-REC --rep 1
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import audit
import inject
import predicates
import recover
import runner
import tap
import sbr_paths
from taskset import load_commitments

CSTATE = sbr_paths.CSTATE
EXIT_NO_DETECT, EXIT_PROVIDER_DEAD, EXIT_CRASHED, EXIT_NOT_INJECTABLE = 6, 7, 8, 9
POLL_SECONDS = 5


def sensor_tick(commitments: list[dict[str, Any]], workspace: Path) -> dict[str, dict[str, str]]:
    """One in-line reading. While the run is live the workspace *is* the current
    state, so no replay is needed — expected/actual are kept because the
    directive renders them."""
    out: dict[str, dict[str, str]] = {}
    for c in commitments:
        try:
            v = predicates.REGISTRY[c["predicate"]]({**c.get("args", {}), "workspace": str(workspace)})
            out[c["id"]] = {"status": v["status"], "expected": str(v.get("expected", ""))[:120],
                            "actual": str(v.get("actual", ""))[:120]}
        except Exception as exc:
            out[c["id"]] = {"status": "fail", "expected": "sensor runs",
                            "actual": f"sensor_error:{type(exc).__name__}:{exc}"}
    return out


class Breakpoint:
    """Inject one recipe, keep the original bytes so the rollback is exact."""

    def __init__(self, recipe_id: str, workspace: Path, run_id: str):
        self.recipe_id = recipe_id
        self.workspace = Path(workspace)
        self.dir = CSTATE / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest: dict[str, Any] = {"recipe": recipe_id, "files": {}}

    def apply(self) -> dict[str, Any]:
        """Mutate one file in the live workspace, keeping the bytes we replaced."""
        recipe = inject.RECIPES[self.recipe_id]
        target = self.workspace / recipe["file"]
        if not target.is_file():
            raise RuntimeError(f"inject target {target} does not exist yet; retry later")
        backup = self.dir / target.name
        shutil.copy2(target, backup)
        self.manifest["files"] = {str(target): str(backup)}
        self.manifest["root_cause"] = recipe.get("root_cause")
        self.manifest["cascade"] = recipe.get("cascade", [])
        shas = inject.apply_recipe(self.recipe_id, self.workspace)
        self.manifest["sha_before"] = shas["before_sha256"]
        self.manifest["sha_after"] = shas["after_sha256"]
        if shas["before_sha256"] == shas["after_sha256"]:
            raise RuntimeError("recipe changed nothing on disk")
        self._save()
        return self.manifest

    def restore(self) -> bool:
        """Byte-exact rollback of the environment only. The model's context is
        deliberately left untouched — that asymmetry is what C-roll measures."""
        ok = True
        for original, backup in self.manifest["files"].items():
            try:
                shutil.copy2(backup, original)
                ok = ok and _sha(Path(original)) == self.manifest["sha_before"]
            except OSError:
                ok = False
        self.manifest["restored"] = ok
        self._save()
        return ok

    def _save(self) -> None:
        (self.dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _sha(path: Path) -> str:
    return inject.sha(path)


def write_recovery_md(workspace: Path, lines: list[str]) -> Path:
    path = workspace / "RECOVERY.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def terminal_verdict(task_id: str, run_id: str, workspace: Path) -> dict[str, Any]:
    alarms = audit.audit_run(task_id, runner.RUNS_ROOT / run_id, workspace)
    lat = audit.latched(alarms)
    return {
        "latched_counts": {s: sum(1 for v in lat.values() if v["status"] == s) for s in ("pass", "fail", "na")},
        "violated": sorted(k for k, v in lat.items() if v["status"] == "fail"),
        "satisfied_by_ledger": all(v["status"] == "pass" for v in lat.values()),
        "ledger_verdict": audit.verdict_of(lat),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--arm", required=True, choices=("C-ROLL", "C-REC"))
    ap.add_argument("--recipe", required=True, choices=sorted(inject.RECIPES))
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--inject-after", type=int, default=6, help="provider steps before the drift is injected")
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--minutes", type=int, default=18)
    ap.add_argument("--extra-rounds", type=int, default=1)
    args = ap.parse_args()

    runner.ping()
    stamp = time.strftime("%H%M%S")
    run_id = f"{args.task}-{args.arm}{args.rep}-{stamp}"
    workspace = runner.WS_ROOT / f"ws_{run_id}"
    import taskset

    task_path = taskset.materialize(args.task, workspace)
    task_text = task_path.read_text(encoding="utf-8")
    # Both C arms see the same standing instruction, so the file is not a hint
    # that only one arm gets: the difference between them is content, not access.
    task_text += "\n\n若工作区根目录存在 RECOVERY.md，先按其中说明处理再继续。\n"
    status, out = runner.call(
        "POST",
        "/api/runs",
        {
            "task": task_text,
            "agent": runner.AGENT,
            "model": runner.MODEL,
            "workspace": str(workspace),
            "run_id": run_id,
            "max_rounds": args.max_rounds,
            "prompt_language": "zh",
        },
    )
    if status != 200:
        raise SystemExit(f"create failed ({status}): {out}")
    bp = Breakpoint(args.recipe, workspace, run_id)
    # The record is created here, not inside _run_body: if the body raises, main
    # still writes what it has. A run that leaves no manifest is a run that
    # silently disappears from the sample count — that is how the C1/C-REC rep-12
    # attempt vanished on 2026-09-27 21:25 (an AssertionError escaped and took the
    # record with it).
    rec: dict[str, Any] = {
        "run_id": run_id, "task_id": args.task, "arm": args.arm, "recipe": args.recipe,
        "entry": "api", "model": runner.MODEL, "agent": runner.AGENT, "workspace": str(workspace),
        "started_at": time.strftime("%F %T %Z"), "attempts": [], "readings": [],
    }
    code = 1
    try:
        code = _run_body(args, rec, run_id, workspace, bp)
    except Exception as exc:                       # noqa: BLE001 - record then degrade
        import traceback

        rec["crashed"] = f"{type(exc).__name__}: {exc}"[:300]
        rec["crash_trace"] = traceback.format_exc(limit=6)[-900:]
        code = EXIT_CRASHED
    finally:
        _ensure_stopped(run_id)
    _write(rec, run_id)
    return code


def _run_body(args, rec: dict[str, Any], run_id: str, workspace: Path, bp: "Breakpoint") -> int:
    """Everything that can raise, kept apart from the create/cleanup bookkeeping
    so a failure cannot leave a worker parked. It happened twice today: two runs
    sat in waiting_approval after the driver died, and each had to be killed by
    hand from a later session.

    `rec` is the caller's dict and is only ever mutated — rebinding it here is
    what made 8 dispatches die before their first provider call (they spent
    nothing, but they also produced nothing, and left no manifest behind).
    """
    commitments = [c for c in load_commitments(args.task) if c["applies_to"] == "workspace" and c["predicate"] not in recover.HEAVY]

    injected_at = None
    reading: list[dict[str, str]] = []
    gates: list[dict[str, Any]] = []
    deadline = time.time() + args.minutes * 60
    snap = {}
    while time.time() < deadline:
        _, snap = runner.call("GET", f"/api/runs/{run_id}/status")
        prog = tap.live_progress(runner.RUNS_ROOT / run_id)
        steps = prog["steps"]
        # Read first, inject second. A reading taken before the drift exists is
        # the only thing that can later serve as a rollback target, and taking
        # the reading in the same breath as the injection would make
        # detected_lag_steps measure my own loop instead of the sensor.
        reading.append({"step": steps, "cost_usd": prog["cost_usd"],
                        "t": round(time.time(), 3), "unclosed": prog["unclosed_steps"],
                        **sensor_tick(commitments, workspace)})
        red = _first_red_after(reading, injected_at)
        if red is not None:
            rec["sensor"] = red
            break
        if injected_at is None and steps >= args.inject_after and snap.get("alive"):
            # The family seeds ship carrying a defect -- the task text asks the
            # agent to find and fix it -- so the promise this recipe breaks can
            # already be red through no action of ours. Injecting then would
            # "detect" a drift we did not cause (measured: 3 of 26 valid samples,
            # all with lag 0-1), and C1's recipe asserts against a row count that
            # isn't there yet. So the gate is: that promise reads pass NOW.
            root, state = inject_gate_state(reading, args.recipe)
            if state != "pass":
                gates.append({"step": steps, "root_cause": root, "state": state})
                rec["injection_gate"] = gates[-8:]
            else:
                try:
                    bp.apply()
                    injected_at = steps
                    rec["injected_at_provider_step"] = injected_at
                    rec["injection"] = bp.manifest
                    rec["green_before_inject"] = True
                except (RuntimeError, AssertionError) as exc:
                    rec["injection_deferred"] = f"{type(exc).__name__}: {exc}"[:200]
        if not snap.get("alive"):
            break
        time.sleep(POLL_SECONDS)

    rec["readings"] = reading[-12:]
    if gates:
        rec["injection_gate"] = gates[-8:]
    if "sensor" not in rec:
        # Three different failures used to share rc=6: the sensor missing a real
        # drift, the provider being dead so the run never started, and the gate
        # never opening (agent hadn't repaired the seeded defect in time).
        # Measured on 2026-09-27: 4 of 6 rc=6 runs returned in under 10 seconds.
        # Folding them together silently depressed the detection rate.
        steps_now = _provider_steps(runner.RUNS_ROOT / run_id)
        if not steps_now:
            rec["aborted"] = "provider_dead_no_steps"
        elif injected_at is None and gates:
            rec["aborted"] = "injection_gate_never_open"
        else:
            rec["aborted"] = "drift_not_detected"
        rec["steps_at_abort"] = steps_now
        rec["verdict_end_state"] = terminal_verdict(args.task, run_id, workspace)
        return EXIT_PROVIDER_DEAD if not steps_now else (
            EXIT_NOT_INJECTABLE if rec["aborted"] == "injection_gate_never_open" else EXIT_NO_DETECT)

    red = rec["sensor"]
    green_before = _last_green_before(reading, red["index"], red["commitment_id"])
    rec["last_invariant_holding_provider_step"] = green_before
    rec["rollback_ok"] = bp.restore()
    if args.arm == "C-REC":
        lines = [
            "# RECOVERY.md — 机器生成的断点说明",
            "",
            f"- 探针在第 {red['provider_step']} 个 provider 步发现不变量被破坏（出处 `rule:{red['commitment_id']}@step{red['provider_step']}`）。",
            f"- 实测：{red['actual']}",
            f"- 要求：{red['expected']}",
            f"- 上一个全部成立的步：第 {green_before} 步。之前的产物视为可信。",
            "- 不要重放已经写过的产物；先核对再决定要不要改。",
        ]
        rec["recovery_md"] = str(write_recovery_md(workspace, lines))
    # Upstream rejects `resume` on a live run with 409 "cannot resume an active
    # run" (service.py:2306). The first version of this driver skipped the stop
    # and recorded a 409 as if recovery had been attempted.
    runner.call("POST", f"/api/runs/{run_id}/stop", {})
    rec["stopped_before_resume"] = _await_terminal(run_id)
    halt = tap.live_progress(runner.RUNS_ROOT / run_id)
    rec["at_halt"] = halt_gap(red, halt)
    resume = runner.resume_run(run_id, args.extra_rounds)
    rec["attempts"].append({"kind": "resume", **resume})
    if resume["ok"]:
        snap = runner.drive_to_terminal(run_id, max_minutes=args.minutes)
    rec["attempts"].append({"kind": "post-recovery", "status": snap.get("status")})
    rec["metrics"] = runner.collect(run_id)
    rec["report"] = runner.read_report(run_id)
    rec["verdict_end_state"] = terminal_verdict(args.task, run_id, workspace)
    _write(rec, run_id)
    return runner.exit_code_for({"metrics": rec["metrics"], "report": rec["report"], "timed_out": False})


def _ensure_stopped(run_id: str) -> None:
    """A driver that exits while its worker is still live is a leak, not a finish."""
    _, snap = runner.call("GET", f"/api/runs/{run_id}/status")
    if not snap.get("alive"):
        return
    runner.call("POST", f"/api/runs/{run_id}/stop", {})
    _await_terminal(run_id)


def _await_terminal(run_id: str, seconds: int = 90) -> bool:
    for _ in range(seconds):
        _, snap = runner.call("GET", f"/api/runs/{run_id}/status")
        if not snap.get("alive"):
            return True
        time.sleep(1)
    return False


def inject_gate_state(reading: list[dict[str, Any]], recipe_id: str) -> tuple[str | None, str | None]:
    """The recipe's root-cause promise and its status in the newest reading.

    Injection waits until that promise reads `pass`. Without the gate a drift is
    injected onto a promise the seed already broke (the family tasks open carrying
    a defect the agent must fix), and the "detection" that follows measures the
    seed rather than the injection — 3 of 26 otherwise-valid samples did exactly
    that, all of them reporting the flattering lag of 0-1 steps.
    """
    root = inject.RECIPES.get(recipe_id, {}).get("root_cause")
    if not root or not reading:
        return root, None
    det = reading[-1].get(root)
    return root, (det.get("status") if isinstance(det, dict) else None)


def halt_gap(red: dict[str, Any], halt: dict[str, Any], t_now: float | None = None) -> dict[str, Any]:
    """What the run spent between "the sensor knew" and "the run lay still".

    This is the only version of that number which survives the resume rewriting
    the round files: it is read live, at both ends. An unclosed step in `halt`
    was cut in flight — no `step_finish` is ever written for it, so any cost it
    incurred stays invisible, and `unclosed > 0` together with a zero cost delta
    is exactly that case rather than "nothing was billed".

    `halt` must be the sample taken the moment the run is confirmed terminal.
    Feeding it the end-of-run state re-adds the recovery work as if it were waste
    — that is defect #21, the mistake this function exists to make impossible.
    """
    out = dict(halt)
    out["steps_after_detection"] = halt["steps"] - red["provider_step"]
    detect_cost = red.get("cost_usd_at_detect")
    detect_t = red.get("t_at_detect")
    out["cost_after_detection_usd"] = (None if detect_cost is None
                                       else round(halt["cost_usd"] - detect_cost, 6))
    out["seconds_after_detection"] = (None if detect_t is None
                                      else round((t_now if t_now is not None else time.time()) - detect_t, 1))
    out["mid_flight_step_cut"] = bool(halt.get("unclosed_steps"))
    return out


def _provider_steps(run_dir: Path) -> int:
    """Thin wrapper so nothing in this file counts steps with its own walk."""
    return tap.live_progress(run_dir)["steps"]


READING_SCALARS = ("step", "cost_usd", "t", "unclosed")


def _first_red_after(reading: list[dict[str, Any]], injected_at: int) -> dict[str, Any] | None:
    """The first red that appears *after* the injection, with the last green
    reading of the same promise kept alongside it."""
    for i, snap in enumerate(reading):
        if injected_at is None or snap["step"] <= injected_at:
            continue  # everything at or before the drift is baseline, not a finding
        for cid, det in snap.items():
            if cid in READING_SCALARS or not isinstance(det, dict) or det["status"] != "fail":
                continue
            return {
                "index": i,
                "provider_step": snap["step"],
                "cost_usd_at_detect": snap.get("cost_usd"),
                "t_at_detect": snap.get("t"),
                "unclosed_at_detect": snap.get("unclosed"),
                "commitment_id": cid,
                "expected": det["expected"],
                "actual": det["actual"],
                "detected_lag_steps": snap["step"] - injected_at,
            }
    return None


def _last_green_before(reading: list[dict[str, Any]], index: int, cid: str) -> int | None:
    step = None
    for snap in reading[:index]:
        det = snap.get(cid)
        if isinstance(det, dict) and det["status"] == "pass":
            step = snap["step"]
    return step


def _write(rec: dict[str, Any], run_id: str) -> None:
    (runner.MANIFESTS / f"{run_id}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: rec[k] for k in rec if k not in ("readings",)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
