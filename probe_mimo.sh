#!/usr/bin/env bash
# One tiny MiMo call: does the endpoint answer, and what does opencode record for
# tokens/cost? Writes no manifest. Same invocation shape as the batch preflight.
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
MODEL="${1:-xiaomi/mimo-v2.6-flash}"
echo "### model: $MODEL  $(date +%T)"
cd /tmp || exit 2
PROBE=$(echo "只回复两个字：收到" | timeout 90 opencode run --format json --yolo --model "$MODEL" 2>&1 | tail -6)
echo "$PROBE"
if grep -qiE "Insufficient Balance|statusCode\":402|quota|Unknown model" <<<"$PROBE"; then
  echo "PROBE FAILED: 该 model 不可用或凭据不足"
  exit 3
fi
grep -q '"type":"step_finish"' <<<"$PROBE" || { echo "PROBE FAILED: 端点无正常 step_finish"; exit 4; }
echo "PROBE OK"
