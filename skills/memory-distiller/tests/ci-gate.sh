#!/usr/bin/env bash
# Dependency-free runner for the ci-gate cases (same assertions as ci-gate.bats).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fails=0
check() { # name, files_content, expected
  local name="$1" content="$2" want="$3"
  local out; out=$(mktemp); local src; src=$(mktemp)
  printf '%s' "$content" > "$src"
  GITHUB_OUTPUT="$out" bash scripts/ci-gate.sh --files-from "$src" >/dev/null 2>&1
  local got; got=$(cat "$out")
  if [ "$got" = "memory_only=$want" ]; then echo "  ok   $name"; else
    echo "  FAIL $name (got '$got', want 'memory_only=$want')"; fails=$((fails+1)); fi
  rm -f "$out" "$src"
}
check "pure memory PR skips gates" \
  "$(printf '.agent/candidates/a.md\n.agent/archive/x--b.md\n.agent/threads/64.md\n')" true
check "code PR runs gates" "app/main.py" false
check "mixed PR runs gates" \
  "$(printf '.agent/candidates/a.md\napp/main.py\n')" false
check "T2 invariants.md is gated, not exempt" ".agent/invariants.md" false
check "blockers.md is gated" ".agent/blockers.md" false
check "memory docs are exempt" ".agents/skills/memory-distiller/SKILL.md" true
check "any installed skill is exempt" ".agents/skills/other-skill/scripts/x.py" true
check "T2 under .agent/ still gated (not caught by .agents rule)" \
  "$(printf '.agent/invariants.md')" false
check "docs/memory is exempt" "docs/memory/tiers.md" true
check "empty list is fail-closed" "" false
check "missing file is fail-closed" "" false
# API path without token must fail closed, not error
out=$(mktemp); GITHUB_OUTPUT="$out" GH_TOKEN= PR_FILES_URL= bash scripts/ci-gate.sh >/dev/null 2>&1
[ "$(cat "$out")" = "memory_only=false" ] && echo "  ok   no-token API path is fail-closed" || { echo "  FAIL no-token"; fails=$((fails+1)); }
rm -f "$out"
[ "$fails" -eq 0 ] && { echo "all ci-gate checks passed"; exit 0; } || { echo "$fails FAILED"; exit 1; }
