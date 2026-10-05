#!/usr/bin/env bash
# D-arm paired batch: the control that decides whether recovery pays.
#
# Each cell runs the SAME task, SAME recipe, SAME detector and SAME budget twice:
#   D      - inject the drift, then interfere with nothing (no stop, no rewind)
#   C-REC  - inject the drift, stop, rewind to the last green step, hand over RECOVERY.md
# Comparing their cost per achieved success is the only reading that answers
# "值不值"; the earlier cross-arm table could not, because A never sees a drift.
#
# Credential preflight first: a 402 in the middle of a batch produces dead runs
# that look exactly like samples, and counting them is defect #16 / #33.
#
# Usage:  bash batch_d.sh 13
#         bash batch_d.sh 13 14 15
#         bash batch_d.sh 13 "T-PIPE-01F-C3-dup-key:C3-dup-key"
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
cd "$HOME/sbr" || exit 2

REPS=(); while [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; do REPS+=("$1"); shift; done
[ ${#REPS[@]} -eq 0 ] && REPS=(13)
FAMS=("$@")
if [ ${#FAMS[@]} -eq 0 ]; then
  FAMS=("T-PIPE-01F-C3-dup-key:C3-dup-key" "T-PIPE-01F-C4-region-typo:C4-region-typo")
fi

# EXTRA_ROUNDS is overridable because the first batch was not budget-aligned: D
# used 2-3 of its 3-round ceiling while C-REC only ever got 1 + 1. Run the
# aligned cells as `EXTRA_ROUNDS=2 bash batch_d.sh 15 <family>` so C-REC's total
# (1 before the halt + 2 after the rewind) equals D's ceiling.
MAX_ROUNDS=${MAX_ROUNDS:-3}; MINUTES=${MINUTES:-12}; INJECT_AFTER=${INJECT_AFTER:-6}; EXTRA_ROUNDS=${EXTRA_ROUNDS:-1}
# ARMS is overridable so a cell can be bought one arm at a time. The recovery arm
# may never trigger (defect #42: the agent rewrites the injected bytes), and a cell
# without a rewind is not a pair, so the cheap order is C-REC first and D only for
# the cells that actually rewound.
ARMS=${ARMS:-"D C-REC"}
# Cost numbers are only comparable within one model, so the model is printed next to
# the budget and passed to the preflight as well.
MODEL=${SBR_MODEL:-deepseek/deepseek-flash}; export SBR_MODEL=$MODEL
echo "### budget: max_rounds=$MAX_ROUNDS extra_rounds=$EXTRA_ROUNDS inject_after=$INJECT_AFTER minutes=$MINUTES"
echo "### model: $MODEL"
echo "### arms: $ARMS"

echo "### preflight $(date +%T)"
PROBE=$(cd /tmp && echo "只回复两个字：收到" | timeout 90 opencode run --format json --yolo \
        --model "$MODEL" 2>&1 | tail -3)
if grep -qiE "Insufficient Balance|statusCode\":402|quota" <<<"$PROBE"; then
  echo "PREFLIGHT FAILED: 凭据不可用 —— 不启动批次"; exit 3
fi
grep -q '"type":"step_finish"' <<<"$PROBE" || { echo "PREFLIGHT FAILED: 端点无正常响应"; exit 3; }
echo "preflight OK"

for rep in "${REPS[@]}"; do
  for spec in "${FAMS[@]}"; do
    task="${spec%%:*}"; recipe="${spec##*:}"
    for arm in $ARMS; do
      # Skip a cell that already has a live sample: a run with provider steps.
      # Replying "ok" to nothing is cheaper than paying twice for the same cell.
      done_cell=$(python3 - "$task" "$arm" "$rep" <<'PY'
import glob, json, sys
task, arm, rep = sys.argv[1:4]
for f in glob.glob(f"/home/laijiawen/sbr/manifests/{task}-{arm}{rep}-*.json"):
    try:
        m = json.load(open(f))
    except Exception:
        continue
    if int((m.get("metrics") or {}).get("steps") or 0) > 0 and not m.get("aborted"):
        print(f.split("/")[-1]); break
PY
)
      if [ -n "$done_cell" ]; then
        echo "### skip $task $arm rep $rep (have $done_cell)"
        continue
      fi
      # Stop at a cell boundary instead of killing the loop: a killed driver leaves
      # its lh-harness child running and billing with nobody left to write the
      # manifest, which is how 0.028885 USD became zero samples (defect #44).
      if [ -f "$HOME/sbr/STOP" ]; then
        echo "### STOP 文件存在：不启动新 run，批次在此退出 $(date +%T)"
        exit 5
      fi
      echo "### $task $arm rep $rep start $(date +%T)"
      python3 arm_c.py --task "$task" --arm "$arm" --recipe "$recipe" --rep "$rep" \
          --inject-after "$INJECT_AFTER" --max-rounds "$MAX_ROUNDS" --minutes "$MINUTES" \
          --extra-rounds "$EXTRA_ROUNDS"
      rc=$?
      echo "### $task $arm rep $rep rc=$rc end $(date +%T)"
      # rc 3/4/7 mean the credential or the endpoint is gone: stop, do not burn
      # empty runs. rc 6 (not detected) and rc 9 (gate never opened) are results.
      if [ "$rc" = "3" ] || [ "$rc" = "4" ] || [ "$rc" = "7" ]; then
        echo "ABORT: rc=$rc 凭据/provider 致命 —— 批次中止，不继续烧空 run"; exit "$rc"
      fi
    done
  done
done

echo "### batch done $(date +%T)"
python3 pair_report.py "*-D*" >/dev/null 2>&1 || true
python3 cost_compare.py | tail -20
