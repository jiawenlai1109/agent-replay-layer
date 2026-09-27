#!/usr/bin/env bash
# Serial driver for the C arms, with the same credential preflight as batch.sh.
#
# Usage: batch_c.sh <C-ROLL|C-REC> [rep]
# The task/recipe pairs below are the only combinations that exist: an injection
# recipe needs a concrete artifact to corrupt, so the family-A repo tasks cannot
# be driven from here until someone writes repo-level recipes.
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
cd "$HOME/sbr"

ARM="${1:-C-ROLL}"; REP="${2:-3}"
case "$ARM" in C-ROLL|C-REC) ;; *) echo "arm must be C-ROLL or C-REC"; exit 2;; esac

echo "### preflight $(date +%T)"
PROBE=$(cd /tmp && echo "只回复两个字：收到" | timeout 90 opencode run --format json --yolo --model deepseek/deepseek-flash 2>&1 | tail -3)
if grep -qiE "Insufficient Balance|statusCode\":402|quota" <<<"$PROBE"; then
  echo "PREFLIGHT FAILED: 凭据不可用 —— 不启动批次"; exit 3
fi
grep -q '"type":"step_finish"' <<<"$PROBE" || { echo "PREFLIGHT FAILED: 端点无正常响应"; exit 3; }
echo "preflight OK"

PAIRS=(
  "T-PIPE-01F-C1-csv-short C1-csv-short"
  "T-PIPE-01F-C2-stale-summary C2-stale-summary"
  "T-PIPE-01F-C3-dup-key C3-dup-key"
  "T-PIPE-01F-C4-region-typo C4-region-typo"
)
LOG="$HOME/sbr/batch$ARM-$(date +%H%M%S).log"; : > "$LOG"
for pair in "${PAIRS[@]}"; do
  set -- $pair
  TASK="$1"; RECIPE="$2"
  echo "### $TASK / $RECIPE / $ARM rep=$REP $(date +%T)" | tee -a "$LOG"
  timeout 1500 python3 arm_c.py --task "$TASK" --recipe "$RECIPE" --arm "$ARM" \
      --rep "$REP" --inject-after 6 --max-rounds 4 --minutes 15 >>"$LOG" 2>&1
  rc=$?
  echo "   rc=$rc $(date +%T)" | tee -a "$LOG"
  if [ "$rc" -eq 3 ]; then echo "BATCH ABORT: 凭据致命" | tee -a "$LOG"; exit 3; fi
done
echo "BATCH_DONE $(date +%T)" | tee -a "$LOG"
