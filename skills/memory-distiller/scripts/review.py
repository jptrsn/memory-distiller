#!/usr/bin/env python3
"""Harvest memory decisions from the consolidation PR's comments.

The review surface is GitHub's own UI: you read the candidate diffs in the pull
request and reply with one line per decision. This turns those replies into
inbox actions, so no one has to run a CLI to move memory forward.

Grammar (one per line; `#` starts a comment; anything else is ignored):

    memory: adopt <id> as "<the rule>" [target=.agent/invariants.md] [area=x] [supersedes=N]
    memory: dismiss <id>: <why this is not durable>
    memory: correct <id> as "<corrected wording>"

What this does and does not prove. OpenHands acts with a PAT that authenticates
as the maintainer, so a GitHub comment cannot distinguish "the maintainer
decided" from "the agent decided" — both post as the same login. Do not read
`author_association` as a human-presence proof here; it only keeps *outsiders*
out of the decision channel, which is still worth having.

The human-presence gate is therefore NOT in GitHub. It lives in the OpenHands
session: `inbox.require_human_turn` must be satisfied by a real user turn in a
live conversation, which is the one signal in this stack the agent cannot
fabricate. Run this from a conversation a person is driving, never from an
unattended nightly cron.

  review.py --repo-dir . --pr 7 --dry-run     # show the plan
  review.py --repo-dir . --pr 7 --apply        # execute it
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox  # noqa: E402  reuse the one code path that is allowed to write T2

API = os.environ.get("GH_API_BASE", "https://api.github.com").rstrip("/")

# Outsider filter, not a human proof: keeps a drive-by commenter from injecting
# decisions. The maintainer and the agent share one identity here, so this says
# nothing about *who* between them wrote it — see the module docstring.
TRUSTED_ASSOCIATION = {"OWNER", "MEMBER", "COLLABORATOR"}
HUMAN_TURNS_FILE = ".agent/memory-human-turn"


def truthy(value):
    """Parse a boolean-ish env var.

    A bare `if os.environ.get(...)` treats "0" and "false" as on, which inverts
    any opt-in flag a config file or a shell writes as false. Same vocabulary the
    driver uses for MEMORY_DRY_RUN.
    """
    return (value or "").strip().lower() not in ("", "0", "false", "no", "off")


ADOPT_RE = re.compile(
    r"^\s*memory:\s*adopt\s+(?P<id>[0-9a-f]{6,40})\s+as\s+"
    r"[\"“](?P<rule>.+?)[\"”]\s*"
    r"(?:target=(?P<target>\S+))?\s*(?:area=(?P<area>\S+))?\s*"
    r"(?:supersedes=(?P<supersedes>\d+))?\s*$", re.I)
DISMISS_RE = re.compile(
    r"^\s*memory:\s*dismiss\s+(?P<id>[0-9a-f]{6,40})\s*:\s*(?P<reason>\S.*)$", re.I)
CORRECT_RE = re.compile(
    r"^\s*memory:\s*correct\s+(?P<id>[0-9a-f]{6,40})\s+as\s+"
    r"[\"“](?P<corrected>.+?)[\"”]\s*$", re.I)
DEFAULT_TARGET = ".agent/invariants.md"
# Only logins you name. Cannot be guessed from here: the factory reviewer posts
# as the same login as the maintainer, so a "[bot]" suffix heuristic would
# either miss it or reject the maintainer's own comments.
BOTS = {b for b in os.environ.get("MEMORY_REVIEW_BOTS", "").split(",") if b}


def token():
    for var in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        v = os.environ.get(var)
        if v:
            return v
    raise SystemExit("no GitHub token in the environment")


def gh(method, path, tok, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "memory-distiller-review"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "null")


def decision_lines(body):
    """Parse decision lines out of one comment body.

    Returns (decisions, malformed). A line that starts like a directive but does
    not parse is reported rather than dropped: silently ignoring a mistyped
    `memory: adopt` is how a decision you believe you made never happens.
    """
    decisions, malformed = [], []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate the backtick/list prefixes people type in the GitHub editor.
        line = re.sub(r"^[-*>]+\s*", "", line).strip()
        if not re.match(r"^`{0,2}memory\s*:", line, re.I):
            continue
        line = line.strip("`").strip()
        for kind, rx, key in (("adopt", ADOPT_RE, "id"),
                              ("dismiss", DISMISS_RE, "id"),
                              ("correct", CORRECT_RE, "id")):
            m = rx.match(line)
            if m:
                d = {k: v for k, v in m.groupdict().items() if v is not None}
                d["kind"] = kind
                decisions.append(d)
                break
        else:
            malformed.append(line)
    return decisions, malformed


def collect(repo, pr, tok):
    """Gather decision lines from both comment streams on the PR."""
    paths = [f"/repos/{repo}/issues/{pr}/comments",
             f"/repos/{repo}/pulls/{pr}/comments"]
    found, refused, malformed = [], [], []
    for path in paths:
        code, comments = gh("GET", path, tok)
        if code != 200:
            raise SystemExit(f"read {path} failed {code}: "
                             f"{(comments or {}).get('message', '')}")
        for c in comments or []:
            decisions, bad = decision_lines(c.get("body", ""))
            if not decisions and not bad:
                continue
            author = (c.get("user") or {}).get("login", "?")
            assoc = c.get("author_association", "NONE")
            if not decisions:
                malformed.extend(
                    {"line": b, "author": author} for b in bad)
                continue
            if assoc not in TRUSTED_ASSOCIATION or author in BOTS:
                refused.append({"author": author, "association": assoc,
                                "count": len(decisions),
                                "ids": [d["id"] for d in decisions]})
                continue
            for d in decisions:
                d.update({"author": author, "association": assoc,
                          "comment_id": c.get("id"), "created_at": c.get("created_at"),
                          "origin": "review" if "/pulls/" in path else "issue"})
            found.extend(decisions)
    # Oldest first: a later `correct` should see the earlier `adopt` already made,
    # and a re-decision should end up whatever the human said last.
    found.sort(key=lambda d: (d.get("created_at") or "", d.get("comment_id") or 0))
    return found, refused, malformed


def in_automation() -> str:
    """Non-empty when running as an unattended automation rather than a person's
    conversation. The automation service injects these into script runs."""
    return (os.environ.get("AUTOMATION_RUN_ID")
            or os.environ.get("AUTOMATION_CALLBACK_URL")
            or os.environ.get("AUTOMATION_KV_TOKEN") or "")


def claim_human_turn(repo_dir, decisions):
    """Record the comment that puts a human on record, for inbox's guard.

    Refuses under an automation context. Without that, the nightly driver could
    harvest comments, write the marker itself, and pass the very guard meant to
    require a person — self-approval through the back door. Applying decisions is
    therefore only possible from a live conversation a human is driving, which is
    the one signal here that the shared PAT cannot counterfeit.
    """
    if in_automation() and not truthy(os.environ.get("MEMORY_REVIEW_ALLOW_AUTOMATION")):
        raise SystemExit(
            "refusing to apply decisions from an unattended automation run "
            f"(run id {in_automation()!r}).\n"
            "A comment cannot prove a human is present when OpenHands posts with "
            "your own credential, so the presence check has to be the live "
            "session. Ask OpenHands to apply the memory decisions in a "
            "conversation you are driving. Set MEMORY_REVIEW_ALLOW_AUTOMATION=1 "
            "only when a person is genuinely watching this run.")
    last = decisions[-1]
    marker = {"via": "pr-comment", "author": last["author"],
              "author_association": last["association"],
              "comment_id": last["comment_id"], "ts": last["created_at"],
              "applied_at": inbox.now()}
    p = repo_dir / HUMAN_TURNS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n")
    return marker


def apply_one(repo_dir, d):
    """Execute one decision through inbox's own verbs' machinery.

    Deliberately not a reimplementation: adopting through append_invariant keeps
    the provenance line, the index row, and the audit record identical to what a
    supervised CLI adoption would have produced.
    """
    f = inbox.cand_path(repo_dir, d["id"])
    text = f.read_text()
    fields = inbox.read_front(text)
    status = fields.get("status")

    # Idempotence, and it has to live here: the nightly harvester re-reads the
    # same PR comments on every pass, so an already-decided candidate must not
    # append its invariant a second time. `corrected` is exempt because
    # correcting and then adopting is the documented two-step, not a re-decision.
    if status in ("adopted", "dismissed"):
        return ("skipped", f"{fields.get('id')} already {status}")

    if d["kind"] == "adopt":
        target = repo_dir / (d.get("target") or DEFAULT_TARGET)
        if not target.exists():
            return ("error", f"T2 target does not exist: {d.get('target') or DEFAULT_TARGET}")
        if not fields.get("source_file"):
            return ("error", "candidate has no source_file; refusing an unsourced invariant")
        if not (d.get("rule") or "").strip():
            return ("error", "empty rule text")
        num = inbox.append_invariant(
            target, d["rule"], d.get("supersedes"), fields)
        f.write_text(inbox.set_fields(text, {
            "status": "adopted", "adopted_to": f"{target.name}#inv-{num}",
            "adopted_at": inbox.now()}))
        inbox.audit(repo_dir, {"action": "adopt", "id": fields.get("id"),
                               "target": str(target.name), "invariant": num,
                               "source": fields.get("source_file"),
                               "supersedes": d.get("supersedes"),
                               "via": "pr-comment", "decided_by": d["author"]})
        return ("applied", f"{fields.get('id')} -> {target.name}#inv-{num}")
    if d["kind"] == "dismiss":
        f.write_text(inbox.set_fields(text, {
            "status": "dismissed", "dismissed_reason": d["reason"],
            "dismissed_at": inbox.now()}))
        inbox.audit(repo_dir, {"action": "dismiss", "id": fields.get("id"),
                               "reason": d["reason"],
                               "source": fields.get("source_file"),
                               "via": "pr-comment", "decided_by": d["author"]})
        return ("applied", f"dismissed {fields.get('id')}")
    # correct
    if "## Correction" not in text:
        text = text.rstrip("\n") + (f"\n\n## Correction ({inbox.now()})\n\n"
                                    f"> {d['corrected']}\n")
    f.write_text(inbox.set_fields(text, {"status": "corrected",
                                         "corrected_text": d["corrected"][:400],
                                         "corrected_at": inbox.now()}))
    inbox.audit(repo_dir, {"action": "correct", "id": fields.get("id"),
                           "source": fields.get("source_file"),
                           "via": "pr-comment", "decided_by": d["author"]})
    return ("applied", f"corrected {fields.get('id')}; adopt it to promote the wording")


def pending_candidates(repo_dir, limit=25):
    """Pending candidates, highest durability first, for the PR body."""
    import glob as _glob
    rows = []
    for path in sorted(_glob.glob(str(repo_dir / inbox.CANDIDATES_DIR / "*.md"))):
        text = Path(path).read_text()
        try:
            f = inbox.read_front(text)
        except SystemExit:
            continue
        if f.get("status") != "pending":
            continue
        quote = ""
        m = re.search(r"^> (.*)$", text, re.M)
        if m:
            quote = m.group(1).strip()
        rows.append({"id": f.get("id", Path(path).stem),
                     "area": f.get("area", "learned"),
                     "durability": f.get("durability_score", 0),
                     "evidence_class": f.get("evidence_class", "?"),
                     "source": f.get("source_file", "?"),
                     "section": f.get("section", "?"),
                     "host_bound": f.get("promotability", "") == "host-bound",
                     "quote": quote[:240]})
    rows.sort(key=lambda r: (r["host_bound"], -float(r["durability"] or 0)))
    return rows[:limit]


def render_body(repo_dir, counts):
    """The PR body a maintainer can act on from the browser.

    The whole point is that review happens in GitHub's UI, so the body has to
    carry the decision syntax and the candidate ids: without them there is
    nothing to copy and the PR is just a wall of frontmatter.
    """
    rows = pending_candidates(repo_dir)
    lines = ["Nightly memory consolidation. Candidates are proposals, not policy:",
             "nothing here reaches an agent's prompt until it is adopted.", ""]
    lines.append(f"- threads ingested: {counts.get('threads_ingested', 0)}")
    lines.append(f"- candidates written: {counts.get('candidates_written', 0)}")
    lines.append(f"- host-bound quarantined: {counts.get('host_bound_quarantined', 0)}")
    lines.append(f"- noise rejected: {counts.get('noise_rejected', 0)}")
    lines.append("")
    if not rows:
        lines.append("_No pending candidates._")
        return "\n".join(lines)
    lines.append(f"**{len(rows)} awaiting a decision.** Comment on this PR with one "
                 "line per candidate — an OpenHands session applies them, so you "
                 "never need a terminal:")
    lines.append("")
    lines.append("```")
    lines.append('memory: adopt <id> as "<the rule, written as a rule>"')
    lines.append("memory: dismiss <id>: <why this is not durable>")
    lines.append('memory: correct <id> as "<corrected wording>"')
    lines.append("```")
    lines.append("")
    lines.append("| id | area | dur | evidence | source | |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        flag = "⚠ host-bound — verify on the device first" if r["host_bound"] else ""
        lines.append(f"| `{r['id']}` | {r['area']} | {r['durability']} | "
                     f"{r['evidence_class']} | `{r['source']}` §{r['section']} | {flag} |")
    lines.append("")
    for r in rows:
        if r["quote"]:
            lines.append(f"- `{r['id']}` — {r['quote']}")
    lines.append("")
    lines.append("Write the adopted text as a rule, not a report line: "
                 "`\"All MA calls go through app/ma/\"`, not "
                 "`\"U3 routed MA calls through app/ma/\"` — this becomes what "
                 "future agents read as fact.")
    return "\n".join(lines)


def repo_from(repo_dir, override=None):
    if override:
        return override
    cfg = repo_dir / ".agent/memory-config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text()).get("repo")
        except json.JSONDecodeError:
            pass
    return None


def open_pr(repo, tok, branch):
    owner = repo.split("/")[0]
    code, pulls = gh("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}", tok)
    if code == 200 and pulls:
        return pulls[0]["number"]
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--repo", help="owner/name (default: config 'repo')")
    ap.add_argument("--pr", type=int, help="consolidation PR number (default: the open one)")
    ap.add_argument("--branch", default="openhands/memory-consolidation")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    mode.add_argument("--apply", action="store_true", help="execute the decisions")
    args = ap.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()
    repo = repo_from(repo_dir, args.repo)
    if not repo:
        raise SystemExit("repo must be owner/name (set config 'repo' or --repo)")
    tok = token()
    pr = args.pr
    if pr is None:
        pr = open_pr(repo, tok, args.branch)
        if pr is None:
            print(json.dumps({"action": "review", "repo": repo, "pr": None,
                              "decisions": [], "note": "no open consolidation PR"}))
            return 0

    decisions, refused, malformed = collect(repo, pr, tok)
    out = {"action": "review", "repo": repo, "pr": pr,
           "decisions": [], "refused": refused, "malformed": malformed}
    if not decisions:
        out["applied"] = 0
        print(json.dumps(out, indent=2))
        return 1 if malformed else 0

    if args.dry_run:
        out["applied"] = 0
        out["decisions"] = [{"kind": d["kind"], "id": d["id"], "by": d["author"],
                             "at": d.get("created_at")} for d in decisions]
        print(json.dumps(out, indent=2))
        return 0

    claim_human_turn(repo_dir, decisions)
    inbox.require_human_turn(repo_dir, "apply", False)
    tally = {"applied": 0, "error": 0}
    for d in decisions:
        try:
            state, detail = apply_one(repo_dir, d)
        except SystemExit as exc:      # a bad candidate must not stop the rest
            state, detail = "error", str(exc)
        except Exception as exc:       # noqa: BLE001
            state, detail = "error", f"{type(exc).__name__}: {exc}"
        tally[state] = tally.get(state, 0) + 1
        out["decisions"].append({"kind": d["kind"], "id": d["id"],
                                 "by": d["author"], "result": state, "detail": detail})
    out["applied"] = tally.get("applied", 0)
    out["errors"] = tally.get("error", 0)
    print(json.dumps(out, indent=2))
    return 1 if tally.get("error") or malformed else 0


if __name__ == "__main__":
    sys.exit(main())
