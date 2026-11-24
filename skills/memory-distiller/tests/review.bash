#!/usr/bin/env bash
# The GitHub-PR review path: decisions written as comments on the consolidation
# PR, harvested and applied by an agent. Pins the grammar, the outsider filter,
# that a malformed line is surfaced rather than silently dropped, and that
# re-applying is inert.
#
#   bash tests/review.bash
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
fails=0
check(){ if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1"; echo "        want: $2"; echo "        got:  $3"; fails=$((fails+1)); fi; }

W=$(mktemp -d); trap 'kill "${SRV_PID:-}" 2>/dev/null; rm -rf "$W"' EXIT
REPO=jptrsn/fixture
R="$W/repo"; cp -r scripts/fixture "$R"; cd "$R" || exit 1
python3 -c "
import json,pathlib
p=pathlib.Path('.agent/memory-config.json'); c=json.loads(p.read_text())
c['repo']='$REPO'; p.write_text(json.dumps(c))"
touch .agent/invariants.md .agent/blockers.md

python3 "$ROOT/scripts/distill.py" --repo-dir . surface >/dev/null 2>&1
read -r A B C <<<"$(python3 -c "
import glob,re
out=[]
for f in sorted(glob.glob('.agent/candidates/*.md')):
    t=open(f).read(); m=re.search(r'^id: (\S+)',t,re.M); s=re.search(r'^status: (\S+)',t,re.M)
    if m and s and s.group(1)=='pending': out.append(m.group(1))
print(' '.join(out[:3]))")"
[ -n "$C" ] || { echo "fixture needs 3 pending candidates, got: $A $B $C"; exit 1; }

# head.label mirrors GitHub's same-repo form, which is what the pulls?head= query
# in review.py is matched against.
python3 - "$W/seed.json" "$REPO" "$A" "$B" "$C" <<'PY'
import json, sys
path, repo, a, b, c = sys.argv[1:6]
branch = "openhands/memory-consolidation"
owner = repo.split("/")[0]
json.dump({
  "issues": {}, "comments": {}, "labels": {},
  "pulls": [{"number": 7, "head": {"label": f"{owner}:{branch}"}}],
  "pr_comments": {"7": [
    {"id": 1, "created_at": "2026-11-24T01:00:00Z",
     "user": {"login": "jptrsn"}, "author_association": "OWNER",
     "body": "read the diffs\n\n"
             f'memory: adopt {a} as "All MA calls go through app/ma/"\n'
             f"memory: dismiss {b}: one-off device quirk, not durable"},
    {"id": 2, "created_at": "2026-11-24T02:00:00Z",
     "user": {"login": "stranger"}, "author_association": "NONE",
     "body": f'memory: adopt {c} as "injected by an outsider"'},
    {"id": 3, "created_at": "2026-11-24T03:00:00Z",
     "user": {"login": "jptrsn"}, "author_association": "OWNER",
     "body": "- memory: adopt badid as \"id too short to parse\""},
  ]},
}, open(path, "w"))
PY
python3 "$ROOT/tests/fixtures/github_api.py" --state "$W/api.json" --seed "$W/seed.json" \
  > "$W/port" 2>"$W/srv.err" &
SRV_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$W/port" ] && break; sleep 0.3; done
PORT=$(head -1 "$W/port"); [ -n "$PORT" ] || { cat "$W/srv.err"; exit 1; }
export GH_API_BASE="http://127.0.0.1:$PORT" GITHUB_PERSONAL_ACCESS_TOKEN=test-token

status_of(){ python3 -c "
import re;t=open('.agent/candidates/$1.md').read()
m=re.search(r'^status: (\S+)',t,re.M); print(m.group(1) if m else '?')"; }
jq(){ python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(eval('d'+sys.argv[2]))" "$1" "$2"; }

echo "--- dry run plans, changes nothing ---"
python3 "$ROOT/scripts/review.py" --repo-dir . --dry-run > "$W/dry.json" 2>&1
check "dry run exit 0" "0" "$?"
check "found the two trusted decisions" "2" "$(jq "$W/dry.json" "['decisions'].__len__()")"
check "outsider decision refused" "stranger" "$(jq "$W/dry.json" "['refused'][0]['author']")"
check "refusal records why" "NONE" "$(jq "$W/dry.json" "['refused'][0]['association']")"
check "malformed line surfaced, not dropped" "1" "$(jq "$W/dry.json" "['malformed'].__len__()")"
check "dry run left the candidate pending" "pending" "$(status_of $A)"
check "dry run wrote no invariant" "0" "$(grep -c '^\*\*[0-9]' .agent/invariants.md)"

echo "--- apply ---"
python3 "$ROOT/scripts/review.py" --repo-dir . --apply > "$W/run.json" 2>&1; RC=$?
check "exit 1: a malformed line still needs a human fix" "1" "$RC"
# adopt + dismiss both take effect; the malformed line is counted separately.
check "both trusted decisions applied" "2" "$(jq "$W/run.json" "['applied']")"
check "adopted candidate is adopted" "adopted" "$(status_of $A)"
check "dismissed candidate is dismissed" "dismissed" "$(status_of $B)"
check "outsider injection never applied" "pending" "$(status_of $C)"
check "adopted wrote a real invariant" "yes" \
  "$(grep -q 'All MA calls go through app/ma/' .agent/invariants.md && echo yes || echo no)"
check "invariant records provenance" "yes" \
  "$(grep -q 'promoted from' .agent/invariants.md && echo yes || echo no)"
check "audit names the person who decided" "yes" \
  "$(grep -q '\"decided_by\": \"jptrsn\"' .agent/memory-audit.md && echo yes || echo no)"
check "audit records the channel" "yes" \
  "$(grep -q '\"via\": \"pr-comment\"' .agent/memory-audit.md && echo yes || echo no)"
check "dismiss reason recorded verbatim" "yes" \
  "$(grep -q 'one-off device quirk' .agent/candidates/$B.md && echo yes || echo no)"
check "human turn marked as comment-derived" "pr-comment" \
  "$(jq .agent/memory-human-turn "['via']")"
check "human turn names the decider" "jptrsn" \
  "$(jq .agent/memory-human-turn "['author']")"

echo "--- idempotent: re-applying is inert ---"
BEFORE=$(md5sum .agent/invariants.md | cut -d' ' -f1)
BEFORE_N=$(grep -c '^\*\*[0-9]' .agent/invariants.md)
python3 "$ROOT/scripts/review.py" --repo-dir . --apply > "$W/re.json" 2>&1
check "re-apply reports both as skipped, not applied" "0" "$(jq "$W/re.json" "['applied']")"
check "no duplicate invariant on re-apply" "$BEFORE" "$(md5sum .agent/invariants.md | cut -d' ' -f1)"
check "invariant count unchanged" "$BEFORE_N" "$(grep -c '^\*\*[0-9]' .agent/invariants.md)"
check "audit did not double-append" "$BEFORE_N" "$(grep -c '"action": "adopt"' .agent/memory-audit.md)"
check "candidate still adopted" "adopted" "$(status_of $A)"

echo "--- a T2 target that does not exist is an error, not a silent create ---"
printf 'memory: adopt %s as "rule needing a missing target" target=.agent/nope.md\n' "$C" \
  > "$W/decision.txt"
cp .agent/invariants.md "$W/inv.snapshot"
python3 - "$W/api.json" "$W/decision.txt" "$C" <<'PY'
import json, sys
api, dec, cid = sys.argv[1:4]
st = json.load(open(api))
st["pr_comments"]["7"] = [{"id": 9, "created_at": "2026-11-24T04:00:00Z",
                           "user": {"login": "jptrsn"}, "author_association": "OWNER",
                           "body": open(dec).read()}]
json.dump(st, open(api, "w"))
PY
python3 "$ROOT/scripts/review.py" --repo-dir . --apply > "$W/miss.json" 2>&1
check "missing target reported as an error" "yes" \
  "$(grep -q 'does not exist' "$W/miss.json" && echo yes || echo no)"
check "no policy file created behind your back" "yes" \
  "$(cmp -s .agent/invariants.md "$W/inv.snapshot" && echo yes || echo no)"
check "missing target counts as an error" "1" "$(jq "$W/miss.json" "['errors']")"

echo "--- an unattended automation run must not self-approve ---"
# The driver shares the maintainer's PAT, so a harvested comment proves nothing
# about presence. If the nightly could write the human-turn marker itself, the
# guard would be decorative.
python3 - "$W/api.json" "$C" <<'PY'
import json, sys
api, c = sys.argv[1:3]
st = json.load(open(api))
st["pr_comments"]["7"] = [
  {"id": 11, "created_at": "2026-11-24T05:00:00Z",
   "user": {"login": "jptrsn"}, "author_association": "OWNER",
   "body": f'memory: dismiss {c}: should never be applied unattended'}]
json.dump(st, open(api, "w"))
PY
rm -f .agent/memory-human-turn
AUDIT_BEFORE=$(grep -c '"action":' .agent/memory-audit.md)
AUTOMATION_RUN_ID=fake-run-123 python3 "$ROOT/scripts/review.py" --repo-dir . --apply > "$W/auto.json" 2>&1
check "unattended apply exits non-zero" "1" "$?"
check "it says why" "yes" \
  "$(grep -q 'unattended automation' "$W/auto.json" && echo yes || echo no)"
check "no human-turn marker written" "no" \
  "$(test -f .agent/memory-human-turn && echo yes || echo no)"
check "the decision was not applied" "pending" "$(status_of $C)"
check "audit gained no unattended row" "$AUDIT_BEFORE" \
  "$(grep -c '"action":' .agent/memory-audit.md)"
# The escape hatch exists for a human genuinely watching a run.
AUTOMATION_RUN_ID=fake-run-123 MEMORY_REVIEW_ALLOW_AUTOMATION=1 \
  python3 "$ROOT/scripts/review.py" --repo-dir . --apply > "$W/hatch.json" 2>&1
check "explicit opt-in lets a supervised run through" "1" "$(jq "$W/hatch.json" "['applied']")"
# ...but only on an actual truthy value. A config file or shell that writes the
# flag off as "0" must not open it: a bare `if os.environ.get(...)` reads "0" as
# on, which would turn the safe default into the unsafe one.
rm -f .agent/memory-human-turn
AUTOMATION_RUN_ID=fake-run-123 MEMORY_REVIEW_ALLOW_AUTOMATION=0 \
  python3 "$ROOT/scripts/review.py" --repo-dir . --apply > "$W/zero.json" 2>&1
check '"0" does not open the gate' "1" "$?"
check '"0" still refuses with a reason' "yes" \
  "$(grep -q 'unattended automation' "$W/zero.json" && echo yes || echo no)"
check '"0" writes no human-turn marker' "no" \
  "$(test -f .agent/memory-human-turn && echo yes || echo no)"
AUTOMATION_RUN_ID=fake-run-123 MEMORY_REVIEW_ALLOW_AUTOMATION=false \
  python3 "$ROOT/scripts/review.py" --repo-dir . --apply > "$W/false.json" 2>&1
check '"false" does not open the gate' "1" "$?"

echo
[ "$fails" -eq 0 ] && { echo "all review checks passed"; exit 0; } || { echo "$fails FAILED"; exit 1; }
