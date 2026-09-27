"""Offline step tap: turn a finished run directory into an indexed step table.

This is the substrate for first_detectable_step. It reads only durable artifacts
(rounds/round_NNN/{role}_trajectory.jsonl and its _raw_ sibling) and never the
dashboard's parse_trajectory(), whose ring buffer drops old steps and would
report the latency optimistically late.

usage: python3 tap.py <run_dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROLES = ("manager", "executor", "auditor", "final_response")


def input_excerpt(step: dict[str, Any], limit: int = 200) -> str:
    """A short, stable rendering of what a tool call was asked to do.

    The recovery side needs this to tell a read-only `bash` from one that writes
    files; without it every shell step looks like a state change and the sensor
    fires far more often than the run actually changed anything.
    """
    inp = step.get("input")
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "filePath", "path", "url", "pattern"):
        val = inp.get(key)
        if isinstance(val, str) and val:
            return val[:limit]
    return json.dumps(inp, ensure_ascii=False, sort_keys=True)[:limit]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def step_tokens(raw_path: Path) -> list[dict[str, Any]]:
    """Per-step token accounting, in order, from the provider's own events."""
    rows = []
    for rec in _jsonl(raw_path):
        if rec.get("type") != "step_finish":
            continue
        part = rec.get("part") or {}
        rows.append(
            {
                "reason": part.get("reason"),
                "tokens": part.get("tokens") or {},
                "cache": (part.get("tokens") or {}).get("cache") or {},
                "cost": part.get("cost", 0) or 0,
                "ts": rec.get("timestamp"),
            }
        )
    return rows


def index_run(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rounds_dir = Path(run_dir) / "lh_harness" / "role_orchestration" / "rounds"
    for round_dir in sorted(p for p in Path(rounds_dir).iterdir() if p.is_dir()) if rounds_dir.is_dir() else []:
        round_no = round_dir.name
        for role in ROLES:
            steps = _jsonl(round_dir / f"{role}_trajectory.jsonl")
            tokens = step_tokens(round_dir / f"{role}_raw_trajectory.jsonl")
            # Normalized steps and provider step_finish events are NOT 1:1: one
            # provider step can render as several UI steps. Aligning them by
            # ordinal silently mislabels the tail, so token columns stay None
            # wherever there is no backing event and the series is reported
            # separately via step_tokens().
            for i, step in enumerate(steps):
                acc = tokens[i] if i < len(tokens) else None
                t = (acc or {}).get("tokens") or {}
                rows.append(
                    {
                        "round": round_no,
                        "role": role,
                        "ordinal": i + 1,
                        "step_num": step.get("step_num"),
                        "kind": step.get("kind"),
                        "name": step.get("name"),
                        "is_error": bool(step.get("is_error")),
                        "input_excerpt": input_excerpt(step),
                        "step_total_tokens": t.get("total") if acc else None,
                        "cache_read": (acc.get("cache") or {}).get("read") if acc else None,
                        "cost": acc.get("cost") if acc else None,
                    }
                )
    return rows


def marginal_tokens(series: list[dict[str, Any]]) -> list[int | None]:
    """Per-step marginal cost = difference of the monotone running-context series."""
    out: list[int | None] = []
    prev: int | None = None
    for row in series:
        total = (row.get("tokens") or {}).get("total")
        out.append(None if total is None or prev is None else total - prev)
        if total is not None:
            prev = total
    return out


def provider_step_index(raw_path: Path) -> dict[str, int]:
    """Map provider event ids to a 1-based step number (the authoritative clock).

    `step_start` opens a step, `step_finish` closes it with token accounting.
    Every tool_use/text event carries the same messageID, so a normalised UI
    step can be attributed to the provider step that produced it. Without this
    the two numbering systems in one run (19 normalised steps vs 8 provider
    steps for the same episode) get silently mixed.
    """
    current = 0
    out: dict[str, int] = {}
    for rec in _jsonl(raw_path):
        kind = rec.get("type")
        part = rec.get("part") or {}
        pid = part.get("id")
        mid = part.get("messageID")
        if kind == "step_start":
            current += 1
        if pid:
            out[pid] = max(current, 1)
        if mid:
            out[mid] = max(current, 1)
    return out


def aligned_steps(run_dir: Path, round_no: str, role: str, *, with_input: bool = False) -> list[dict[str, Any]]:
    """Normalised steps carrying their provider step index and that step's tokens."""
    round_dir = Path(run_dir) / "lh_harness" / "role_orchestration" / "rounds" / round_no
    steps = _jsonl(round_dir / f"{role}_trajectory.jsonl")
    clock = provider_step_index(round_dir / f"{role}_raw_trajectory.jsonl")
    tokens = step_tokens(round_dir / f"{role}_raw_trajectory.jsonl")
    rows = []
    for i, step in enumerate(steps):
        key = next(
            (step[k] for k in ("id", "tool_use_id", "messageID") if step.get(k) in clock), None
        )
        prov = clock.get(key) if key else None
        rows.append(
            {
                "ordinal": i + 1,
                "step_num": step.get("step_num"),
                "kind": step.get("kind"),
                "name": step.get("name"),
                "is_error": bool(step.get("is_error")),
                "input_excerpt": input_excerpt(step),
                "input": step.get("input") if with_input else None,
                "provider_step": prov,
                "provider_total": (tokens[prov - 1]["tokens"].get("total") if prov and prov <= len(tokens) else None),
            }
        )
    # text/result steps carry no id the provider stream keys on, but they are the
    # assistant's own message content inside a step, so they belong to the step
    # that surrounds them. Forward-fill, then back-fill a leading gap.
    last = None
    for row in rows:
        if row["provider_step"] is None:
            row["provider_step"] = last
        else:
            last = row["provider_step"]
    if rows and rows[0]["provider_step"] is None:
        first = next((r["provider_step"] for r in rows if r["provider_step"]), 1)
        for row in rows:
            if row["provider_step"] is None:
                row["provider_step"] = first
    for row in rows:
        prov = row["provider_step"]
        row["provider_total"] = tokens[prov - 1]["tokens"].get("total") if prov and prov <= len(tokens) else None
    return rows


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    rows = index_run(Path(argv[1]))
    print(f"{'round':<11}{'role':<15}{'ord':<5}{'step_num':<10}{'kind':<13}{'name':<22}{'err':<5}{'tok':<9}{'cache'}")
    for r in rows:
        print(
            f"{r['round']:<11}{r['role']:<15}{r['ordinal']:<5}{str(r['step_num']):<10}"
            f"{str(r['kind']):<13}{str(r['name'])[:20]:<22}{('Y' if r['is_error'] else '-'):<5}"
            f"{str(r['step_total_tokens']):<9}{r['cache_read']}"
        )
    print(f"-- {len(rows)} steps, {sum(1 for r in rows if r['is_error'])} error steps")

    for round_no in sorted({r["round"] for r in rows}):
        raw = step_tokens(Path(argv[1]) / "lh_harness" / "role_orchestration" / "rounds" / round_no / "executor_raw_trajectory.jsonl")
        if raw:
            totals = [x["tokens"].get("total") for x in raw]
            print(f"-- {round_no} executor provider steps: {len(raw)}  运行上下文 {totals[0]} -> {totals[-1]}  每步边际 {marginal_tokens(raw)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

def _role_of_round_path(path: Path) -> str:
    return path.name.replace("_raw_trajectory.jsonl", "")


def _role_of_episode_path(path: Path) -> str:
    # lh_harness/cli_executor_episodes/ep001/provider_stream.jsonl -> executor
    for part in reversed(path.parts):
        if part.endswith("_episodes"):
            return part[len("cli_"):-len("_episodes")] if part.startswith("cli_") else part[:-len("_episodes")]
    return "unknown"


def live_progress(run_dir: Path) -> dict[str, Any]:
    """One pass over the artifacts as they stand *right now*: steps, cost, and
    how many steps are open without closing.

    Two things this exists to prevent:
    * Step count and cost must come from the same walk. When they are read twice
      with different filters the two rulers drift apart, and a metric built on
      the pair ("spend after detection") looks like savings while measuring
      nothing. This function is the single source for both.
    * A step that was started but never finished is what a halt cuts mid-flight:
      the provider had opened it, no `step_finish` was ever written, so whether it
      cost anything is invisible in the artifacts afterwards. The only way to
      catch it is to look while the run is live.
    """
    steps = 0
    cost = 0.0
    unclosed = 0
    last_ts: float | None = None
    last_type: str | None = None
    for f in sorted(Path(run_dir).glob(
            "lh_harness/role_orchestration/rounds/round_*/*_raw_trajectory.jsonl")):
        starts = finishes = 0
        for rec in _jsonl(f):
            kind = rec.get("type")
            if kind == "step_start":
                starts += 1
            elif kind == "step_finish":
                finishes += 1
                steps += 1
                cost += ((rec.get("part") or {}).get("cost", 0) or 0)
            ts = rec.get("timestamp")
            if ts is not None and (last_ts is None or float(ts) > last_ts):
                last_ts, last_type = float(ts), kind
        unclosed += max(starts - finishes, 0)
    return {"steps": steps, "cost_usd": round(cost, 6), "unclosed_steps": unclosed,
            "last_event_ts": last_ts, "last_event_type": last_type}


def cost_timeline(run_dir: Path, *, with_episodes: bool = False) -> list[dict[str, Any]]:
    """Every provider step_finish event in the run, oldest first.

    The run-progress number the sensors record is just a *count* of these events,
    so "N steps at detection" means "the N events with the smallest timestamps
    existed when the probe read the state". That makes the count usable as a cut
    in time — but only once the events are sorted by their own millisecond
    timestamps. Counting walks the files in alphabetical order inside a round
    (auditor, executor, final_response, manager) while the roles actually run
    manager -> executor -> auditor, so slicing that sequence at position N cuts
    somewhere other than the moment the probe looked.

    `with_episodes` also reads `lh_harness/*_episodes/*/provider_stream.jsonl`,
    which mirrors the round files. It is not redundant: a resume rewrites the
    round file for the same round number wholesale (0 of the 100 role files on
    disk mix attempts), so when the resumed attempt reuses an episode slot the
    earlier attempt survives only here — measured on 11 of 22 C samples.
    Duplicate events are dropped on (role, timestamp).
    """
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()

    def add(path: Path, role: str, where: str) -> None:
        rnd = path.parent.parent.name if where == "episodes" else path.parent.name
        for seq, rec in enumerate(step_tokens(path), start=1):
            ts = rec.get("ts")
            if ts is not None and where == "episodes":
                # Only the mirror is deduplicated. Two distinct steps can finish in
                # the same millisecond in the same role file, and dropping one of
                # those would silently shorten the authoritative stream.
                if (role, float(ts)) in seen:
                    continue
            if ts is not None:
                seen.add((role, float(ts)))
            events.append({
                "round": rnd,
                "role": role,
                "seq": seq,
                "source": where,
                "ts": None if ts is None else float(ts),
                "cost": rec.get("cost", 0) or 0,
                "tokens_total": (rec.get("tokens") or {}).get("total"),
            })

    for f in sorted(Path(run_dir).glob(
            "lh_harness/role_orchestration/rounds/round_*/*_raw_trajectory.jsonl")):
        add(f, _role_of_round_path(f), "rounds")
    if with_episodes:
        for f in sorted(Path(run_dir).glob("lh_harness/*_episodes/*/provider_stream.jsonl")):
            add(f, _role_of_episode_path(f), "episodes")
    # An event with no timestamp cannot be placed against the cut, so it sorts
    # last and the caller is told how many there are rather than getting a
    # silently shorter prefix.
    events.sort(key=lambda e: (float("inf") if e["ts"] is None else e["ts"], e["round"], e["role"], e["seq"]))
    return events


def split_at_detection(events: list[dict[str, Any]], n_at_detect: int | None) -> dict[str, Any]:
    """Sum event costs before / after the moment the probe saw N steps.

    Returns counts and both candidate denominators, because the share is only
    publishable when the two rulers agree; when they don't, say so in the row.
    """
    out: dict[str, Any] = {
        "events_total": len(events),
        "n_at_detect": n_at_detect,
        "no_ts_events": sum(1 for e in events if e["ts"] is None),
        "no_ts_cost": round(sum(e["cost"] for e in events if e["ts"] is None), 6),
        "timeline_cost_usd": round(sum(e["cost"] for e in events), 6),
        "cost_after_usd": None,
        "cost_before_usd": None,
        "cut_ts_ms": None,
        "tie_at_cut": 0,
        "error": "",
    }
    if n_at_detect is None:
        out["error"] = "no_detection_step"
        return out
    if n_at_detect < 0 or n_at_detect > len(events):
        # More steps at poll time than events on disk now means the artifact set
        # shrank under us (a truncated final line is the known cause).
        out["error"] = "count_exceeds_events"
        return out
    if out["no_ts_cost"]:
        # An event with no timestamp could be on either side of the cut, and it
        # costs money, so the share is unknowable rather than approximately right.
        out["error"] = "untimestamped_cost"
        return out
    head, tail = events[:n_at_detect], events[n_at_detect:]
    out["cost_before_usd"] = round(sum(e["cost"] for e in head), 6)
    out["cost_after_usd"] = round(sum(e["cost"] for e in tail), 6)
    if head:
        out["cut_ts_ms"] = head[-1]["ts"]
        out["tie_at_cut"] = sum(1 for e in events if e["ts"] == head[-1]["ts"])
    return out


def attempt_boundary(run_dir: Path) -> dict[str, Any]:
    """When the attempt that is now on disk started, from the control plane's own record.

    `owner.json` is rewritten on every resume, so its `started_at` marks the
    *latest* attempt's start — not the run's. That is the only durable
    resume instant: rounds.jsonl rows carry no timestamps and the command log is
    a second source that agrees with it to the millisecond.
    """
    out: dict[str, Any] = {"ts_ms": None, "resume_epoch": None, "error": ""}
    p = Path(run_dir) / "control" / "owner.json"
    if not p.is_file():
        out["error"] = "no_owner_file"
        return out
    try:
        own = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        out["error"] = "owner_unreadable:%s" % type(exc).__name__
        return out
    start = own.get("started_at")
    out["resume_epoch"] = own.get("resume_epoch")
    if not start:
        out["error"] = "owner_without_started_at"
        return out
    out["ts_ms"] = float(start) * 1000
    return out


def attempt_split(events: list[dict[str, Any]], boundary: dict[str, Any]) -> dict[str, Any]:
    """Split the surviving event stream into the interrupted attempt and the recovery.

    The recovery side is sound: nothing overwrites it afterwards. The interrupted
    side is only sound if it survived — a resume rewrites the same round
    directory, so the events the probe counted can be gone. Callers must compare
    `steps_before` against the count the probe recorded and refuse the number
    when it has shrunk.
    """
    out: dict[str, Any] = {"cost_before_usd": None, "cost_recovery_usd": None,
                           "steps_before": None, "steps_recovery": None,
                           "no_ts": None, "error": boundary.get("error") or ""}
    if out["error"]:
        return out
    bnd = boundary["ts_ms"]
    before = [e for e in events if e["ts"] is not None and e["ts"] < bnd]
    after = [e for e in events if e["ts"] is not None and e["ts"] >= bnd]
    out["steps_before"], out["steps_recovery"] = len(before), len(after)
    out["no_ts"] = len(events) - len(before) - len(after)
    out["cost_before_usd"] = round(sum(e["cost"] for e in before), 6)
    out["cost_recovery_usd"] = round(sum(e["cost"] for e in after), 6)
    return out


def epoch_split(run_dir: Path, epoch_ts_seconds: float | None) -> dict[str, Any]:
    """Split provider steps into before / after an epoch boundary.

    Directory mtimes cannot do this: a round directory is appended to across a
    restart, so its mtime is the *last* write and the whole directory then looks
    like it belongs to the newer epoch. The raw events carry their own
    millisecond timestamps, so the split is done per step instead.
    """
    out: dict[str, Any] = {"rounds": {}, "total_before": 0, "total_after": 0,
                           "no_ts": 0, "epoch_ts": epoch_ts_seconds}
    if not epoch_ts_seconds:
        out["error"] = "no epoch timestamp"
        return out
    boundary_ms = epoch_ts_seconds * 1000
    rounds_dir = Path(run_dir) / "lh_harness" / "role_orchestration" / "rounds"
    if not rounds_dir.is_dir():
        out["error"] = "no rounds dir"
        return out
    for round_dir in sorted(p for p in rounds_dir.iterdir() if p.is_dir()):
        per: dict[str, Any] = {}
        for role in ROLES:
            raw = round_dir / f"{role}_raw_trajectory.jsonl"
            if not raw.is_file():
                continue
            before = after = skip = 0
            for rec in step_tokens(raw):
                ts = rec.get("ts")
                if ts is None:
                    # Counted rather than dropped: a split that quietly loses
                    # events cannot be reconciled against the other two walkers.
                    skip += 1
                elif float(ts) < boundary_ms:
                    before += 1
                else:
                    after += 1
            per[role] = {"before": before, "after": after, "no_ts": skip}
            out["no_ts"] += skip
            out["total_before"] += before
            out["total_after"] += after
        out["rounds"][round_dir.name] = per
    return out
