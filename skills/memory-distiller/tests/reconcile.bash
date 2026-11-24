#!/usr/bin/env bash
# Label/file drift. The nightly run labels an issue `memory-ingested` when it
# writes the thread file, but that file only lands on the default branch if a
# human merges the consolidation PR. If it is closed instead, the label survives
# and the content never does — and since that label is what keeps issues out of
# the queue, the issue would be invisible forever. reconcile() repairs that.
#
#   bash tests/reconcile.bash
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"; fails=0
check(){ if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1"; echo "        want: $2"; echo "        got:  $3"; fails=$((fails+1)); fi; }

W=$(mktemp -d); trap 'kill "${SRV_PID:-}" 2>/dev/null; rm -rf "$W"' EXIT

# 70 labelled ingested but NO thread file   -> orphan, must be reclaimed
# 71 labelled ingested WITH a thread file    -> correct, must be untouched
# 72 has a thread file, no ingested label    -> must be relabelled (opt-in scan)
cat > "$W/seed.json" <<'JSON'
{
  "issues": {
    "70": {"number": 70, "title": "PR closed, content lost", "state": "open",
           "comments": 2, "labels": [{"name": "memory-ingested"}, {"name": "area:audio"}]},
    "71": {"number": 71, "title": "Correctly ingested", "state": "closed",
           "comments": 1, "labels": [{"name": "memory-ingested"}]},
    "72": {"number": 72, "title": "File exists, label lost", "state": "open",
           "comments": 1, "labels": []}
  },
  "comments": {},
  "labels": {"memory-needs-review": {"name": "memory-needs-review"},
             "memory-ingested": {"name": "memory-ingested"}}
}
JSON
python3 tests/fixtures/github_api.py --state "$W/api.json" --seed "$W/seed.json" > "$W/port" 2>"$W/srv.err" &
SRV_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$W/port" ] && break; sleep 0.3; done
PORT=$(head -1 "$W/port")
[ -n "$PORT" ] || { echo "fixture server failed:"; cat "$W/srv.err"; exit 1; }
export GH_API_BASE="http://127.0.0.1:$PORT" GITHUB_PERSONAL_ACCESS_TOKEN=test-token

R="$W/repo"; mkdir -p "$R/.agent/threads"
cat > "$R/.agent/memory-config.json" <<'JSON'
{"repo": "jptrsn/fixture", "tiers": {}}
JSON
# Only 71 and 72 have real thread files on disk.
echo "# 71" > "$R/.agent/threads/71.md"
echo "# 72" > "$R/.agent/threads/72.md"

labels_of(){ python3 - "$W/api.json" "$1" <<'PY'
import json,sys
st=json.load(open(sys.argv[1]))
print(",".join(sorted(l["name"] for l in st["issues"][sys.argv[2]]["labels"])))
PY
}

echo "--- dry run changes nothing ---"
python3 scripts/labels.py --repo-dir "$R" reconcile --dry-run > "$W/dry.json" 2>&1
check "dry run names the orphan" "[70]" \
  "$(python3 -c "import json;print(json.load(open('$W/dry.json'))['reclaimed_to_pending'])" 2>/dev/null)"
check "dry run left #70 alone" "area:audio,memory-ingested" "$(labels_of 70)"
check "dry run reported no writes" "True" \
  "$(python3 -c "import json;print(json.load(open('$W/dry.json'))['dry_run'])" 2>/dev/null)"

echo "--- real run ---"
python3 scripts/labels.py --repo-dir "$R" reconcile --include-unlabelled > "$W/run.json" 2>&1
check "exit 0" "0" "$?"
check "reclaimed the orphan" "[70]" \
  "$(python3 -c "import json;print(json.load(open('$W/run.json'))['reclaimed_to_pending'])" 2>/dev/null)"
check "orphan back in the pending queue" "area:audio,memory-needs-review" "$(labels_of 70)"
check "foreign label preserved on reclaim" "yes" \
  "$(labels_of 70 | grep -q 'area:audio' && echo yes || echo no)"
check "correctly-ingested #71 untouched" "memory-ingested" "$(labels_of 71)"
check "file-but-no-label #72 relabelled" "[72]" \
  "$(python3 -c "import json;print(json.load(open('$W/run.json'))['relabelled_ingested'])" 2>/dev/null)"
check "#72 now carries the ingested label" "memory-ingested" "$(labels_of 72)"

echo "--- convergence: a second run is a no-op ---"
python3 scripts/labels.py --repo-dir "$R" reconcile --include-unlabelled > "$W/run2.json" 2>&1
check "no further reclaims" "[]" \
  "$(python3 -c "import json;print(json.load(open('$W/run2.json'))['reclaimed_to_pending'])" 2>/dev/null)"
check "no further relabels" "[]" \
  "$(python3 -c "import json;print(json.load(open('$W/run2.json'))['relabelled_ingested'])" 2>/dev/null)"

echo "--- the reclaimed issue is selectable again ---"
python3 scripts/threads.py --repo-dir "$R" --dry-run > "$W/q.json" 2>&1
check "reclaimed #70 is back in the queue" "yes" \
  "$(grep -q '"issue": 70' "$W/q.json" && echo yes || echo no)"

echo
[ "$fails" -eq 0 ] && { echo "all reconcile checks passed"; exit 0; } || { echo "$fails FAILED"; exit 1; }
