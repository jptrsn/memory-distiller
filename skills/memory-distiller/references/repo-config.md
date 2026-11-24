# `.agent/memory-config.json`

| Field | Meaning |
|---|---|
| `repo` | name recorded on candidates and audit lines |
| `roots.<origin>.tier` | only `T0` roots are surfaced |
| `roots.<origin>.glob` | repo-relative glob of T0 markdown (supports `**`) |
| `roots.<origin>.kind` | label used in the candidate heading ("unit report", "issue report") |
| `targets.<evidence_class>` | T2 file for adopt when `--target` is omitted by the operator |
| `include_classes` | evidence classes allowed as candidates (default excludes `narrative`) |
| `provenance.<origin>` | extra provenance fields copied into the candidate |

## Three worked examples — do not share one config

**A backend monolith** — mature ladder already in prose; its reports state that
anything still binding was already promoted into `invariants.md`.

```json
{ "repo": "acme/backend",
  "roots": {
    "agent-reports": { "tier": "T0", "glob": ".agent/reports/*.md", "kind": "unit report" } },
  "targets": { "unverified": ".agent/invariants.md", "unknown": ".agent/invariants.md",
               "assumption": ".agent/invariants.md", "deviation": ".agent/blockers.md",
               "blocker": ".agent/blockers.md", "correction": ".agent/invariants.md",
               "review-round": ".agent/invariants.md", "finding": ".agent/invariants.md" } }
```

Retired reports move to `.agent/reports/archived/`, which the `*.md` glob
excludes — retiring removes a report from future passes without deleting it.

**owntone_remote** — nightly capture dirs are per-date; ongoing passes should
point at the orchestrator's output location, and seed from `night-*` once.

```json
{ "repo": "jptrsn/owntone_remote",
  "roots": {
    "orchestrator-reports": { "tier": "T0", "glob": ".ai-dlc/*/reports/*.md", "kind": "issue report" } },
  "targets": { "unverified": ".agent/invariants.md", "unknown": ".agent/invariants.md",
               "assumption": ".agent/invariants.md", "blocker": ".agent/blockers.md",
               "correction": ".agent/invariants.md", "supervised": ".agent/invariants.md" } }
```

Reports carry `## Supervised pass — <date> (user present, <device>)` and
`## Corrections — <date> (independent review of merged PR …)` — the
`supervised`/`correction` classes exist to catch those.

**esp-airsync** — topic files are already T2; its ladder is "read the relevant
topic file before starting related work, and add distilled learnings after".
Seed `targets` at `.agent/memory/` topic files plus `invariants`/`blockers` to
introduce the missing rung, and keep the repo's Evidence/Policy section shape
(frontmatter `topic/created/status/tags`). Its T0 will be factory PR reports.

## Pipeline labels

Labels are the state machine, so `gh` or the web UI answers "what is waiting"
without running anything. `labels.py` owns the vocabulary:

| Label | Meaning | Set by |
|---|---|---|
| `needs-host-tools` | Someone owes this issue a device/host pass | you, the maintainer |
| `memory-needs-review` | A written-up report is posted, awaiting the nightly run | you |
| `memory-ingested` | The report has been distilled into candidates | `threads.py` |

`threads.py` selects issues carrying `memory-needs-review` and, on a successful
ingest, removes it and adds `memory-ingested`. That makes exclusion free: an
ingested issue simply stops matching the queue query, so no run re-fetches
processed work. Keeping `memory-ingested` while you re-add `memory-needs-review`
(after posting a second report) distinguishes "never processed" from "processed,
and there is more".

```bash
python3 .../scripts/labels.py --repo-dir . ensure            # create the three
python3 .../scripts/labels.py --repo-dir . status --state all
python3 .../scripts/threads.py --repo-dir .                  # drain the queue
```

Use `--no-labels` only for the first backfill, when old issues have reports but
no labels yet. `set_labels` reads the current set before writing: GitHub's
PATCH replaces the entire list, so a blind write would silently drop labels
this tool does not own (`bug`, `area:ui`, …). The test suite pins that.
