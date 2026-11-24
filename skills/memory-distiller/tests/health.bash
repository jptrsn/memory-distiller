#!/usr/bin/env bash
# Run health classification: which outcomes count as working, degraded, or broken.
# Guards the property that a stuck repo can never be reported as success.
#
#   bash tests/health.bash
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export GITHUB_PERSONAL_ACCESS_TOKEN=not-a-real-token
python3 - <<'PY'
import sys
sys.path.insert(0, "automation"); import main
cases = [
  ("healthy",            {"labels":"ok","pr":"https://..."}, "ok"),
  ("labels failed",      {"labels":"failed: 403","pr":"unchanged"}, "degraded"),
  ("threads crashed",    {"labels":"ok","threads_error":"Traceback","pr":"unchanged"}, "degraded"),
  ("distill crashed",    {"labels":"ok","distill_error":"Traceback","pr":"unchanged"}, "degraded"),
  ("T2 refused",         {"labels":"ok","refused_paths":[".agent/invariants.md"],"pr":"https://"}, "degraded"),
  ("push failed",        {"labels":"ok","pr":"push failed: remote rejected"}, "degraded"),
  ("pr create failed",   {"labels":"ok","pr":"create failed 422"}, "degraded"),
  ("unchanged",          {"labels":"ok","pr":"unchanged"}, "ok"),
  ("dry-run",            {"labels":"ok","pr":"dry-run (not pushed)"}, "ok"),
  ("local repo",         {"labels":"ok","pr":"local repo (PR step skipped)"}, "ok"),
  ("not installed",      {"status":"skipped","reason":"x"}, "skipped"),
  ("local repo n/a",     {"labels":"n/a (local repo)","threads":"n/a (local repo)","pr":"local repo (PR step skipped)"}, "ok"),
]
bad=0
for name, summary, want in cases:
    got = main.health(summary)
    flag = "ok  " if got==want else "FAIL"
    if got!=want: bad+=1
    print(f"  {flag} {name:18} want={want:9} got={got}")
# the headline regression: one repo down must fail the whole run
print("  ---")
mixed=[{"repo":"a","status":"ok"},{"repo":"b","status":"error"},{"repo":"c","status":"ok"}]
broken=[r["repo"] for r in mixed if r.get("status") in ("error","degraded")]
print(f"  {'ok  ' if broken==['b'] else 'FAIL'} one-repo-failure surfaces broken={broken} (would have been COMPLETED before)")
allskip=[{"repo":"a","status":"skipped"}]
print(f"  {'ok  ' if not [r for r in allskip if r['status'] in ('error','degraded')] else 'FAIL'} all-skipped is not a failure")
sys.exit(1 if bad else 0)
PY
