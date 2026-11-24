#!/usr/bin/env bash
# Label state machine: selection, transition, and preservation of foreign labels.
# Runs threads.py and labels.py against tests/fixtures/github_api.py, so nothing
# touches a real repository.
#
#   bash tests/labels.bash
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
fails=0

check() { # name, expected, actual
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else
    echo "  FAIL $1"; echo "        expected: $2"; echo "        actual:   $3"; fails=$((fails+1)); fi
}

WORK=$(mktemp -d)
trap 'kill "${SRV_PID:-}" 2>/dev/null; rm -rf "$WORK"' EXIT

# --- seed: 3 issues -------------------------------------------------------
# 61: pending report, plus a FOREIGN label the tool must never drop
# 62: already ingested (must not be re-selected by the queue query)
# 63: no labels at all (must not be picked up by the queue query)
cat > "$WORK/seed.json" <<'JSON'
{
  "issues": {
    "61": {"number": 61, "title": "Red screen on pushed-route drawer nav",
           "state": "closed", "comments": 1,
           "labels": [{"name": "memory-needs-review"}, {"name": "area:ui"}]},
    "62": {"number": 62, "title": "Already distilled",
           "state": "closed", "comments": 1,
           "labels": [{"name": "memory-ingested"}]},
    "63": {"number": 63, "title": "Never reported",
           "state": "open", "comments": 0, "labels": []}
  },
  "comments": {},
  "labels": {"memory-needs-review": {"name": "memory-needs-review"},
             "memory-ingested": {"name": "memory-ingested"}}
}
JSON

python3 tests/fixtures/github_api.py --state "$WORK/api.json" --seed "$WORK/seed.json" \
  > "$WORK/port" 2>"$WORK/srv.err" &
SRV_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$WORK/port" ] && break; sleep 0.3; done
PORT=$(head -1 "$WORK/port")
[ -n "$PORT" ] || { echo "fixture server failed to start:"; cat "$WORK/srv.err"; exit 1; }

export GH_API_BASE="http://127.0.0.1:$PORT"
export GITHUB_PERSONAL_ACCESS_TOKEN=test-token
REPO=jptrsn/fixture

labels_of() {
  python3 - "$WORK/api.json" "$1" <<'PY'
import json,sys
st=json.load(open(sys.argv[1]))
print(",".join(sorted(l["name"] for l in st["issues"][sys.argv[2]]["labels"])))
PY
}

mkdir -p "$WORK/repo/.agent"
printf '{"repo":"%s"}' "$REPO" > "$WORK/repo/.agent/memory-config.json"

echo "--- ensure ---"
python3 scripts/labels.py --repo-dir "$WORK/repo" --repo "$REPO" ensure >/dev/null 2>&1
check "ensure is idempotent for existing labels" "0" "$?"

echo "--- selection: queue label only ---"
out=$(python3 scripts/threads.py --repo-dir "$WORK/repo" --repo "$REPO" --state all \
        --dry-run 2>&1)
scanned=$(echo "$out" | python3 -c "import json,sys;print(json.load(sys.stdin)['issues_scanned'])")
# Only #61 carries memory-needs-review, so it is the only candidate scanned.
check "queue query returns exactly the pending issue" "1" "$scanned"

echo "--- ingest + transition ---"
out=$(python3 scripts/threads.py --repo-dir "$WORK/repo" --repo "$REPO" --state all 2>&1)
echo "$out" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['details'][0].get('labels',''))" \
  > "$WORK/labres"
check "label write reported ok" "ok" "$(cat "$WORK/labres")"
check "pending label removed, ingested added" \
  "area:ui,memory-ingested" "$(labels_of 61)"
check "foreign label (area:ui) preserved" \
  "yes" "$(labels_of 61 | grep -q 'area:ui' && echo yes || echo no)"
check "ingested issue not re-selected by queue" \
  "0" "$(python3 scripts/threads.py --repo-dir "$WORK/repo" --repo "$REPO" --state all \
          --dry-run 2>&1 | python3 -c "import json,sys;print(json.load(sys.stdin)['issues_scanned'])")"

echo "--- idempotence: second run writes nothing new ---"
check "second run reports 0 written" "0" \
  "$(python3 scripts/threads.py --repo-dir "$WORK/repo" --repo "$REPO" --state all --dry-run 2>&1 \
     | python3 -c "import json,sys;print(json.load(sys.stdin)['threads_written'])")"

echo "--- re-open: posting another report makes it pending again ---"
# Guard the fixture itself: a follow-up body under MIN_REPORT_CHARS (600) would
# be rejected and every assertion below would fail for the wrong reason.
python3 - "$WORK/api.json" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
body = st["comments"]["61"][-1]["body"]
assert len(body) >= 700, f"follow-up fixture too short to be a report: {len(body)}"
PY
[ "$?" -eq 0 ] || { echo "  FAIL follow-up fixture too short to be a report"; fails=$((fails+1)); }
python3 - "$WORK/api.json" <<'PY'
import json,sys
p=sys.argv[1]; st=json.load(open(p))
st["issues"]["61"]["labels"]=[{"name":"memory-ingested"},{"name":"memory-needs-review"},{"name":"area:ui"}]
st["comments"]["61"].append({"id":4242,"user":{"login":"jptrsn"},
  "created_at":"2026-11-24T09:00:00Z",
  "body":"## Host device pass 2026-11-24 — follow-up verification\n\nRe-ran the reproduction steps on the Pixel 6 in a debug build after the eager-read fix.\n\nObserved no red screen across ten theme toggles and three hot reloads; the\nenvironment was the shared test server, paused before and after per the audio\nsafety rules. Verified the drawer still opens from a pushed route and that\nSettings receives the captured config. Root cause confirmed as the deferred\nclosure described in the previous report; no further device work outstanding.\nGates: flutter analyze, dart format, and flutter test --exclude-tags live all\nclean on this revision. Severity is unchanged and the issue can be closed once\nthe regression test from the earlier report is merged.\n"})
json.dump(st,open(p,"w"),indent=2)
PY
out=$(python3 scripts/threads.py --repo-dir "$WORK/repo" --repo "$REPO" --state all 2>&1)
check "re-pending issue re-ingested" "1" \
  "$(echo "$out" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['details']))")"
check "both labels correct after re-ingest" \
  "area:ui,memory-ingested" "$(labels_of 61)"
check "thread file grew (append, not overwrite)" \
  "yes" "$(grep -q "Host device pass 2026-11-24" "$WORK/repo/.agent/threads/61.md" \
            && grep -q "Host device pass 2026-11-23" "$WORK/repo/.agent/threads/61.md" \
            && echo yes || echo no)"

echo "--- status buckets ---"
st=$(python3 scripts/labels.py --repo-dir "$WORK/repo" --repo "$REPO" status --state all 2>&1)
check "ingested bucket counts #61 and #62" "2" \
  "$(echo "$st" | python3 -c "import json,sys;print(json.load(sys.stdin)['ingested'])")"
check "unlabelled bucket counts #63" "1" \
  "$(echo "$st" | python3 -c "import json,sys;print(json.load(sys.stdin)['unlabelled'])")"
check "state=all really scans all (3)" "3" \
  "$(echo "$st" | python3 -c "import json,sys;print(json.load(sys.stdin)['issues_scanned'])")"
# Regression: omitting state would make GitHub default to open and hide #61/#62.
stopen=$(python3 scripts/labels.py --repo-dir "$WORK/repo" --repo "$REPO" status --state open 2>&1)
check "state=open scans only the open issue" "1" \
  "$(echo "$stopen" | python3 -c "import json,sys;print(json.load(sys.stdin)['issues_scanned'])")"

echo "--- show ---"
check "show reports ingested state" "ingested" \
  "$(python3 scripts/labels.py --repo-dir "$WORK/repo" --repo "$REPO" show --issue 61 2>&1 \
     | python3 -c "import json,sys;print(json.load(sys.stdin)['pipeline'])")"

echo "--- --no-labels backfill leaves labels untouched ---"
before=$(labels_of 63)
python3 scripts/threads.py --repo-dir "$WORK/repo" --repo "$REPO" --state all --no-labels \
  >/dev/null 2>&1
check "--no-labels does not relabel" "$before" "$(labels_of 63)"

echo
if [ "$fails" -eq 0 ]; then echo "all label checks passed"; exit 0; fi
echo "$fails FAILED"; exit 1
