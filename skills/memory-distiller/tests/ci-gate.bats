#!/usr/bin/env bats
# Run: bats tests/ci-gate.bats   (or: sh tests/ci-gate.sh for a no-deps run)

setup() {
  export GITHUB_OUTPUT="$BATS_TMPDIR/out.$$"
  : > "$GITHUB_OUTPUT"
}
run_gate() { bash scripts/ci-gate.sh --files-from "$1"; }

@test "pure memory PR skips gates" {
  printf '.agent/candidates/a.md\n.agent/archive/x--b.md\n.agent/threads/64.md\n' > "$BATS_TMPDIR/f"
  run run_gate "$BATS_TMPDIR/f"
  [ "$status" -eq 0 ]
  [ "$(cat "$GITHUB_OUTPUT")" = "memory_only=true" ]
}
@test "code PR runs gates" {
  printf 'app/main.py\n' > "$BATS_TMPDIR/f"
  run run_gate "$BATS_TMPDIR/f"
  [ "$(cat "$GITHUB_OUTPUT")" = "memory_only=false" ]
}
@test "mixed PR runs gates (no bypass by adding a memory file)" {
  printf '.agent/candidates/a.md\napp/main.py\n' > "$BATS_TMPDIR/f"
  run run_gate "$BATS_TMPDIR/f"
  [ "$(cat "$GITHUB_OUTPUT")" = "memory_only=false" ]
}
@test "T2 files are NOT exempt - invariants.md must still be gated" {
  printf '.agent/invariants.md\n' > "$BATS_TMPDIR/f"
  run run_gate "$BATS_TMPDIR/f"
  [ "$(cat "$GITHUB_OUTPUT")" = "memory_only=false" ]
}
@test "empty file list runs gates (fail-closed)" {
  : > "$BATS_TMPDIR/f"
  run run_gate "$BATS_TMPDIR/f"
  [ "$status" -eq 0 ]
  [ "$(cat "$GITHUB_OUTPUT")" = "memory_only=false" ]
}
@test "missing file runs gates (fail-closed)" {
  run run_gate "/nonexistent/list"
  [ "$status" -eq 0 ]
  [ "$(cat "$GITHUB_OUTPUT")" = "memory_only=false" ]
}
