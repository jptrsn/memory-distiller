# Consolidation PRs and branch protection

A nightly PR touching `.agent/candidates/` triggers `pull_request: branches:
[develop, main]` in both repos, so `CI / gates`, `CI / frontend`, `CI / e2e`
(a FastAPI monolith) and `gates / gates` (a Flutter app) would each burn a full run on
files no gate can check.

## The three ways to suppress it, and why two are wrong

| Approach | Verdict |
|---|---|
| `on.pull_request.paths-ignore: ['.agent/**']` | **Breaks merges.** GitHub: "If a workflow is skipped due to path filtering… checks associated with that workflow will remain in a **Pending** state. A pull request that requires those checks… **blocked from merging.**" |
| `[skip ci]` in the commit message | **Same trap.** A commit-message skip is the third case in that same sentence. |
| Job-level `if:` so the job reports **skipped** | **Correct.** Required status checks accept `successful`, `skipped`, or `neutral`. |

The difference is where the skip happens: workflow-level filtering never
creates a check at all, so protection waits forever; a job-level `if` creates
the check and reports `skipped`.

## Guard against the bypass this opens

A skipped required check satisfies protection, so anything that makes the gate
job skip on the wrong signal is a way to merge unverified code — including an
error in the classifier itself, because `needs:` skips dependents.

1. `ci-gate.sh` is **fail-closed**: missing token, API error, or empty list all
   yield `memory_only=false`, which runs the gates.
2. `gates` uses `if: needs.changes.outputs.memory_only != 'true' &&
   needs.changes.result == 'success'`. Without the `result` clause, a **failed**
   `changes` job would skip `gates` and wave the PR through.
3. The exemption covers `candidates/`, `archive/`, `threads/` only. **T2 is not
   exempt**: a PR touching `.agent/invariants.md` or `blockers.md` runs the
   gates, because that is the file every future session reads.
4. Exempting `.agents/skills/memory-distiller/**` and `docs/memory/**` is a
   deliberate trade — treat both as trusted until they gain executable hooks.

## Patch (`ci.yaml`; same shape for `gates.yml`)

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    outputs:
      memory_only: ${{ steps.filter.outputs.memory_only }}
    steps:
      - uses: actions/checkout@v6
      - id: filter
        env:
          GH_TOKEN: ${{ github.token }}
          PR_FILES_URL: repos/${{ github.repository }}/pulls/${{ github.event.pull_request.number }}/files
        run: bash scripts/ci-gate.sh

  gates:
    # Both clauses matter: the second stops a failed classifier from skipping
    # the gates, and a skipped required check would satisfy branch protection.
    if: >-
      needs.changes.outputs.memory_only != 'true'
      && needs.changes.result == 'success'
    needs: changes
    runs-on: ubuntu-24.04
    # … unchanged …

  frontend:
    needs: gates        # unchanged — a skip cascades down `needs`
  e2e:
    needs: gates        # unchanged
```

A repo whose `frontend` and `e2e` jobs both `needs:
gates`, and GitHub docs confirm "If a job fails or is skipped, all jobs that
need it are skipped."

## Verify before trusting it

Open a throwaway PR that adds one file under `.agent/candidates/` and confirm:

- [ ] `CI / gates` shows **skipped** (not pending, not passed)
- [ ] `CI / frontend` and `CI / e2e` show **skipped**
- [ ] the PR is **mergeable**
- [ ] a PR touching `app/` runs all three
- [ ] deliberately failing `changes` leaves `gates` **running**, not skipped

If `gates` shows "Expected" instead of "skipped", the `if` is on the wrong job.
