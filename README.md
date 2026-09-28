# agent-replay-layer

A recovery layer for long-horizon agents. When a run drifts, this finds the step where an invariant broke, rolls the workspace back to that step, and hands the agent a machine-written breakpoint, so the run does not have to start over.

Built on top of [AMAP-ML/LongHorizon-Harness](https://github.com/AMAP-ML/LongHorizon-Harness) (used as a dependency, not a fork). The harness still orchestrates the run; this reads its durable artifacts, decides when a run has gone wrong, and drives stop/resume through the harness control API.

**Status**: the mechanism and its measurement apparatus are implemented and covered by offline tests. Measured results and the false-positive audit are pending, see [What is not here yet](#what-is-not-here-yet). This layer is MIT licensed (see `LICENSE`); the upstream harness's own licence is still unconfirmed, which is a separate question and is noted below.

## Why

The first reproduced run left an auditable waste: two consecutive rounds in `rounds.jsonl` carried identical `plan_text`, `next_step`, and `status`, meaning the same failure repeated in place, and nothing in the loop noticed. The run then spent about 480 more seconds before stopping with `max_rounds_exhausted`.

The step-level data needed to detect that is already on disk (every provider step records `step_num`, tokens, and cost). What is missing is a reader that turns it into a verdict and a rollback point. That is what this is.

## How it works

```
task text (agent sees this)          commitment ledger (agent never sees this)
        |                                        |
        v                                        v
   run via lh-harness  ---->  deterministic predicates check the workspace
                                             |
                              latched sensor table (pass / fail / na)
                                             |
                              locate the first broken step
                                             |
                    snapshot-replay the workspace back to that step
                                             |
                    render RECOVERY.md from a Python template (no LLM text)
                                             |
                              resume the run through the control API
```

Three rules hold the design together:

1. **The model proposes checks, it does not pass them.** Every verdict comes from a deterministic predicate over files and command exit codes. No LLM judge is used for success or failure, because the judge fails in ways that are hard to separate from the subject.
2. **A red assertion stays red.** Latching means one violation propagates to every later step until a reset, so "it looked fine again two steps later" cannot hide a break. `na` means the artifact does not exist yet, and is never read as a pass.
3. **Recovery instructions are rendered, not generated.** `RECOVERY.md` is built from a Python template over measured values, so the experiment never confuses "the model wrote a better hint" with "the hint helped".

## The four arms

The comparison exists to keep attribution honest. Each arm differs from the previous one by exactly one thing:

| Arm | Behaviour on failure | Isolates |
|---|---|---|
| A | restart from scratch | baseline |
| B | upstream round-level `resume` | what the existing harness already gives you |
| C-ROLL | rewind the workspace to the detected step, no instruction | the value of stepping, versus resuming at a round boundary |
| C-REC | rewind, plus the generated `RECOVERY.md` | the value of the instruction itself |

`C-REC minus C-ROLL` is the contribution of this project's directive. `C-ROLL minus B` is the contribution of step-level rewind. The two are reported separately and never added into one savings figure.

Every arm is dispatched through the same entry point (`POST /api/runs`). Runs created from the CLI instead land in a state where resume silently falls back to a default agent and workspace, and appends a fake round, so the API path is the only supported one here.

## Layout

| File | Role |
|---|---|
| `sbr_paths.py` | single owner of every filesystem location; all overridable by environment |
| `predicates.py` | 14 deterministic checks, one signature: `run(args) -> {status, expected, actual, cmd_line}` |
| `taskset.py`, `tasks/`, `ledger/` | task cards the agent reads, and the commitment ledger it does not |
| `tap.py` | artifact reader: provider-step timeline, step alignment, cross-attempt attribution |
| `snapshot.py` | replays workspace state from the seed, so a rollback has somewhere to land |
| `recover.py` | verdict store, three-tier locator, escalation ladder, directive template |
| `audit.py`, `audit_ledger_promises.py` | offline re-audit; checks that no promise depends on wording the task never required |
| `fp_audit.py` | the false-positive audit: re-scores every collected run with the current predicates, and only reports a rate where a truth independent of the sensor exists |
| `arm_c.py` | the C-arm driver: gate, inject, halt, rewind, resume, cost sampling |
| `runner.py` | run dispatch and control-API client |
| `batch*.sh`, `restart_api.sh` | serial sweeps with a credential preflight; service restart |
| `pair_report.py`, `cost_to_success.py`, `stats.py` | results. Exact tests live in `stats.py` only, so no p-value is ever hand computed |
| `make_family_a.py`, `inject.py`, `btest.py`, `probe_dead.py`, `rescore_verdicts.py` | task construction, drift recipes, one-shot probes, re-scoring |

## Requirements

- Python 3.10 or newer (the harness wheel is `py3-none-any`; the installed package is the source you read).
- `pip install lh-harness` (see `requirements.txt`).
- An agent CLI on `PATH`. This was built against `opencode-ai` 1.18.32 driven non-interactively.
- Model credentials configured **in the agent CLI's own store**. The harness `--api-key` flag is deliberately unused: that adapter builds a command line containing the key, and the harness writes the full command into run metadata.
- Linux. The upstream README notes only macOS testing; everything here ran on Ubuntu 22.04.

Locations default to `$HOME` and can be pointed elsewhere:

| Variable | Default | Contents |
|---|---|---|
| `SBR_HOME` | `~/sbr` | this code |
| `LH_RUNS_ROOT` | `~/lh_runs` | harness run directories |
| `LH_WORKSPACE_ROOT` | `~/sbr_ws` | one workspace per run |
| `LH_API` | `http://127.0.0.1:8800` | harness control API (loopback only) |
| `SBR_REPOS`, `SBR_VENVS` | `~/tasks`, `~/venvs` | optional: upstream checkouts and venvs for the real-bug tasks |

## Quick start

```bash
pip install -r requirements.txt
npm install -g opencode-ai          # then: opencode auth login, and set the model there
python3 -m unittest discover -p "test_*.py"      # offline, no provider calls

bash restart_api.sh                              # starts the harness web control plane
python3 arm_c.py --task T-PIPE-01F-C1-csv-short --recipe C1-csv-short \
    --arm C-REC --rep 1 --inject-after 6 --max-rounds 4 --minutes 12
python3 pair_report.py                           # per-sample table plus paired comparison
python3 cost_to_success.py                       # cost attribution, live and offline
python3 fp_audit.py --arm all --repeat 3         # false-positive audit over the collected runs
```

All tests are offline: no provider call, no cost. The suites that evaluate a predicate
run it as a subprocess against a real interpreter, so they assume Linux; off Linux they
skip rather than report a false failure. Verified state: 144 tests, all green on
Ubuntu 22.04.

Fixtures are split by what can be redistributed, see [tasks/README.md](tasks/README.md):
the pipeline seeds are ours, the real-bug seeds are upstream trees rebuilt by
`make_family_a.py`, and the hidden oracle test copies are not distributed at all.

`arm_c.py` writes one JSON record per run under `$SBR_HOME/manifests/`, including the parameters, the detection reading, the halt sample, and the ledger verdict. Those records are the input to both report scripts, so results are recomputable from artifacts rather than quoted from a terminal.

## Known limits

- **Replay covers declared file writes only.** `write` carries full content and `edit` carries an exact replacement, so both replay. A `bash` action that mutates a repository tree is opaque, so runs that work that way have an end-state verdict but no step-level localisation.
- **External side effects do not roll back.** Requests already sent and remote writes already landed are outside any local snapshot, and a rollback that ignores them creates false confidence.
- **A promise must be formalizable to be latched.** Acceptance criteria that cannot be written as a predicate fall outside the mechanism.
- **A halt is not free money.** On the samples where cost was sampled live at both the detection and the halt, the spend between them was zero, which means early interception avoids no completed work; the savings argument rests on skipped rounds and on side-effect risk instead.
- **The online detector is an experiment instrument, not a production gate.** `arm_c.py` treats a red as drift only after the step where it injected the defect itself, which is the one piece of information a real deployment does not have. A deployable trigger has to read "a red that is new relative to the last green reading, on a promise whose paths are already in play"; the second half exists in `recover.py` and the first half is not wired into the sensor tick.
- **Small, repeated templates.** Sweeps are cheap in dollars and expensive in wall-clock on a 2-core box, so breadth is limited until the report says otherwise.

## What is not here yet

- Measured result tables with sample counts and exact tests. They will land with the report, together with the reproduction records the numbers came from.
- A false-positive rate for the online gate. `fp_audit.py` computes it and prints a bound rather than a bare "none observed", but it reports a rate only over the universe where the truth does not come from the sensor itself. The numbers belong with the report.
- Injection recipes for real-bug repositories, which is what is needed before any claim transfers from synthetic pipeline tasks to repository tasks.
- A second agent CLI adapter. Until one exists, "harness and CLI independent" is an architectural claim, not a measured one.
- Confirmation of the **upstream** licence. This repository is MIT, but `lh-harness` leaves its PyPI licence field empty while its badge claims MIT, so the dependency's terms are still unverified. Resolve before any redistribution of this code as a product.

## Upstream

This depends on `LongHorizon-Harness` and does not modify it: the recovery primitives, the round records, and the runtime signal classification all come from upstream, and the honest description of the gap is that upstream classifies signals without localising them, resumes at run level without a step index, and offers VM snapshots only for hosted-VM tasks. Anything in this repository that duplicates an upstream capability is a mistake worth reporting.
