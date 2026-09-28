#!/usr/bin/env bash
# The web service is the parent of every worker, so ITS PATH decides whether
# `opencode` resolves. Six traps, each hit for real:
#   1. a tmux start command runs in a NON-interactive shell and never sources
#      ~/.bashrc, so the export below is not optional;
#   2. `tmux kill-session` does not kill a server piped into tee and detached --
#      the stale process keeps port 8800, so new runs silently go to it;
#   3. items of GET /api/runs carry `id` and `status` but NO `alive` (verified
#      upstream shape), so liveness = status not in the terminal set, and a guard
#      written as r.get("alive") passes even with 8 runs in flight;
#   4. after a reboot there is no listener, so "panel unreadable" (-1) is the
#      ordinary case, not a busy box: refusing to start then is a deadlock;
#   5. `pgrep -f "lh-harness web"` also matches the tmux client and the pane's
#      `bash -c`, whose argv carries that literal string, so it names the wrong
#      pid. The server is identified by the port it owns instead;
#   6. the run list must reach python as a file argument: piped on stdin a heredoc
#      steals the stdin, and in the environment 94 runs exceed MAX_ARG_STRLEN.
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
command -v opencode >/dev/null || { echo "FATAL: opencode not on PATH"; exit 3; }

BUSY_JSON=$(mktemp); trap 'rm -f "$BUSY_JSON"' EXIT
curl -s http://127.0.0.1:8800/api/runs > "$BUSY_JSON"
# The run list travels as a FILE. Two shapes already failed for real: piping it
# on stdin while the program arrives as a heredoc (the heredoc wins the stdin, so
# a healthy panel scored -1), and passing it through the environment (94 runs
# blow past MAX_ARG_STRLEN and python dies with "Argument list too long").
BUSY=$(python3 - "$BUSY_JSON" <<'PY'
import json, sys
TERMINAL = {"completed", "failed", "cancelled", "blocked", "incomplete"}
try:
    runs = json.load(open(sys.argv[1], encoding="utf-8")).get("runs", [])
except Exception:
    print(-1)
    raise SystemExit
print(sum(1 for r in runs if r.get("status") and r["status"] not in TERMINAL))
PY
)
case "$BUSY" in ''|*[!0-9-]*) BUSY=-1 ;; esac
# -1 means the panel answered nothing, which is NOT the same fact as "runs are in
# flight". Folding them together made the script refuse to start the very service
# it was asked to start, and after a reboot that is the only state the box is in.
if [ "$BUSY" = "-1" ]; then
  OWNER=$(ss -ltn 2>/dev/null | grep -c ':8800 ')
  WORKERS=$(ps -eo cmd | grep -cE '[l]h-harness web|[o]pencode run')
  if [ "$OWNER" = "0" ] && [ "$WORKERS" = "0" ]; then
    echo "panel unreachable, port 8800 free, no worker: restart is safe"
    BUSY=0
  fi
fi
if [ "$BUSY" != "0" ]; then
  echo "ABORT: $BUSY 条 run 未到终态（-1 = 面板不可读）—— 先确认再重启"
  exit 4
fi

# Measured 2026-09-28: the server was pid 3157, `pgrep -f | head -1` named 3150.
port_owner() { ss -ltnp 2>/dev/null | grep ':8800 ' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2; }
pid=$(port_owner)
if [ -n "$pid" ]; then echo "kill stale web pid=$pid"; kill "$pid"; fi
tmux kill-session -t api 2>/dev/null
for i in $(seq 1 15); do [ -z "$(port_owner)" ] && break; sleep 1; done
[ -n "$(port_owner)" ] && { echo "ABORT: 旧进程还占着 8800"; exit 5; }

sleep 1
tmux new-session -d -s api "export PATH=\"$HOME/.npm-global/bin:$HOME/.local/bin:\$PATH\"; cd $HOME && $HOME/lh/bin/lh-harness web --runs-root $HOME/lh_runs --workspace-root $HOME/sbr_ws --port 8800 --no-open 2>&1 | tee $HOME/sbr/web8800.log; exec bash"
code=000
for i in $(seq 1 25); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8800/api/runs)
  [ "$code" = "200" ] && break
  sleep 1
done
pid=$(port_owner)
echo "api_http=$code pid=$pid busy=$BUSY"
if [ -n "$pid" ]; then tr "\0" "\n" < "/proc/$pid/environ" | grep "^PATH=" | sed "s|^|server |"; fi
