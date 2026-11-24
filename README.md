# memory-distiller

An OpenHands skill that turns the reports your agents already write into durable
project memory — through a review queue a human actually controls, from the
GitHub web UI.

It is the "dreaming" pass for an [OpenHands software
factory](https://docs.openhands.dev): a nightly job reads yesterday's unit and
issue reports, extracts the claims worth keeping, and proposes them. Nothing it
proposes becomes what future agents are told to believe until a person says so.

```
T0  reports your stations write        .agent/reports/, .ai-dlc/*/reports/
     │  distill.py surface  (deterministic, no LLM, runs unattended)
T1  proposed candidates                .agent/candidates/*.md      ← inert
     │  a person decides, in the consolidation PR
T2  binding memory, read by every run  .agent/invariants.md, blockers.md, memory/*.md
```

**The rule that makes this safe:** T1 files are never injected into a prompt.
An unattended run can propose infinitely and adopt nothing. If you ignore the
queue entirely, nothing breaks and nothing improves.

## Why not just let the agent write its own memory

Because the failure mode is silent. A run that "learns" a wrong fact from a
half-validated run keeps asserting it in every future session, and you find out
weeks later. So promotion is gated, the gate is auditable, and the audit names
who decided and where the claim came from:

```markdown
**32. Nightly consolidation never writes T2**
_promoted from `.agent/reports/x-report.md` §FINDINGS (sha256:deadbeef)_
```

## Install

Copy this skill into each repository's agent-skills directory:

```bash
git clone https://github.com/jptrsn/memory-distiller.git
mkdir -p <repo>/.agents/skills
cp -r memory-distiller/skills/memory-distiller <repo>/.agents/skills/
```

Then give the repo a config. `scripts/fixture/.agent/memory-config.json` is a
working example; [`references/repo-config.md`](skills/memory-distiller/references/repo-config.md) covers
every field. The minimum:

```json
{
  "repo": "owner/name",
  "roots": {
    "agent-reports": { "tier": "T0", "glob": ".agent/reports/*.md", "kind": "unit report" }
  },
  "targets": {
    "assumption": ".agent/invariants.md",
    "blocker": ".agent/blockers.md"
  }
}
```

`roots` is required (the loader exits if it's missing) and an entry needs
`tier: "T0"` to be surfaced at all. `targets` maps an evidence class to the T2 file
`adopt` writes when you don't pass `--target`.

A repo with no T2 files yet gets seeded from its AI-DLC corpus — see
[`references/seeding.md`](skills/memory-distiller/references/seeding.md).

## Run the nightly pass

Deterministic, no LLM call, and structurally unable to touch T2.

Install it as an OpenHands automation from this repo — `.plugin/plugin.json`
declares a `script` runner, so no LLM is in the loop and the repo list is a
config field rather than an edit to code:

| Config field | Meaning |
|---|---|
| `repos` | list of `owner/repo`, optionally `owner/repo@base-branch` |
| `dry_run` | `true` (default) = everything except labels, push, and open PR |
| `review_bots` | logins whose comments are never treated as decisions |
| `review_allow_automation` | accept an automation comment as the human turn — leave off |

Leave `dry_run` on for the first run and read the counts it prints. Nothing
touches GitHub until you turn it off: no labels are created, no issue label is
flipped, no branch is pushed, no PR is opened.

By hand, the same driver reads the same settings from the environment. Env wins
over the config file, so a dispatch can override a value ad hoc:

```bash
MEMORY_REPOS=owner/repo@develop MEMORY_DRY_RUN=1 \
  python3 skills/memory-distiller/automation/main.py
```

| Variable | Meaning |
|---|---|
| `MEMORY_REPOS` | comma-separated `owner/repo`, optionally `@base-branch` |
| `MEMORY_DRY_RUN` | `1` = everything except labels, push, and open PR |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | contents+issues+PRs; fetched from the agent server's secret store in automation runs |
| `MEMORY_REVIEW_BOTS` | logins whose comments are never treated as decisions |

A run over zero repos refuses instead of exiting 0, so a mis-set list cannot
report green having consolidated nothing.

One consolidation PR opens per repo, reusing the same branch night to night so
`surface` doesn't regenerate candidates it already proposed. The run prints one
JSON document and exits non-zero when anything needs you — a repo whose label
writes silently failed is reported `degraded`, not `ok`. See
[`references/nightly-run.md`](skills/memory-distiller/references/nightly-run.md) for the status table and
what each field means.

Base-branch resolution matters for GitFlow repos: `@develop` is validated
against the remote's actual head list, so a typo fails immediately naming the
branches available rather than dying inside `git clone`.

## Review from the browser

The consolidation PR's body is the review surface. It lists every pending
candidate with its id, durability, evidence class, source file and section, the
quote itself, and the syntax:

```
memory: adopt 0332334f402a as "All MA calls go through app/ma/"
memory: dismiss 11dc46170501: one-off device quirk, not durable
memory: correct 2a3307735a75 as "the corrected wording"
```

Comment those on the PR, then tell OpenHands **"apply the memory decisions"**.
It shows you the plan first (`--dry-run`), applies through the same code path a
supervised CLI adoption uses, commits, and reports `applied` / `refused` /
`malformed` / `errors`. No terminal required.

```bash
python3 .agents/skills/memory-distiller/scripts/review.py --repo-dir . --dry-run
python3 .agents/skills/memory-distiller/scripts/review.py --repo-dir . --apply
```

Wording matters: write the adopted text as a **rule**, not a report line.
`"All MA calls go through app/ma/"`, not `"U3 routed MA calls through app/ma/"` —
that string is what future agents will read as established fact.

The CLI inbox (`scripts/inbox.py show/adopt/dismiss/correct/retire`) still works
and produces identical records, for when you're already in a shell.

## The human gate, and why it isn't in GitHub

OpenHands authenticates with **your** PAT. The agent and you post as the same
login, so a PR comment, a review, and `author_association` cannot distinguish
your decision from the agent's. `author_association` only filters outsiders out
of the decision channel; it proves nothing about *which* of you two typed it.

So presence is enforced where it can't be counterfeited — a live conversation:

- `adopt`/`dismiss`/`correct` refuse without a human turn on record, and exit
  non-zero rather than warning.
- `review.py --apply` refuses outright inside an automation context
  (`AUTOMATION_RUN_ID`/`AUTOMATION_CALLBACK_URL`/`AUTOMATION_KV_TOKEN`), so the
  nightly cron cannot harvest comments, write the marker, and pass its own gate.

There is an escape hatch, `MEMORY_REVIEW_ALLOW_AUTOMATION=1`, for a run a person
is genuinely watching. Don't schedule it.

## Labels

Queue state lives in GitHub so you can see it in the UI: `memory-needs-review`,
`memory-ingested`, `needs-host-tools`.

`memory-ingested` is written when the thread file is, but that file only lands if
you merge. Close the PR and the label survives while the content never arrives —
and that label is what excludes an issue from the queue, so the issue would go
invisible forever. `labels.py reconcile` repairs it from local files, which are
the source of truth. Run it before ingest.

## Tests

Eight bash/python suites, stdlib only, no network — GitHub is a fixture HTTP
server, not a mock:

```bash
cd skills/memory-distiller
python3 scripts/test_lifecycle.py          # bundled fixture
bash tests/nightly.bash                    # full driver against local bare repos
bash tests/review.bash                     # PR-comment harvesting incl. refusal paths
bash tests/guard.bash                      # T2 commit allowlist
bash tests/ci-gate.sh && bash tests/labels.bash
bash tests/reconcile.bash && bash tests/health.bash
```

`tests/nightly.bash` fault-injects a leaky distiller and asserts the commit
allowlist refuses it, and asserts the token never reaches `.git/config` or the
commit log.

## Known limits

Read these before trusting a schedule.

- **Never run against real GitHub.** The driver has only been exercised against
  local bare repos and a fixture API: push, PR creation, and label writes are
  unverified in production. Start with `MEMORY_DRY_RUN=1`.
- **No T0 source, no pipeline.** A repo without report directories reports
  `skipped` every night. Point `MEMORY_REPOS` at repos that actually have
  reports; don't leave permanent skips in the list.
- **`needs-host-tools` is advisory.** Claims needing a device or an IDF build get
  flagged in the PR body, not blocked. You still have to actually run that pass
  before adopting them — the tool cannot know you did.
- **Malformed decisions fail loudly by design** (exit 1, listed in `malformed`),
  so a typo'd decision you believe took effect gets caught rather than dropped.

## Layout

```
skills/memory-distiller/
  SKILL.md              the agent-facing procedure
  scripts/              distill · inbox · threads · labels · review · tiers
  automation/main.py    the nightly driver
  references/           tiers · repo-config · nightly-run · seeding · ci-wiring
  tests/                8 suites + a fixture GitHub API server
```

`SKILL.md` is the file OpenHands loads; everything above it in the tree is
supporting material, which is why the skill lives under `skills/` and a repo
installs it with a plain copy.

## License

GPL-3.0-only, matching `owntone_remote` and `esp-airsync`. See [LICENSE](LICENSE).
