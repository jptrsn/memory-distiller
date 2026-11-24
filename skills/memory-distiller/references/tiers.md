# Tier model and schemas

## Why these tiers

Adapted from AI-DLC's learnings ritual (`docs/reference/08-rule-system.md`,
`06-hooks-and-tools.md` in awslabs/aidlc-workflows): detection and writing are
deterministic tool work; the conflict check is the model's; keep/skip/escalate
is the human's, with **no override path**. The directory layout is NOT adopted —
these repos have no `aidlc/spaces/`, no engine, and AGENTS.md plus role skills
are the delivery mechanism. Only the governance transfers.

## T1 candidate file schema

`.agent/candidates/<12-hex-id>.md` — id is the first 12 hex of
`sha256("origin|source_file|section|item")`, which makes `surface` idempotent.

```yaml
id: 01b5d7e8713b
status: pending            # pending | adopted | dismissed | corrected | superseded
evidence_class: assumption # see map below
promotability: provisional # promotable | provisional
durability_score: 0        # heuristic only, never an auto-adopt licence
repo: jptrsn/owntone_remote
source_file: .ai-dlc/night-2026-11-19/reports/45.md
source_digest: sha256:...  # of the WHOLE report at surface time
section: ASSUMPTIONS
line: 173
origin: night-2026-11-19   # config root name
surfaced: 2026-11-24
adopted_to: null           # set to "<target>#inv-<n>" on adopt
dismissed_reason: null     # required on dismiss
```

`source_digest` is what makes staleness detectable: if the report changed after
surfacing, the digest in the audit line no longer matches the file.

## Section -> evidence class map (tiers.py SECTIONS)

| Report section | class | candidate? |
|---|---|---|
| OBSERVED | observed | **no — evidence only** |
| GATES, SUMMARY, GO/NO-GO CHECK | gate/summary/go-no-go | no |
| NOT VERIFIED | unverified | yes |
| ASSUMPTIONS | assumption | yes |
| DEVIATIONS | deviation | yes |
| UNKNOWN | unknown | yes |
| BLOCKERS | blocker | yes |
| CORRECTIONS, SUPERVISED PASS | correction/supervised | yes (highest value) |
| RESOLVED FINDINGS, FINDINGS AND ADJUDICATION | finding | yes |
| Review round <n> | review-round | yes (the conclusion, not the disposition) |
| Step <k> (build narrative) | narrative | opt-in via `include_classes` |

Any ATX heading closes the current section, so unlisted headings (`## Commits`)
cannot leak into a candidate body.

## Why OBSERVED is evidence, not a candidate

Measured across 40 real unit reports and 31 live invariants in one repo: of the invariants
whose origin phrase is traceable in T0, **one** traces to an OBSERVED line; the
largest groups trace to build-step narrative, review-round dispositions, and
NOT VERIFIED notes. OBSERVED items are per-test assertions (`test_x → 200, body
exactly {...}`) — exactly the issue-specific detail a well-run AGENTS.md forbids
from memory. OBSERVED is therefore
attached to each candidate as `corroboration` lines and quoted at adopt time,
never surfaced as a candidate itself.

The admission rule this implements, from the repos' own
`.agent/verification-protocol.md`: "OBSERVED entries must name an action you
took and a result you saw. 'pytest passed' is not an OBSERVED entry."

## Volume expectations (measured)

- Steady state, one night of owntone_remote (11 issue reports): **117 candidates**,
  median 216 chars. Classes: assumption 75, correction 32, supervised 6, blocker 4.
- One-time backfill of all 40 reports in that corpus: **449** (narrative excluded).
- `narrative` (build steps) is 55% of raw volume; it stays opt-in per repo.

If a nightly pass produces far more than ~150 candidates, narrow
`include_classes` or `--since` before working the queue.

## Durability heuristic (surface-time only)

+3 always/never/must/only/sole/single source · +2 root cause/stale/drift/
tripwire/regression/invariant · +1 pre-existing/flaky/ephemeral/manual/human.
It orders `show`; it never adopts. Dismissed-for-noise patterns: empty/none/NA,
"all gates green", "no findings", "see the report", bare commit lines, URLs,
`FINDING <id>:` disposition lines, conventional-commit subject lines.
