#!/usr/bin/env bash
# The nightly run's commit allowlist: T2 (adopted memory) must never be
# committed by an unattended job. See references/nightly-run.md.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export GITHUB_PERSONAL_ACCESS_TOKEN=not-a-real-token
python3 - <<'PY'
import sys
sys.path.insert(0, "automation")
import main

ALLOW = [".agent/candidates/abc.md", ".agent/candidates/quarantined/x.md",
         ".agent/threads/64.md", ".agent/memory-audit.md"]
DENY = [".agent/invariants.md", ".agent/blockers.md", ".agent/memory/notes.md",
        ".agent/reports/issue-20.md", ".agent/threads", ".agent/candidates",
        # traversal must not be able to reach T2 from inside an allowed prefix
        ".agent/candidates/../invariants.md",
        ".agent/threads/../../.agent/invariants.md",
        ".agent/memory-audit.md/../../invariants.md",
        # not a repo-relative path at all
        "/etc/passwd", "../outside.md", ".agent/../.agent/invariants.md"]

fails = 0
for p in ALLOW:
    if not main.is_allowed(p):
        print(f"  FAIL should allow {p}"); fails += 1
for p in DENY:
    if main.is_allowed(p):
        print(f"  FAIL should deny   {p}"); fails += 1
# A denial must be reported, not silently dropped: that field is the operator's
# only signal that something tried to write outside the allowlist.
print("  ok   allow/deny matrix" if not fails else f"  {fails} FAILED")
sys.exit(1 if fails else 0)
PY
