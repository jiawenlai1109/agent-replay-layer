#!/usr/bin/env bash
# rep-12 sweep over the four injectable families.
#  * skips cells that already hold a VALID (metrics.steps > 0) manifest -- the
#    21:04 attempt wrote four dead ones (service PATH lacked opencode);
#  * derives the recipe id from the task id in ONE step, and refuses to run with
#    an empty one (a first draft produced "-csv-short" and would have failed all
#    seven runs in argparse).
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"
cd "$HOME/sbr"
REP=12
TASKS="T-PIPE-01F-C1-csv-short T-PIPE-01F-C2-stale-summary T-PIPE-01F-C3-dup-key T-PIPE-01F-C4-region-typo"

valid_cell() {
  python3 - "$1" "$2" "$REP" <<PY
import glob, json, re, sys
task, arm, rep = sys.argv[1], sys.argv[2], sys.argv[3]
pat = re.compile("-%s%s-\d{6}$" % (arm, rep))
for p in glob.glob("manifests/*.json"):
    rid = p.split("/")[-1][:-5]
    try:
        m = json.load(open(p))
    except Exception:
        continue
    if m.get("task_id") != task or not pat.search(rid):
        continue
    if (m.get("metrics") or {}).get("steps"):
        print(rid)
        sys.exit(0)
sys.exit(1)
PY
}

echo "### rep$REP sweep $(date +%T)"
for arm in C-ROLL C-REC; do
  for task in $TASKS; do
    recipe=$(sed -n "s/^T-PIPE-01F-\(C[0-9]-.*\)$/\1/p" <<<"$task")
    [ -n "$recipe" ] || { echo "ABORT: cannot derive recipe for $task"; exit 6; }
    if out=$(valid_cell "$task" "$arm"); then
      echo "SKIP $task $arm (valid sample $out)"
      continue
    fi
    echo "### $task / $recipe / $arm rep=$REP $(date +%T)"
    timeout 1200 python3 arm_c.py --task "$task" --recipe "$recipe" --arm "$arm" \
        --rep "$REP" --inject-after 6 --max-rounds 4 --minutes 12
    rc=$?
    echo "   rc=$rc $(date +%T)"
    [ "$rc" -eq 3 ] && { echo "ABORT: provider credential dead"; exit 3; }
  done
done
echo "SWEEP_DONE $(date +%T)"
