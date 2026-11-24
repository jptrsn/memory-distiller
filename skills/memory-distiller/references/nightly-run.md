# Nightly wiring (surface only)

Deterministic work only — per the automation guidance, a task whose output is a
fixed parse needs no LLM. Prefer a plugin-spec automation with
`entrypoint.runner: "script"`; a prompt preset is wrong here because reasoning
must not stand between the reports and the inbox.

What a nightly run may do:

```bash
git pull --ff-only                 # or the factory's clone step
python3 scripts/distill.py --repo-dir <repo> surface --since <last-run-date>
python3 scripts/distill.py --repo-dir <repo> status
# commit + PR ONLY the .agent/candidates/ directory
```

Hard boundaries:

- The run commits `.agent/candidates/**` and nothing else. Never
  `invariants.md`, `blockers.md`, `memory/*`, or T0 files.
- The run does not call `inbox.py` decision verbs. The human-turn guard makes
  that fail closed if wiring ever regresses.
- Committing candidates directly to the default branch is acceptable (they are
  inert until adopted and never injected); if the repo prefers review, open one
  consolidation PR per repo per night touching only `candidates/`.
- Store the last-run date in the automation KV store, not a file — sandbox
  filesystems are ephemeral on cloud runs.
- Keep the run cheap: one surface pass is O(new reports), no LLM, no network
  beyond git.

The human side is the skill itself: `work the memory inbox` in a supervised
session opens the queue the nightly pass filled.

## Reading the result

The driver prints one JSON document and exits non-zero when anything needs you.

```json
{"ran_at": "...", "dry_run": false,
 "summary": {"ok": 2, "degraded": 1},
 "broken": ["acme/widget"],
 "results": [{"repo": "...", "base_branch": "develop", "status": "ok",
              "labels": "ok", "threads_ingested": 3, "candidates_written": 4,
              "noise_rejected": 1, "host_bound_quarantined": 0,
              "files_changed": 5, "refused_paths": [],
              "pr": "https://github.com/.../pull/7"}]}
```

A repo whose distill pass wrote nothing new reports `pr: "unchanged"` and no
commit; `pr_updated: true` means the run pushed onto an existing open PR rather
than opening a second one.

Per-repo `status` is one of four values, computed from what happened rather than
assumed:

| status | means | you should |
|---|---|---|
| `ok` | every step that applied did its job | nothing; read the PR if one opened |
| `degraded` | a step ran and failed, or the run refused a path | read the field named below |
| `error` | the repo crashed outright (clone, auth, base branch) | read `error` |
| `skipped` | the skill is not installed in that repo | install it, or drop the repo from `MEMORY_REPOS` |

`COMPLETED` is reported only when no repo is `error` or `degraded`. A single bad
repo fails the run deliberately: a nightly that goes green while one repo's queue
is stuck teaches you to stop reading it, and un-ingested reports then age out
silently.

Which field to read when `status` is `degraded`:

- `labels: "failed: ..."` — label writes failed, so the queue did not advance;
  issues already ingested will be re-fetched next run. Usually a token scope.
- `threads_error` / `distill_error` — that step crashed; the text is its stderr.
- `refused_paths` — the distiller wrote outside the allowlist and the run
  declined to commit it. This is the T2 guard working; investigate why a surface
  pass reached for adopted memory.
- `pr: "push failed: ..."` / `"commit failed: ..."` / `"create failed ..."` —
  consolidation did not reach GitHub.

`unchanged`, `dry-run (not pushed)`, and `local repo (PR step skipped)` in `pr`
are normal, not failures. `labels`/`threads` reading `n/a (local repo)` is normal
for a local-path repo, which has no GitHub side to reach.

## Repairing label drift

`memory-ingested` is written the moment the thread file is, but that file only
lands on the default branch if you merge the consolidation PR. Close the PR
instead and the label survives while the content never arrives — and because that
label is what excludes an issue from the queue, the issue becomes invisible with
no path back.

Local files are the source of truth, so repair from them:

```bash
python3 scripts/labels.py --repo-dir . reconcile --dry-run
python3 scripts/labels.py --repo-dir . reconcile
```

An issue labelled ingested with no thread file is reclaimed to
`memory-needs-review` and re-enters the queue; foreign labels are preserved.
`--include-unlabelled` also labels ingested issues that lost their label (one
extra Search API call). Run it before ingest so drift cannot accumulate.

## Verifying it works before trusting a schedule

Run it yourself once and read the JSON, rather than finding out from a silent
week:

```bash
MEMORY_DRY_RUN=1 MEMORY_REPOS=owner/repo python3 automation/main.py; echo "exit=$?"
```

Expect `status: ok`, a `pr` value, and exit `0`. Then check three mornings later
that `distill.py status` shows candidates moving out of `pending` — a queue that
never drains means the run is degraded in a way the label alone would not show.
