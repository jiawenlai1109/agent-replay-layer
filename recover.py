"""Recovery side: verdict store, failure locator, escalation, recovery directive.

Deterministic and offline — no LLM call anywhere in this file. The ledger author
proposes the checks; the predicates pass or fail them; this module only reads
recorded verdicts and decides where to rewind.

DESIGN §5 wiring order (tap first, in-line veto only after G3 is clean) is why
`scan_run` replays a *finished* run directory through the checkpoints an in-line
sensor would have hit, instead of observing a live one.

usage:
  python3 recover.py scan      <task_id> <run_dir> <workspace> [--out F.jsonl]
  python3 recover.py locate    <verdicts.jsonl>
  python3 recover.py directive <verdicts.jsonl> [--task-id ID]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import predicates
import snapshot
import tap
from taskset import load_commitments

STORE_VERSION = 2

# How expensive a sensor is decides how often it may run. A per-step pytest is
# 20 s x 232 steps, which is not "step granularity" but a new bottleneck. Cheap
# sensors run at every write checkpoint; heavy ones only once per round boundary.
HEAVY = {"tests_pass", "oracle_named_tests_pass", "differential_no_new_failures"}

# A red on a product-bearing predicate means the wrong thing is already on disk,
# so reminding the agent cannot undo it — only rewinding can.
PRODUCT = {
    "artifact_exists",
    "csv_row_count",
    "csv_unique_key",
    "json_matches_csv_aggregate",
    "claim_matches_measurement",
    "oracle_named_tests_pass",
    "differential_no_new_failures",
    "tests_pass",
}
WRITE_TOOLS = {"write", "edit", "str_replace", "create_file", "apply_patch", "multiedit"}
READ_ONLY_PREFIXES = (
    "ls", "cat", "head", "tail", "find", "grep", "rg", "pwd", "wc", "sort", "uniq",
    "stat", "file", "which", "echo", "python3 -m pytest", "python -m pytest",
    "python3 -m unittest", "python -m unittest", "git status", "git diff", "git log",
    "pip list", "python3 --version", "python -V",
)


def touched_gate(c: dict[str, Any], snap: Any) -> bool:
    """Judge a promise at a step only once its own paths are in play."""
    wanted = promised_paths(c)
    if not wanted:
        return True
    return bool(snap.touched & wanted)


def is_write_step(row: dict[str, Any]) -> bool:
    """A checkpoint exists only where state could have changed."""
    if row.get("kind") != "tool_use":
        return False
    name = (row.get("name") or "").lower()
    if name in WRITE_TOOLS:
        return True
    if name != "bash":
        return False
    probe = (row.get("input_excerpt") or "").strip()
    if not probe:
        return True
    return not any(probe.startswith(p) for p in READ_ONLY_PREFIXES)


PATH_KEYS = ("path", "file", "csv", "json", "glob", "dir")


def promised_paths(c: dict[str, Any]) -> set[str]:
    """Relative paths a workspace promise is about.

    Used to decide whether a checkpoint may judge it at all: an artifact nobody has
    written yet is 'not yet applicable', not a violation. Without this gate the
    sensor turns every on-progress state into a red, which is the false-positive
    class G3 exists to catch.
    """
    args = c.get("args", {}) or {}
    out = set()
    for key in PATH_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val and not val.startswith("/"):
            out.add(val.rstrip("/"))
    return out


class VerdictStore:
    """Append-only JSONL, one row per (checkpoint, predicate) evaluation.

    Every row carries provenance in the DESIGN §5 spelling so anything later
    rendered into a prompt can be traced back to the rule that produced it.
    """

    def __init__(self, path: Path | None = None, rows: list[dict[str, Any]] | None = None):
        self.path = Path(path) if path else None
        self.rows: list[dict[str, Any]] = list(rows or [])
        if self.path:
            # record() appends straight through, so the directory has to exist
            # before the first row. A scan into a fresh verdicts/ dir died with
            # FileNotFoundError the first time it was pointed at one.
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, path: Path) -> "VerdictStore":
        rows = [json.loads(ln) for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
        return cls(rows=rows)

    def record(self, **row: Any) -> None:
        row.setdefault("version", STORE_VERSION)
        self.rows.append(row)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def ordered(self) -> list[dict[str, Any]]:
        return sorted(self.rows, key=lambda r: (r["seq"], r["commitment_id"]))

    def for_commitment(self, cid: str) -> list[dict[str, Any]]:
        return [r for r in self.ordered() if r["commitment_id"] == cid]

    def checkpoints(self) -> list[int]:
        return sorted({r["seq"] for r in self.rows})

    def reds(self) -> list[dict[str, Any]]:
        return [r for r in self.ordered() if r["status"] == "fail"]

    def step_at(self, seq: int) -> str | None:
        for r in self.rows:
            if r["seq"] == seq:
                return r["step"]
        return None


def step_key(round_no: str, role: str, ordinal: int, provider_step: int | None) -> str:
    return f"{round_no}/{role}#{ordinal}:p{provider_step if provider_step else '?'}"


def scan_run(
    task_id: str,
    run_dir: Path,
    workspace: Path,
    store: VerdictStore | None = None,
    *,
    run_id: str | None = None,
    live_predicate: bool = True,
    commitments: list[dict[str, Any]] | None = None,
    attempt: int = 1,
) -> VerdictStore:
    """Replay a finished run through the checkpoints an in-line sensor would hit.

    `live_predicate=False` skips filesystem sensors and is what the unit tests use
    to check ordering without a workspace.
    """
    store = store or VerdictStore()
    run_id = run_id or Path(run_dir).name
    commitments = commitments if commitments is not None else load_commitments(task_id)
    seq = 0

    def evaluate(c: dict[str, Any], where: dict[str, Any], ws: Path) -> None:
        nonlocal seq
        fn = predicates.REGISTRY[c["predicate"]]
        t0 = time.perf_counter()
        try:
            verdict = fn({**c.get("args", {}), "workspace": str(ws)})
            status, actual, expected = verdict["status"], verdict["actual"], verdict.get("expected", "")
        except Exception as exc:
            # A sensor that crashes must report itself as red. The first version
            # of this guard died with a NameError and the batch read that as a
            # normal run, so the failure path is a tested path, not a comment.
            status, actual, expected = "fail", f"sensor_error:{type(exc).__name__}:{exc}", "sensor ran"
        store.record(
            run_id=run_id,
            seq=where["seq"],
            step=where["step"],
            round=where["round"],
            role=where["role"],
            provider_step=where["provider_step"],
            commitment_id=c["id"],
            predicate=c["predicate"],
            status=status,
            expected=str(expected)[:160],
            attempt=attempt,
            actual=str(actual)[:160],
            trigger=where["trigger"],
            provenance=f"rule:{c['id']}@{where['step']}",
            ms=int((time.perf_counter() - t0) * 1000),
        )

    if live_predicate:
        rounds_dir = Path(run_dir) / "lh_harness" / "role_orchestration" / "rounds"
        round_names = sorted(p.name for p in Path(rounds_dir).iterdir() if p.is_dir()) if rounds_dir.is_dir() else []
        # One forward pass: the snapshot is mutated to the state of this step, then
        # the cheap sensors read the snapshot. Reading the live workspace instead
        # would return the same verdict at every step and fake a location.
        with snapshot.Snapshot(task_id, workspace) as snap:
            for round_no in round_names:
                for role in tap.ROLES:
                    for row in tap.aligned_steps(Path(run_dir), round_no, role, with_input=True):
                        seq += 1
                        snap.apply(row)
                        if not is_write_step(row):
                            continue
                        where = {
                            "seq": seq,
                            "round": round_no,
                            "role": role,
                            "provider_step": row.get("provider_step"),
                            "step": step_key(round_no, role, row["ordinal"], row.get("provider_step")),
                            "trigger": f"write_step:{row.get('name')}",
                        }
                        for c in commitments:
                            if c["applies_to"] != "workspace" or c["predicate"] in HEAVY:
                                continue
                            if not touched_gate(c, snap):
                                continue
                            evaluate(c, where, snap.dir)
                for c in commitments:
                    if c["applies_to"] != "episode_metadata":
                        continue
                    round_dir = Path(run_dir) / "lh_harness" / "role_orchestration" / "rounds" / round_no
                    for meta_path in sorted(round_dir.glob("*_metadata.json")):
                        seq += 1
                        verdict = predicates.REGISTRY[c["predicate"]](
                            {"metadata": json.loads(meta_path.read_text(encoding="utf-8"))}
                        )
                        store.record(
                            run_id=run_id, seq=seq, step=f"{round_no}/{meta_path.name}", round=round_no,
                            role="episode_metadata", provider_step=None, commitment_id=c["id"],
                            predicate=c["predicate"], status=verdict["status"],
                            expected=str(verdict.get("expected", ""))[:160],
                            actual=str(verdict["actual"])[:160], trigger="runtime_signal",
                            provenance=f"rule:{c['id']}@{round_no}/{meta_path.name}", attempt=attempt, ms=0,
                        )
                seq += 1
                boundary = {
                    "seq": seq,
                    "round": round_no,
                    "role": "boundary",
                    "provider_step": None,
                    "step": f"{round_no}/boundary",
                    "trigger": "round_boundary",
                }
                for c in commitments:
                    if c["applies_to"] != "workspace" or c["predicate"] not in HEAVY:
                        continue
                    if not snap.touched.issuperset(promised_paths(c)) and snap.complete:
                        continue  # never attempted by this round: no step to blame
                    if not snap.complete:
                        # A bash step or an un-replayable edit happened: the tree we
                        # would test is not the tree the agent had. Say so, do not guess.
                        store.record(
                            run_id=run_id, seq=seq, step=boundary["step"], round=round_no,
                            role="boundary", provider_step=None, commitment_id=c["id"],
                            predicate=c["predicate"], status="na",
                            actual=f"snapshot_incomplete:{len(snap.opaque)}bash_writes/{len(snap.divergences)}divergences",
                            trigger="round_boundary", provenance=f"rule:{c['id']}@{boundary['step']}", attempt=attempt, ms=0,
                        )
                        continue
                    evaluate(c, boundary, snap.dir)

    rounds_path = Path(run_dir) / "lh_harness" / "role_orchestration" / "rounds.jsonl"
    rounds = predicates.read_rounds(rounds_path) if rounds_path.is_file() else []
    for c in commitments:
        if c["applies_to"] != "round":
            continue
        fn = predicates.REGISTRY[c["predicate"]]
        for at in range(1, len(rounds) + 1):
            seq += 1
            verdict = fn({"rounds": rounds, "at": at})
            store.record(
                run_id=run_id,
                seq=seq,
                step=f"round_{at:03d}",
                round=f"round_{at:03d}",
                role="round",
                provider_step=None,
                commitment_id=c["id"],
                predicate=c["predicate"],
                status=verdict["status"],
                actual=str(verdict["actual"])[:160],
                trigger="round_ledger",
                provenance=f"rule:{c['id']}@round_{at:03d}",
                attempt=attempt,
                ms=0,
            )
    return store


def locate(store: VerdictStore) -> list[dict[str, Any]]:
    """Three confidence tiers, per DESIGN §7. A bare step number is never emitted
    as if it were certain: what the sensor did not sample stays a gap."""
    out: list[dict[str, Any]] = []
    cps = store.checkpoints()
    for red in store.reds():
        cid = red["commitment_id"]
        hist = store.for_commitment(cid)
        earlier_fail = [r for r in hist if r["status"] == "fail" and r["seq"] < red["seq"]]
        if earlier_fail:
            continue  # only the first red localizes anything; the rest are latching
        prior_pass = [r for r in hist if r["status"] == "pass" and r["seq"] < red["seq"]]
        if not prior_pass:
            tier, bounded, rewind = "DETECTED_ONLY", None, None
        else:
            last_ok = max(prior_pass, key=lambda r: r["seq"])
            gap = [s for s in cps if last_ok["seq"] < s < red["seq"]]
            if gap:
                tier, bounded, rewind = "BOUNDED_BY", (last_ok["step"], red["step"]), last_ok["seq"]
            else:
                tier, bounded, rewind = "DETECTED_AT", None, last_ok["seq"]
        out.append(
            {
                "commitment_id": cid,
                "predicate": red["predicate"],
                "first_detectable_seq": red["seq"],
                "first_detectable_step": red["step"],
                "last_invariant_holding_seq": rewind,
                "last_invariant_holding_step": store.step_at(rewind) if rewind is not None else None,
                "bounded_by": bounded,
                "tier": tier,
                "rule": red["provenance"],
                "expected": red.get("expected", ""),
                "actual": red["actual"],
                "product_touched": red["predicate"] in PRODUCT,
            }
        )
    return sorted(out, key=lambda e: e["first_detectable_seq"])


def signatures(store: VerdictStore) -> dict[str, int]:
    """Red count per signature *per attempt*, not per checkpoint.

    Counting re-observations fires the breaker on one stable red state sampled
    sixteen times, which is latching rather than a loop. Measured on
    C1-csv-short, where a seed-level defect was re-sampled 16 times in one
    attempt and the first version of this function called that a circuit breaker.
    """
    seen: dict[str, set[Any]] = {}
    for r in store.reds():
        key = f"{r['commitment_id']}|{r['predicate']}|{r['actual'][:60]}"
        seen.setdefault(key, set()).add(r.get("attempt", 1))
    return {k: len(v) for k, v in seen.items()}


def escalate(
    store: VerdictStore,
    evidences: list[dict[str, Any]],
    *,
    observe: tuple[str, ...] = (),
    repeat_limit: int = 2,
) -> dict[str, Any]:
    """NUDGE / VETO / HALT / OBSERVE. The threshold rises with the cost of the
    intervention, because a false VETO destroys a run that would have passed.

    The breaker fires when one signature recurs across >=2 attempts: within a
    single attempt a red simply latches, and re-reading the same broken artifact
    is not evidence that the agent is looping.
    """
    if not evidences:
        return {"response": "OBSERVE", "rewind_to": None, "reason": "no_red_verdict", "blocked": []}
    sig = signatures(store)
    breaker = sorted(k for k, n in sig.items() if n >= repeat_limit)
    if breaker:
        return {
            "response": "HALT",
            "rewind_to": None,
            "reason": "circuit_breaker",
            "signatures": {k: sig[k] for k in breaker},
            "blocked": sorted({k.split("|")[0] for k in breaker}),
        }
    ev = min(evidences, key=lambda e: e["first_detectable_seq"])
    cid = ev["commitment_id"]
    if cid in observe:
        return {"response": "OBSERVE", "rewind_to": None, "reason": f"observe_only:{cid}", "blocked": []}
    if ev["tier"] == "DETECTED_ONLY" and not ev["product_touched"]:
        return {"response": "NUDGE", "rewind_to": None, "reason": f"first_red_unlocalised:{cid}", "blocked": []}
    if ev["product_touched"]:
        return {
            "response": "HALT" if ev["last_invariant_holding_seq"] is None else "VETO",
            "rewind_to": ev["last_invariant_holding_seq"],
            "reason": f"product_contaminated:{ev['predicate']}",
            "blocked": [cid],
            "at_step": ev["first_detectable_step"],
        }
    return {"response": "NUDGE", "rewind_to": ev["last_invariant_holding_seq"], "reason": f"first_red:{cid}", "blocked": []}


def compose_directive(
    store: VerdictStore,
    evidences: list[dict[str, Any]],
    rec: dict[str, Any],
    task_id: str | None = None,
    attempt_no: int = 1,
) -> str:
    """The recovery prompt is rendered from recorded verdicts. Zero LLM generation."""
    lines = [
        f"# 恢复指令（第 {attempt_no} 次尝试，响应={rec['response']}）",
        "",
        "以下每一条都来自台账判定并带触发步骤，不是对你的任务的重新描述。",
        "",
    ]
    if rec.get("rewind_to") is not None:
        lines.append(
            f"- 回到 {self_step(store, rec['rewind_to'])} 之后的状态继续（step_seq={rec['rewind_to']}），"
            "该区间的产物视为可信。"
        )
    else:
        lines.append("- 无安全回退点：相关承诺在变红之前从未被观测为绿，整段产物都要重新核对。")
    for ev in evidences:
        lines.append(
            f"- 承诺 `{ev['commitment_id']}`（{ev['predicate']}）在 {ev['first_detectable_step']} 判 fail，"
            f"置信档 {ev['tier']}；要求 {ev['expected'][:60]}，实测 {ev['actual'][:90]}；出处 {ev['rule']}。"
        )
        if ev.get("bounded_by"):
            lines.append(
                f"  ⇒ 只能夹出区间 {ev['bounded_by'][0]} → {ev['bounded_by'][1]}，"
                "中间没有被采样的步，不要假装知道更早的位置。"
            )
    if rec.get("blocked"):
        lines += ["", "禁止重放以下承诺对应的写动作（产物已污染），先修好再前进：" + "、".join(rec["blocked"])]
    if task_id:
        lines += ["", f"（任务 {task_id} 的完整承诺集不在本指令中复述，判据不应变成提示词。）"]
    return "\n".join(lines)


def self_step(store: VerdictStore, seq: int) -> str:
    return store.step_at(seq) or f"seq{seq}"


def overhead(store: VerdictStore) -> dict[str, Any]:
    return {
        "rows": len(store.rows),
        "checkpoints": len(store.checkpoints()),
        "eval_ms_total": sum(r.get("ms", 0) for r in store.rows),
        "reds": len(store.reds()),
        "by_status": {s: sum(1 for r in store.rows if r["status"] == s) for s in ("pass", "fail", "na")},
    }


def report(store: VerdictStore) -> dict[str, Any]:
    ev = locate(store)
    return {"overhead": overhead(store), "evidences": ev, "recovery": escalate(store, ev)}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "scan":
        task_id, run_dir, workspace = argv[2], Path(argv[3]), Path(argv[4])
        out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else Path(f"{run_dir.name}.verdicts.jsonl")
        if out.exists():
            out.unlink()
        store = scan_run(task_id, run_dir, workspace, VerdictStore(out), run_id=run_dir.name)
        print(json.dumps(report(store), ensure_ascii=False, indent=2))
        print(f"-- wrote {out} ({len(store.rows)} rows)")
        return 1 if store.reds() else 0
    if cmd == "locate":
        store = VerdictStore.load(Path(argv[2]))
        print(json.dumps(report(store), ensure_ascii=False, indent=2))
        return 1 if store.reds() else 0
    if cmd == "directive":
        store = VerdictStore.load(Path(argv[2]))
        ev = locate(store)
        rec = escalate(store, ev)
        tid = argv[argv.index("--task-id") + 1] if "--task-id" in argv else None
        print(compose_directive(store, ev, rec, task_id=tid))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
