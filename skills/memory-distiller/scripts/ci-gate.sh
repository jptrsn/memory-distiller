#!/usr/bin/env bash
# Decide whether a pull request touches anything other than memory artifacts.
#
# Used by the CI gate job so a nightly memory-consolidation PR does not burn a
# full test run on files no gate can check. Prints memory_only=true|false to
# GITHUB_OUTPUT.
#
# FAIL-CLOSED by design: anything unexpected yields memory_only=false, so the
# real gates run. A workflow job that skips reports "skipped", and a *skipped*
# required status check satisfies branch protection — so an error here must
# never be able to report true, or it becomes a way to bypass the gates.
#
#   ci-gate.sh                 # reads the PR file list from the GitHub API
#   ci-gate.sh --files-from F  # reads a newline list from F ("-" for stdin)
set -uo pipefail

# `.agents/` (plural) is the OpenHands skill install location: tooling the repo
# ships rather than code it owns, so exempt wholesale. `.agent/` (singular) is
# this system's data area, where only the append-only subdirs are exempt — T2
# files like .agent/invariants.md stay gated. The two differ by one character.
MEMORY_RE='^(\.agent/(candidates|archive|threads)/|\.agents/|docs/memory/)'

out() {
  # Append to GITHUB_OUTPUT when present; always echo for local review.
  echo "memory_only=$1"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "memory_only=$1" >>"$GITHUB_OUTPUT"
  fi
}

files=""
if [ "${1:-}" = "--files-from" ]; then
  src="${2:--}"
  if [ "$src" = "-" ]; then files=$(cat); else files=$(cat "$src" 2>/dev/null) || files=""; fi
else
  if [ -z "${GH_TOKEN:-}" ] || [ -z "${PR_FILES_URL:-}" ]; then
    echo "ci-gate: GH_TOKEN and PR_FILES_URL must be set; running full gates" >&2
    out false
    exit 0
  fi
  # Never let the API call decide the outcome on error: capture, do not abort.
  files=$(gh api "$PR_FILES_URL?per_page=100" --paginate -q '.[].filename' 2>/dev/null) || files=""
fi

if [ -z "${files//[[:space:]]/}" ]; then
  echo "ci-gate: no file list; running full gates" >&2
  out false
  exit 0
fi

if printf '%s\n' "$files" | grep -qvE "$MEMORY_RE"; then
  out false
else
  out true
fi
