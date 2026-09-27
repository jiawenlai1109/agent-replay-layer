#!/usr/bin/env bash
# The web service is the parent of every worker, so ITS PATH decides whether
# `opencode` resolves. Three traps, all hit for real on 2026-09-27:
#   1. a tmux start command runs in a NON-interactive shell and never sources
#      ~/.bashrc, so the export below is not optional;
#   2. `tmux kill-session` does not kill a server piped into tee and detached --
#      the stale process keeps port 8800, so new runs silently go to it;
#   3. items of GET /api/runs carry `id` and `status` but NO `alive` (verified
#      upstream shape), so liveness = status not in the terminal set, and a guard
#      written as r.get("alive") passes even with 8 runs in flight.
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
command -v opencode >/dev/null || { echo "FATAL: opencode not on PATH"; exit 3; }

BUSY_JSON=$(curl -s http://127.0.0.1:8800/api/runs)
BUSY=$(printf "%s" "$BUSY_JSON" | python3 - <<'PY'
import json, sys
TERMINAL = {"completed", "failed", "cancelled", "blocked", "incomplete"}
try:
    runs = json.load(sys.stdin).get("runs", [])
except Exception:
    print(-1)
    raise SystemExit
print(sum(1 for r in runs if r.get("status") and r["status"] not in TERMINAL))
PY
)
if [ "$BUSY" != "0" ]; then
  echo "ABORT: $BUSY 条 run 未到终态（-1 = 面板不可读）—— 先确认再重启"
  exit 4
fi

for pid in $(pgrep -f "lh-harness web"); do echo "kill stale web pid=$pid"; kill "$pid"; done
for i in $(seq 1 15); do pgrep -f "lh-harness web" >/dev/null || break; sleep 1; done
pgrep -f "lh-harness web" >/dev/null && { echo "ABORT: 旧进程没退出"; exit 5; }

tmux kill-session -t api 2>/dev/null; sleep 1
tmux new-session -d -s api "export PATH=\"$HOME/.npm-global/bin:$HOME/.local/bin:\$PATH\"; cd $HOME && $HOME/lh/bin/lh-harness web --runs-root $HOME/lh_runs --workspace-root $HOME/sbr_ws --port 8800 --no-open 2>&1 | tee $HOME/sbr/web8800.log; exec bash"
code=000
for i in $(seq 1 25); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8800/api/runs)
  [ "$code" = "200" ] && break
  sleep 1
done
pid=$(pgrep -f "lh-harness web" | head -1)
echo "api_http=$code pid=$pid busy=$BUSY"
if [ -n "$pid" ]; then tr "\0" "\n" < "/proc/$pid/environ" | grep "^PATH=" | sed "s|^|server |"; fi
