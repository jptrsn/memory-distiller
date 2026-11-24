#!/usr/bin/env bash
# Nightly driver against a throwaway LOCAL bare repo: clone, branch, scoped add,
# commit, idempotence, and token hygiene. Never contacts GitHub.
#
#   bash tests/nightly.bash
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"; fails=0
# Scrub inherited config first: this suite runs in a shell that may already
# export MEMORY_* from an earlier session, and a leaked MEMORY_REPOS makes every
# assertion below meaningless.
unset MEMORY_REPOS MEMORY_DRY_RUN GITHUB_PERSONAL_ACCESS_TOKEN
check(){ if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1"; echo "        want: $2"; echo "        got:  $3"; fails=$((fails+1)); fi; }

W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
REMOTE="$W/remote.git"; SEED="$W/seed"

git init -q --bare "$REMOTE"
git init -q "$SEED"
git -C "$SEED" checkout -q -b develop
git -C "$SEED" config user.email t@t; git -C "$SEED" config user.name t
mkdir -p "$SEED/.agent/reports" "$SEED/.agents/skills"
cp -r "$ROOT/scripts/fixture/.agent/reports/." "$SEED/.agent/reports/"
# Exclude __pycache__: a real skill install has none, and leaving them in the
# seed would make the "no bytecode committed" assertion test the fixture rather
# than the driver.
cp -r "$ROOT" "$SEED/.agents/skills/memory-distiller"
find "$SEED/.agents/skills/memory-distiller" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$ROOT/scripts/fixture/.agent/memory-config.json" "$SEED/.agent/memory-config.json"
python3 -c "
import json,sys
p=sys.argv[1]; c=json.load(open(p)); c['repo']='local/fixture'
json.dump(c, open(p,'w'), indent=2)" "$SEED/.agent/memory-config.json"
git -C "$SEED" add -A && git -C "$SEED" commit -qm init
git -C "$SEED" push -q "$REMOTE" develop

# Second remote with the esp-airsync shape: `main` and `feature/x`, no develop.
git init -q --bare "$W/nodev.git"
git init -q "$W/seed2"
git -C "$W/seed2" checkout -q -b main
git -C "$W/seed2" config user.email t@t; git -C "$W/seed2" config user.name t
echo x > "$W/seed2/README"
git -C "$W/seed2" add -A && git -C "$W/seed2" commit -qm one
git -C "$W/seed2" branch feature/x
git -C "$W/seed2" push -q "$W/nodev.git" main feature/x
NO_DEV_REMOTE="$W/nodev.git"

export GITHUB_PERSONAL_ACCESS_TOKEN=not-a-real-token
export MEMORY_REPOS="$REMOTE"
# Pin it: a leaked MEMORY_DRY_RUN from the caller's environment would silently
# skip the push and make the steady-state assertions below meaningless.
export MEMORY_DRY_RUN=0
export TMPDIR="$W/tmp"; mkdir -p "$TMPDIR"

field(){ python3 -c "
import json,sys
try: print(json.load(open(sys.argv[1]))['results'][0].get(sys.argv[2]))
except Exception: print('ERR')" "$1" "$2"; }

echo "--- first run ---"
python3 automation/main.py > "$W/out.json" 2>"$W/err"
check "driver exits 0" "0" "$?"
check "status ok" "ok" "$(field "$W/out.json" status)"
check "candidates surfaced" "yes" \
  "$([ "$(field "$W/out.json" candidates_written)" -gt 0 ] 2>/dev/null && echo yes || echo no)"
check "run 1 reports a PR step (local repo, no GitHub)" "local repo (PR step skipped)" \
  "$(field "$W/out.json" pr)"

CLONE="$TMPDIR/memory-nightly/remote"
[ -d "$CLONE" ] || { echo "  FAIL clone dir missing"; fails=$((fails+1)); }

echo "--- commit shape ---"
check "on consolidation branch" "openhands/memory-consolidation" \
  "$(git -C "$CLONE" rev-parse --abbrev-ref HEAD 2>/dev/null)"
stray=$(git -C "$CLONE" show --name-only --pretty=format: HEAD 2>/dev/null \
        | grep -v '^$' | grep -vE '^(\.agent/|\.agents/)' || true)
check "commit touches only memory paths" "" "$stray"
check "commit message names the generator" "yes" \
  "$(git -C "$CLONE" log -1 --pretty=%B 2>/dev/null | grep -q 'nightly consolidation' && echo yes || echo no)"

echo "--- idempotence: a second run changes nothing ---"
python3 automation/main.py > "$W/out2.json" 2>/dev/null
# Steady state: run 2 clones the branch run 1 pushed, so surface() finds the
# candidates already on disk, writes nothing, and reports unchanged.
check "re-run reports unchanged" "unchanged" "$(field "$W/out2.json" pr)"
check "re-run wrote no candidates" "0" "$(field "$W/out2.json" candidates_written)"
echo "--- convergence: a third run must not add a commit ---"
# Not "exactly one commit": the first run legitimately commits a few one-time
# files (the thread ledger, the audit log). The property that matters is that
# commits STOP — a run that always finds the tree dirty force-pushes a fresh
# empty commit every night, forever.
c2=$(git -C "$REMOTE" rev-list --count refs/heads/openhands/memory-consolidation 2>/dev/null)
python3 automation/main.py > "$W/out3.json" 2>/dev/null
c3=$(git -C "$REMOTE" rev-list --count refs/heads/openhands/memory-consolidation 2>/dev/null)
check "run 3 adds no commit" "$c2" "$c3"
check "run 3 reports unchanged" "unchanged" "$(field "$W/out3.json" pr)"
check "no byte-compiled files committed" "" \
  "$(git -C "$REMOTE" ls-tree -r --name-only refs/heads/openhands/memory-consolidation 2>/dev/null | grep '__pycache__\|\.pyc$' || true)"

echo "--- new evidence on base must still be distilled ---"
# The clone prefers the consolidation branch so surface() sees the candidates it
# already proposed. The T0 reports ride along from that branch's commit, so if
# the base moved since, the new reports are invisible and the run reports
# `unchanged` forever. The fix reads T0 from base while keeping candidates from
# the branch; this is the check that would fail without it.
cat > "$SEED/.agent/reports/unit-03-fresh-report.md" <<'RPT'
# Unit report — unit-03 (added after the branch existed)

## OBSERVED — actions you took and results you saw

- `test_freshly_added_route_returns_json` (TestClient `GET /api/v1/things`)
  → 200, body `{"things": []}`.

## ASSUMPTIONS

- The `/api/v1/things` collection is unordered; clients sort client-side.
RPT
git -C "$SEED" add -A && git -C "$SEED" commit -qm "add unit-03 report"
git -C "$SEED" push -q "$REMOTE" develop
python3 automation/main.py > "$W/out4.json" 2>/dev/null
check "a new base report is not silently ignored" "1" \
  "$(python3 -c "
import json; d=json.load(open('$W/out4.json'))['results'][0]
print(1 if d.get('candidates_written',0) > 0 and d.get('pr') != 'unchanged' else 0)")"
check "the new candidate reached the branch" "yes" \
  "$(git -C "$REMOTE" ls-tree -r --name-only refs/heads/openhands/memory-consolidation 2>/dev/null | grep -q 'unit-03' && echo yes || echo no)"
check "run 5 converges back to unchanged" "unchanged" \
  "$(python3 automation/main.py 2>/dev/null | python3 -c "
import json,sys; print(json.load(sys.stdin)['results'][0].get('pr'))")"

echo "--- the nightly run must not commit T2 ---"
# Fault injection: consolidate() executes the distill.py *inside the clone*, so
# patching that copy simulates a surface() bug that reaches for adopted memory.
# This is the case the commit allowlist exists to catch; without the guard the
# T2 write would ride out on the automated PR.
git -C "$SEED" checkout -q develop
python3 tests/inject_t2_leak.py \
  "$SEED/.agents/skills/memory-distiller/scripts/distill.py"
git -C "$SEED" add -A && git -C "$SEED" commit -qm inject && git -C "$SEED" push -q "$REMOTE" develop
# Drop the consolidation branch so the run re-derives it from the patched develop.
git -C "$REMOTE" update-ref -d refs/heads/openhands/memory-consolidation
python3 automation/main.py > "$W/out4.json" 2>/dev/null

# The bug must have actually fired, or the checks below would pass vacuously.
# It cannot pass silently: `refused_paths` is only populated if the file was
# really written and staged, so a fixture that never fires fails there.
check "injected bug was caught and reported" "yes" \
  "$(python3 -c "
import json;r=json.load(open('$W/out4.json'))['results'][0]
print('yes' if any('invariants' in x for x in r.get('refused_paths',[])) else 'no')" 2>/dev/null)"
# The guard now removes the path from the worktree too, not just the index: a
# file left on disk is one `add -A` away from riding out on a later run.
check "refused T2 removed from the worktree" "no" \
  "$(test -f "$TMPDIR/memory-nightly/remote/.agent/invariants.md" && echo yes || echo no)"
nightly_touched=$(git -C "$REMOTE" show --name-only --pretty=format: refs/heads/openhands/memory-consolidation 2>/dev/null | grep -v '^$')
check "nightly commit introduces no T2 path" "" \
  "$(echo "$nightly_touched" | grep -E '\.agent/(invariants|blockers)\.md|^\.agent/memory/' || true)"
check "candidates still shipped despite the refusal" "yes" \
  "$(git -C "$REMOTE" ls-tree -r --name-only refs/heads/openhands/memory-consolidation 2>/dev/null | grep -q '\.agent/candidates/' && echo yes || echo no)"

echo "--- base branch resolution ---"
# A remote whose only branches are `main` and `feature/x`: proves the base comes
# from the remote's real head list, that @branch can point anywhere that exists,
# and that @branch naming a branch that does NOT exist is rejected up front
# rather than failing later inside `git clone`.
check "resolves the remote's own HEAD" "main" \
  "$(python3 -c "
import sys; sys.path.insert(0,'$ROOT/automation')
import os; os.environ['GITHUB_PERSONAL_ACCESS_TOKEN']='fake'
import main
print(main.base_branch('$NO_DEV_REMOTE','', main.write_cred_helper('$W/bb')))
" 2>&1 | tail -1)"
check "@branch override wins" "feature/x" \
  "$(python3 -c "
import sys; sys.path.insert(0,'$ROOT/automation')
import os; os.environ['GITHUB_PERSONAL_ACCESS_TOKEN']='fake'
import main
print(main.base_branch('$NO_DEV_REMOTE','feature/x', main.write_cred_helper('$W/bb')))
" 2>&1 | tail -1)"
check "@branch naming a missing branch errors, not silently clones" "errors" \
  "$(python3 -c "
import sys; sys.path.insert(0,'$ROOT/automation')
import os; os.environ['GITHUB_PERSONAL_ACCESS_TOKEN']='fake'
import main
try:
    main.base_branch('$NO_DEV_REMOTE','develop', main.write_cred_helper('$W/bb'))
    print('silent')
except RuntimeError:
    print('errors')
" 2>&1 | tail -1)"

echo "--- token hygiene ---"
check "token absent from .git/config" "" \
  "$(grep -l 'not-a-real-token' "$CLONE/.git/config" 2>/dev/null || true)"
check "token absent from git log" "" \
  "$(git -C "$CLONE" log --all --pretty=%B 2>/dev/null | grep 'not-a-real-token' || true)"
check "credential helper is 0700" "700" \
  "$(stat -c %a "$TMPDIR/memory-nightly/git-cred.sh" 2>/dev/null)"

echo
[ "$fails" -eq 0 ] && { echo "all nightly checks passed"; exit 0; } || { echo "$fails FAILED"; exit 1; }
