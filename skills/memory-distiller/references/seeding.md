# One-time seeding (owntone_remote today; any repo without T2)

Goal: create the repo's first `.agent/invariants.md` / `.agent/blockers.md` from
the existing `.ai-dlc/` corpus, supervised, then hand the repo the ongoing
pass. AI-DLC specs are a legitimate T0 source **for the seed only**.

1. Seed the T2 files if absent, copying the house discipline verbatim so the
   conventions match across repos:
   - `# Invariants — <repo>` + the "Each entry has a **WHY**… append at the
     end; never renumber" paragraph + empty `## Index` table.
   - `# Blockers — <repo>` + the B<n> template from an existing repo's blockers.md
     (Status / What-why / Where it hurts / Disposition).
2. Write a seed-only `memory-config.json` whose roots point at the AI-DLC
   corpus only:
   `.ai-dlc/*/reports/*.md`, `.ai-dlc/*/units/*.md`, `.ai-dlc/*/intent.md`,
   `.ai-dlc/*/contract.md`, `.ai-dlc/orchestrator/*.md`.
3. `distill.py surface`, then work the inbox supervised until empty. Expect
   the bulk to be unit specs' verified decisions; adopt only cross-unit,
   non-obvious rules ("obvious from the code" fails the adoption test).
4. Rewrite the config to the ongoing roots (`references/repo-config.md`) so
   nightly passes see reports only. Commit the config with the seeded files.
5. `git log --follow` check: every adopted entry's source file must still exist
   or be archived — never delete seeded evidence.

The seed is a supervised session, not an automation. It is the one time an agent
may quote AI-DLC spec text into invariants, and every adoption is still one
human decision at a time.
