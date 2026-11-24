---
name: memory-distiller
description: This skill should be used when the user asks to "work the memory inbox", "triage memory candidates", "apply the memory decisions", "apply my PR memory comments", "adopt this candidate", "dismiss this candidate", "correct this candidate", "distill the reports", "surface memory candidates", "retire a report", or "seed the invariants". It runs the nightly distillation pass that turns T0 unit/issue reports into T1 candidates, harvests the maintainer's decisions from the consolidation PR's comments, and works the T1 inbox one candidate at a time into the repo's T2 memory files.
---

# Memory distiller

Distil T0 evidence into T2 memory through a human-reviewed T1 inbox. The point is
that **a run never writes what future agents are told to believe** — it only
proposes. A person decides, one candidate at a time.

Three tiers, three directories, one rule: T2 is written by a human-approved verb
and nothing else.

| Tier | What | Where | Written by |
|---|---|---|---|
| T0 | raw unit/issue reports | `.agent/reports/`, `.ai-dlc/night-*/reports/` | the build/review stations; **never edited by this skill** |
| T1 | proposed, not yet binding | `.agent/candidates/*.md` | `distill.py surface` only |
| T2 | binding, read by every future session | `.agent/invariants.md`, `.agent/blockers.md`, `.agent/memory/*.md` | `inbox.py adopt` under a human turn |

Read `references/tiers.md` before your first adoption in a repo — it holds the
file schemas, the section-to-class map, and why OBSERVED is evidence rather than a
candidate.

## No candidate may depend on a device run

A candidate must be settleable from evidence already on disk. Never propose, and
never help someone adopt, a claim that needs a device, a container, or an IDF
build to confirm. `surface` enforces this: an item naming a host surface **and**
deferral language ("deferred to the morning", "manual (human) per the device
policy", "could not run") goes to `.agent/candidates/quarantined/`, which is not
adoptable.

Mentioning a device is not disqualifying — a mutation-verified widget test that
happens to run on an AVD is a perfectly good candidate. Only *deferral* is.

The fix for a quarantined item is never to adopt it. It is to run the check
out-of-band and post the report to the issue, then label the issue
`memory-needs-review` so the nightly `threads.py` picks it up (labels are the
queue — see `references/repo-config.md`):

```bash
python3 .../scripts/threads.py --repo-dir . --repo <owner/name> [--label memory-report]
```

That writes `.agent/threads/<n>.md`, which `surface` reads as ordinary T0. Host
reports use an RCA shape rather than the factory headings, and its best material
surfaces as `root-cause`, `test-blind-spot`, and `triage-answer`. A written-up
host pass is the highest-value input the system has; a human being asked to
confirm a guess is a dependency you should refuse to create.

## Two entry points

- **Unattended (nightly automation):** run the surface pass only. It is
  deterministic, contains no LLM call, and cannot touch T2.
- **Supervised (a person present):** work the inbox — one candidate, one
  decision, one audit line. The normal shape of "supervised" is the
  **consolidation PR in the GitHub web UI**: the human comments decisions and you
  apply them. See *Review from the PR*.

Never work the inbox unattended. `adopt`, `dismiss`, and `correct` refuse
without a human turn on record; do not pass `--force` to get around that.

## Review from the PR

The nightly run opens one consolidation PR per repo whose body lists every pending
candidate with its id, evidence class, and source, and states the decision
grammar. The maintainer reviews it in the browser and replies in plain comments:

```
memory: adopt 0332334f402a as "All MA calls go through app/ma/"
memory: dismiss 11dc46170501: one-off device quirk, not durable
memory: correct 2a3307735a75 as "the corrected wording"
```

When they say "apply the memory decisions", do this:

```bash
python3 .../scripts/review.py --repo-dir . --dry-run   # show the plan, change nothing
python3 .../scripts/review.py --repo-dir . --apply     # execute it
python3 .../scripts/inbox.py --repo-dir . ledger       # confirm the tally
```

Then commit the resulting `.agent/` changes to the consolidation branch and tell
the maintainer what moved. Report the JSON: `applied`, `refused`, `malformed`,
`errors`.

Why this is safe and where the limit is. OpenHands authenticates with the
maintainer's own PAT, so a GitHub comment cannot distinguish "the maintainer
decided" from "you decided" — both post as the same login. `author_association`
only keeps outsiders out of the channel; it is not a human-presence proof here.
The presence check is the **live conversation**: `claim_human_turn` refuses to run
under an automation context, so `review.py --apply` works only when a person is
driving a session. Never arrange for a cron or a webhook to run `--apply`, and do
not set `MEMORY_REVIEW_ALLOW_AUTOMATION` to work around that.

- `refused` lists decisions from a commenter GitHub does not recognise as repo
  personnel. Report them; do not apply them.
- `malformed` lists lines that looked like a decision but did not parse — a typo
  the maintainer believes took effect. Ask them to restate it rather than
  guessing at the intent.
- `--apply` on an already-decided candidate is inert; re-running is safe.
- Never invent the `as "..."` text for a candidate the maintainer only mentioned
  in passing. If a decision is ambiguous, ask.

## Before anything else

Read the repo's own memory protocol if it has one — `.agent/verification-protocol.md`
in some repos, `.agent/memory/README.md` in others. Those files win over this
skill on any conflict about evidence or format. Confirm `.agent/memory-config.json`
exists and names the repo's T0 roots and T2 targets; read
`references/repo-config.md` to create or adjust one.

## Surface pass (unattended)

```bash
python3 .agents/skills/memory-distiller/scripts/distill.py --repo-dir . surface
python3 .agents/skills/memory-distiller/scripts/distill.py --repo-dir . status
```

Deterministic and idempotent: candidate ids are content digests, so a report
already surfaced contributes 0 new candidates and an edited report contributes
exactly one. Report the JSON it prints — `written`, `already_in_inbox`,
`noise_rejected` — and stop. Do not adopt anything.

Use `--since YYYY-MM-DD` to limit a pass, and `--dry-run` to see what a pass
would write.

## Work the inbox (supervised)

Record the human turn first, once per session — this is what unlocks the
decision verbs and is the audit record that a person was present:

```bash
date -u +%FT%TZ > .agent/memory-human-turn
```

Then take candidates strictly one at a time. Never batch a decision across
candidates, and never adopt on the strength of the summary alone — open the
source at the cited line when the quote is not self-evident.

```bash
python3 .../scripts/inbox.py --repo-dir . show --limit 5    # highest durability first
```

For each candidate: read the quote, its `evidence_class`, and its
`corroboration` lines. Then do exactly one of:

**Adopt** — the claim is durable, correct, and belongs in what future agents read.

```bash
python3 .../scripts/inbox.py --repo-dir . adopt <id> \
  --target .agent/invariants.md --entry "<the claim, edited to a rule>" \
  --supersedes <n>            # only if it replaces an existing invariant
```

Pass the claim as a rule, not as a report line: "All MA calls go through
`app/ma/`", not "U3 routed MA calls through app/ma/". Append-only — the tool
adds the index row and body entry and never renumbers.

**Dismiss** — the claim is one-off detail, already covered, or wrong.

```bash
python3 .../scripts/inbox.py --repo-dir . dismiss <id> --reason "<why, in a sentence>"
```

The reason is mandatory because it is the record: a dismissed candidate that
resurfaces should be recognisable as already-decided, not re-litigated.

**Correct** — the observation is real but the wording or scope is wrong. This
keeps the original quote untouched as what was reported, and the corrected text
becomes the memory.

```bash
python3 .../scripts/inbox.py --repo-dir . correct <id> --corrected "<corrected claim>"
python3 .../scripts/inbox.py --repo-dir . adopt   <id> --target ... --entry "<corrected claim>"
```

**Requeue** when the decision needs evidence that does not exist yet (a host
verification, an upstream answer). Say what would unblock it in your reply.

### What goes where

Route by `evidence_class`; the config's `targets` map is authoritative:

- `unverified`, `unknown`, `assumption` → `.agent/invariants.md` (as a constraint
  or a settled interpretation), or dismiss as one-off noise.
- `blocker`, `deviation` → `.agent/blockers.md` in the `B<n>` shape, with a
  Disposition line.
- `correction`, `supervised` → highest value. These are claims a human confirmed
  in the real environment; they are what the whole pass exists to catch.
- `review-round`, `finding` → promote the *reviewer's* conclusion, not the
  bookkeeping line about fixing it.

Reject candidates that are actually test-case descriptions. `.agent/invariants.md`
says to keep out anything that "will be obvious from reading the code",
and issue-specific detail belongs in the report that already records it. A
high `durability_score` is a hint about how it reads, not a licence to adopt.

## Retiring a T0 report

Retiring works like closing an issue: the file is archived, not deleted, and the
optional reason and comment are both recorded. Never retire a report a live
invariant cites.

```bash
python3 .../scripts/inbox.py --repo-dir . retire .agent/reports/unit-03-x-report.md \
  --reason "all durable claims promoted" --comment "adopted 3, dismissed 5"
python3 .../scripts/inbox.py --repo-dir . ledger      # review what was retired
python3 .../scripts/inbox.py --repo-dir . unretire unit-03-x-report.md --comment "..."
```

Suggest retirements; let the person accept them. After a candidate's report is
retired, cite the archive path in the invariant's provenance so the evidence
stays reachable.

## Seeding a repo that has no T2 file yet

One-time. Point the config's T0 roots at the AI-DLC data, surface, work the
inbox supervised until the queue is clear, then reset the config to the ongoing
roots so later passes see only new evidence. The AI-DLC specs are a legitimate
T0 source for a first pass only — ongoing passes read reports. See
`references/seeding.md`.

## Guards

- Never edit T0. Never edit a candidate's original quote — use `correct`.
- Never adopt, dismiss, or correct without the human turn; never `--force` it.
- Never adopt from `.agent/candidates/quarantined/`. Ingest a written-up report
  instead, so the evidence exists rather than the assumption.
- Never renumber an invariant, and never delete one — supersede with `--supersedes`.
- Keep T2 small: it is injected into every future session. Long evidence stays in
  the report; the invariant is the one-line rule plus its WHY.
- Every action appends one JSON line to `.agent/memory-audit.md`. That log is
  append-only; do not rewrite it.
- If a candidate contradicts an existing invariant, stop and surface it. Do not
  pick a side silently — the repo's own rule is that this is a fact-check.

## Verifying the installation

```bash
python3 .agents/skills/memory-distiller/scripts/test_lifecycle.py          # bundled fixture
python3 .agents/skills/memory-distiller/scripts/test_lifecycle.py <repo>   # against a real repo
bash .agents/skills/memory-distiller/tests/ci-gate.sh                      # CI gate classifier
bash .agents/skills/memory-distiller/tests/labels.bash                     # label state machine
```

Runs the full lifecycle on a throwaway copy: surface idempotence, the three
human-turn refusals, append-only adoption into a numbered index, and
retire/unretire. Ends `all checks passed`, or lists failures and exits non-zero.
The bundled `scripts/fixture/` locks in every parsing trap found in the real
corpus — a `## Commits` section that must not leak into BLOCKERS, `FINDING F1:`
disposition lines, conventional-commit bullets, `none.` fillers, and the
`Review round` / `Supervised pass` / `Corrections` shapes.

## Additional resources

- `references/tiers.md` — tier schemas, the section→class map, why OBSERVED is
  evidence not a candidate, the durability heuristic.
- `references/repo-config.md` — `.agent/memory-config.json` fields, the three
  repos' differences, and the pipeline labels that make processed work visible
  in the GitHub UI.
- `references/seeding.md` — the one-time AI-DLC seed for a repo with no T2 file.
- `references/nightly-run.md` — wiring the surface pass as an automation, and what
  it must never be allowed to do.
- `references/ci-wiring.md` — how to stop a consolidation PR from tripping
  branch protection, and why `paths-ignore` and `[skip ci]` both break merges.
