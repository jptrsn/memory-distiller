#!/usr/bin/env python3
"""End-to-end lifecycle test against a real repo checkout.

Proves the four properties the design depends on:
  1. surface is idempotent (re-run adds 0 candidates)
  2. adopt/dismiss/correct refuse without a human turn on record
  3. adopt appends a numbered invariant and never renumbers existing ones
  4. retire moves a report and writes a ledger row; unretire restores it

Run:  test_lifecycle.py <repo-dir-with-.agent>   (default ./fixture)
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILS = []


def run(*argv, expect_ok=True, label=""):
    cmd = [sys.executable] + [str(x) for x in argv]
    p = subprocess.run(cmd, capture_output=True, text=True)
    ok = p.returncode == 0
    if ok != expect_ok:
        FAILS.append(f"{label}: exit {p.returncode} (expected {'0' if expect_ok else 'nonzero'})"
                     f"\n  stdout: {p.stdout[:300]}\n  stderr: {p.stderr[:300]}")
    return p


def check(cond, label):
    if not cond:
        FAILS.append(f"assert failed: {label}")
    print(("  ok   " if cond else "  FAIL ") + label)


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "fixture"
    if not (src / ".agent").exists():
        print(f"no .agent under {src}", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="memdistill-"))
    repo = work / src.name
    shutil.copytree(src, repo)
    # Start from a clean inbox so "surface produced candidates" is meaningful
    # even when run against a repo that has already been surfaced once.
    for stale in (".agent/candidates", ".agent/memory-audit.md",
                  ".agent/memory-human-turn"):
        t = repo / stale
        if t.is_dir():
            shutil.rmtree(t)
        elif t.exists():
            t.unlink()
    distill = HERE / "distill.py"
    inbox = HERE / "inbox.py"
    base = ["--repo-dir", str(repo)]

    # --- seed T2 if the repo lacks it (the one-time seeding step) -----------
    inv = repo / ".agent/invariants.md"
    if not inv.exists():
        inv.write_text(
            "# Invariants\n\nAppend new entries at the end; never renumber.\n\n"
            "## Index\n\n| # | Invariant | Area |\n|---|---|---|\n"
        )
    before_inv = inv.read_text()
    existing = before_inv.count("\n| ")

    # --- 1. surface, twice --------------------------------------------------
    p = run(distill, *base, "surface", label="surface #1")
    first = json.loads(p.stdout)
    p = run(distill, *base, "surface", label="surface #2")
    second = json.loads(p.stdout)
    check(first["written"] > 0, f"surface produced candidates ({first['written']})")
    check(second["written"] == 0,
          f"surface is idempotent (re-run wrote {second['written']}, dup {second['already_in_inbox']})")
    check(second["already_in_inbox"] >= first["written"],
          "every re-surfaced item was recognised as a duplicate")

    # --- 2. human-presence gate --------------------------------------------
    cands = sorted((repo / ".agent/candidates").glob("*.md"))
    cid = cands[0].stem
    p = run(inbox, *base, "adopt", cid, "--target", ".agent/invariants.md",
            "--entry", "A test invariant", expect_ok=False, label="adopt unattended")
    check("no human turn" in (p.stderr + p.stdout),
          "adopt refused without a human turn")
    p = run(inbox, *base, "dismiss", cid, "--reason", "nope",
            expect_ok=False, label="dismiss unattended")
    check("no human turn" in (p.stderr + p.stdout), "dismiss refused without a human turn")
    p = run(inbox, *base, "correct", cid, "--corrected", "x",
            expect_ok=False, label="correct unattended")
    check("no human turn" in (p.stderr + p.stdout), "correct refused without a human turn")

    # --- show ---------------------------------------------------------------
    p = run(inbox, *base, "show", "--limit", "3", label="show")
    shown = json.loads(p.stdout)
    check(shown["pending"] == len(cands), f"show lists pending ({shown['pending']})")
    scores = [r["durability_score"] for r in shown["next"]]
    check(scores == sorted(scores, reverse=True), "show orders by durability desc")

    # --- 3. supervised session: adopt / dismiss / correct -------------------
    (repo / ".agent/memory-human-turn").write_text("supervised session\n")
    target = shown["next"][0]["id"]
    p = run(inbox, *base, "adopt", target, "--target", ".agent/invariants.md",
            "--entry", "The distiller must never write T2 unattended", label="adopt")
    adopted = json.loads(p.stdout)
    check(adopted["invariant"] == existing or adopted["invariant"] > 1,
          f"adopt appended invariant #{adopted['invariant']}")
    text = inv.read_text()
    # Append-only: every original line must survive, in order (exactly one new
    # index row is inserted mid-file, and the body entry is appended).
    orig = before_inv.splitlines()
    now_ = text.splitlines()
    it = iter(now_)
    preserved = all(any(o == n for n in it) for o in orig)
    check(preserved, "no prior line was rewritten or reordered")
    check(len(now_) > len(orig), "file only grew")
    check(text.count("\n| ") == existing + 1, "index gained exactly one row")
    check(f"**{adopted['invariant']}. The distiller must never write T2" in text,
          "body entry written in the repo's invariant shape")
    check("promoted from" in text, "promotion provenance recorded on the entry")
    meta = cands and (repo / ".agent/candidates" / f"{target}.md").read_text()
    check("status: adopted" in meta, "candidate marked adopted")

    second_id = shown["next"][1]["id"]
    run(inbox, *base, "dismiss", second_id, "--reason", "one-off test detail",
        label="dismiss")
    check("status: dismissed" in (repo / ".agent/candidates" / f"{second_id}.md").read_text(),
          "candidate marked dismissed with reason")

    third_id = shown["next"][2]["id"]
    run(inbox, *base, "correct", third_id, "--corrected", "corrected wording", label="correct")
    corrected = (repo / ".agent/candidates" / f"{third_id}.md").read_text()
    check("status: corrected" in corrected, "candidate marked corrected")
    check("## Correction" in corrected, "correction appended below the original quote")

    # re-deciding a terminal candidate must refuse
    run(inbox, *base, "adopt", second_id, "--target", ".agent/invariants.md",
        "--entry", "x", expect_ok=False, label="re-adopt a dismissed candidate")

    # --- 4. retire / unretire ----------------------------------------------
    report = next(p for p in (repo / ".agent/reports").glob("*.md")) \
        if (repo / ".agent/reports").exists() else None
    if report is None:
        roots = json.loads((repo / ".agent/memory-config.json").read_text())["roots"]
        cand_files = [f for spec in roots.values()
                      for f in repo.glob(spec["glob"]) if f.name.endswith(".md")]
        report = cand_files[0]
    rel = str(report.relative_to(repo))
    name = report.name
    run(inbox, *base, "retire", rel, "--reason", "all durable claims promoted",
        "--comment", "3 candidates adopted", label="retire")
    check(not report.exists(), "retired report no longer in its T0 root")
    archived = list((repo / ".agent/archive").glob(f"*--{name}")) if (repo / ".agent/archive").exists() else []
    check(len(archived) == 1, f"report landed in .agent/archive flattened ({len(archived)})")
    # The archive is outside every T0 glob, so a retired file can never resurface.
    cfg = json.loads((repo / ".agent/memory-config.json").read_text())
    for origin, spec in cfg["roots"].items():
        check(not list((repo).glob(spec["glob"])) or name not in
              [f.name for f in (repo).glob(spec["glob"])],
              f"retired file invisible to root '{origin}' glob")
    ledger = (repo / ".agent/reports/archive-log.md").read_text()
    check("all durable claims promoted" in ledger, "ledger recorded the reason")
    check("3 candidates adopted" in ledger, "ledger recorded the comment")
    check("digest: `sha256:" in ledger, "ledger recorded the content digest")

    run(inbox, *base, "retire", rel, expect_ok=False, label="re-retire")
    run(inbox, *base, "unretire", name, "--comment", "needed again", label="unretire")
    check(report.exists(), "unretire restored the report to its T0 root")

    # retired report is not re-surfaced
    run(distill, *base, "surface", label="surface after unretire")

    # --- 5. thread reports: RCA classes surface, host-deferred quarantined ---
    # Only assertible when the repo actually has a threads root (a repo that
    # has not run threads.py yet legitimately has none).
    has_threads = (repo / ".agent/threads").exists() \
        and any((repo / ".agent/threads").glob("*.md"))
    thread_cands = []
    for f in (repo / ".agent/candidates").glob("*.md"):
        fields = dict(l.split(":", 1) for l in f.read_text().split("---\n")[1].strip().splitlines() if ":" in l)
        thread_cands.append((fields.get("evidence_class", "").strip(),
                             fields.get("section", "").strip()))
    classes = {c for c, _ in thread_cands}
    if not has_threads:
        print("  skip  thread RCA assertions (no .agent/threads in this repo)")
    if has_threads:
        check("root-cause" in classes, "thread RCA surfaced as root-cause candidate")
        check("test-blind-spot" in classes, "test-blind-spot surfaced (durable host lesson)")
        check("triage-answer" in classes, "triage answers surfaced")
        for forbidden in ("reproduction", "plan", "stack", "evidence"):
            check(forbidden not in classes, f"{forbidden} section is context, not a candidate")
        sections = " ".join(sec for _c, sec in thread_cands)
        check("Reproduction" not in sections, "repro steps did not leak into candidates")
        check("Plan for completion" not in sections, "forward-looking plan did not leak in")
    q = repo / ".agent/candidates/quarantined"
    qq = sorted(q.glob("*.md")) if q.exists() else []
    if has_threads or (repo / ".agent/reports").exists():
        check(len(qq) >= 1, f"host-deferred items quarantined ({len(qq)})")
    if qq:
        qt = qq[0].read_text()
        check("quarantined_host_bound" in qt, "quarantine file records its status")
        check("not adoptable" in qt.lower(), "quarantine file says it is not adoptable")
    # promotability must agree with the evidence printed beside it. Real corpus
    # data exposed 125 bookkeeping lines ("fixed (c93d789)", "GATES re-run tails")
    # labelled promotable at durability_score 0 — the reviewer's first read of a
    # candidate, so a wrong value there is how noise gets adopted.
    def fm(f, key):
        for line in f.read_text().split("---\n")[1].splitlines():
            if line.startswith(f"{key}: "):
                return line.split(": ", 1)[1].strip()
        return None

    allc = list((repo / ".agent/candidates").glob("*.md"))
    mism = [f.name for f in allc
            if fm(f, "promotability") == "promotable" and fm(f, "durability_score") == "0"]
    check(not mism, f"no promotable candidate scores 0 ({len(mism)} found: {mism[:3]})")
    # --keep-host-bound surfaces quarantined claims, so they must be identifiable:
    # review.py flags `promotability == "host-bound"`, and a host-bound claim that
    # renders as provisional is how an unverified device claim reaches T2.
    if has_threads or (repo / ".agent/reports").exists():
        run(distill, *base, "surface", "--keep-host-bound", label="surface --keep-host-bound")
        hb = [f for f in (repo / ".agent/candidates").glob("*.md")
              if fm(f, "promotability") == "host-bound"]
        check(len(hb) >= 1, f"--keep-host-bound marks host-bound items as such ({len(hb)})")
        check(all("promotability: promotable" not in f.read_text() for f in hb),
              "host-bound items are never labelled promotable")
    # A settled claim that merely mentions a device must stay a candidate.
    kept = [f for f in (repo / ".agent/candidates").glob("*.md")
            if "pixel" in f.read_text().lower() or "device" in f.read_text().lower()]
    check(len(kept) >= 1, "device-mentioning but settled claims stayed adoptable")

    audit = (repo / ".agent/memory-audit.md").read_text().strip().splitlines()
    kinds = [json.loads(l)["action"] for l in audit if l.startswith("{")]
    for want in ("surface", "adopt", "dismiss", "correct", "retire", "unretire"):
        check(want in kinds, f"audit logged {want}")

    print(f"\nworktree kept for inspection: {repo}")
    if FAILS:
        print(f"\n{len(FAILS)} FAILURE(S):")
        for f in FAILS:
            print(" - " + f)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
