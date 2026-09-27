"""Arm-B plumbing check on a throwaway run: does resume reopen it, and does the
owner record survive?

Deliberately refuses any run that is still a citable A-arm sample, because
resume(mode="continue") reopens the same run directory and appends rounds to
its rounds.jsonl. Measured cost of this script is a few seconds of worker time,
not a run.

usage: python3 btest.py <run_id> [--hold SECONDS]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
import sbr_paths

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner  # noqa: E402

MANIFESTS = sbr_paths.MANIFESTS
ROUNDS = "lh_harness/role_orchestration/rounds.jsonl"


def protected_ids() -> set[str]:
    """Runs that appear in a table must stay byte-identical."""
    out = set()
    for path in MANIFESTS.glob("*.json"):
        m = json.loads(path.read_text(encoding="utf-8"))
        if (m.get("metrics") or {}).get("steps") and not runner.is_provider_fatal(m.get("report") or {}):
            out.add(m.get("run_id") or path.stem)
    return out


def rounds_lines(run_id: str) -> list[dict]:
    path = sbr_paths.RUNS_ROOT / run_id / ROUNDS
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def show(label: str, value) -> None:
    print("%-22s %s" % (label, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value))


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: btest.py <run_id> [--hold SECONDS]")
    run_id = sys.argv[1]
    hold = 0.0
    if "--hold" in sys.argv:
        hold = float(sys.argv[sys.argv.index("--hold") + 1])

    guard = protected_ids()
    if run_id in guard:
        show("REFUSED", "run is a citable A-arm sample: %s" % run_id)
        return 2

    status0, snap0 = runner.call("GET", f"/api/runs/{run_id}/status")
    o0 = snap0.get("owner") or {}
    show("pre.http", status0)
    show("pre.status", snap0.get("status"))
    show("pre.alive", snap0.get("alive"))
    show("pre.owner", {k: o0.get(k) for k in ("agent", "model", "workspace", "resume_epoch")})
    pre = rounds_lines(run_id)
    show("pre.rounds", len(pre))

    t_start = time.time()
    status1, body = runner.call("POST", f"/api/runs/{run_id}/resume", {"mode": "continue", "extra_rounds": 1})
    show("resume.http", status1)
    show("resume.body_keys", sorted(body) if isinstance(body, dict) else str(body)[:200])
    show("resume.ok", body.get("ok") if isinstance(body, dict) else None)
    run_body = body.get("run") if isinstance(body, dict) else {}
    show("resume.run_keys", sorted(run_body) if isinstance(run_body, dict) else str(run_body)[:200])

    time.sleep(hold)

    # POST, not GET, and with a JSON body: the endpoint rejects a bodyless request
    # with 400 "request must be application/json". The first version of this line
    # was a GET, so the run was never stopped and kept billing for 60s.
    status2, snap1 = runner.call("POST", f"/api/runs/{run_id}/stop", {})
    show("stop.http", status2)
    show("stop.body", snap1 if not isinstance(snap1, dict) else {k: snap1.get(k) for k in ("ok", "status", "alive")})

    for _ in range(20):
        _, snap2 = runner.call("GET", f"/api/runs/{run_id}/status")
        if not snap2.get("alive"):
            break
        time.sleep(1)
    o1 = (snap2.get("owner") or {})
    show("post.status", snap2.get("status"))
    show("post.owner", {k: o1.get(k) for k in ("agent", "model", "workspace", "resume_epoch")})
    show("owner_preserved", all(
        o1.get(k) == o0.get(k) for k in ("agent", "model", "workspace")
    ))
    post = rounds_lines(run_id)
    show("post.rounds", len(post))
    show("wall_seconds", round(time.time() - t_start, 1))
    if len(post) > len(pre):
        added = post[len(pre):]
        show("added_rounds", [
            {
                "round": r.get("round") or r.get("round_index"),
                "kind": r.get("kind"),
                "dur_ms": r.get("duration_ms"),
                "has_output": bool(r.get("output") or r.get("result")),
            }
            for r in added
        ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
