#!/usr/bin/env python3
"""Label vocabulary: the pipeline state a maintainer can read in the GitHub UI.

Three labels, each answering a different question. Keep them distinct — the
first is a request for work, the other two are the memory pipeline's state.

  needs-host-tools      someone owes this issue a device/host pass
  memory-needs-review   a written-up report is posted and awaits the nightly run
  memory-ingested       the report has been distilled into candidates

The transitions are owned by threads.py: a pending issue carries
`memory-needs-review`, and a successful ingest adds `memory-ingested` and
removes `memory-needs-review`. Re-adding `memory-needs-review` (after posting
another report) makes it pending again while `memory-ingested` records that it
has been through before — so the two together distinguish "never processed"
from "processed, and there is more".

Why labels and not just local state: the label is the only marker that survives
a fresh sandbox and is visible without running anything. The comment-id ledger
in .agent/threads/.ingested.json still de-duplicates within an issue; labels
select *which* issues are worth fetching at all.

  labels.py --repo-dir D status              # the state, read from labels
  labels.py --repo-dir D ensure              # create the labels if missing
  labels.py --repo-dir D show --issue N
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PENDING_LABEL = "memory-needs-review"
INGESTED_LABEL = "memory-ingested"
HOST_LABEL = "needs-host-tools"

DEFINITIONS = {
    PENDING_LABEL: ("FB8C00", "Written-up report posted; awaiting the nightly distill"),
    INGESTED_LABEL: ("0E8801", "Report distilled into memory candidates"),
    HOST_LABEL: ("D4C5F9", "Needs a device/host pass the sandbox cannot run"),
}

# Overridable for GitHub Enterprise and for the fixture server in tests.
API = os.environ.get("GH_API_BASE", "https://api.github.com").rstrip("/")


def repo_from(repo_dir: Path, override=None):
    if override:
        return override
    cfg = repo_dir / ".agent/memory-config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text()).get("repo")
        except json.JSONDecodeError:
            pass
    return None


def token(args) -> str:
    if getattr(args, "token_file", None):
        return Path(args.token_file).read_text().strip()
    tok = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
    if not tok:
        raise SystemExit("GITHUB_PERSONAL_ACCESS_TOKEN is not set")
    return tok


def gh(method: str, path: str, tok: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "memory-distiller"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def labels_on(issue: dict):
    return {l["name"] for l in issue.get("labels", [])}


def set_labels(repo: str, number: int, tok: str, add=(), remove=()):
    """Add/remove labels, preserving all others. Returns (ok, detail).

    GitHub replaces the whole set on PUT, so read the current set first: a
    blind PUT would silently drop labels this tool does not own.
    """
    code, issue = gh("GET", f"/repos/{repo}/issues/{number}", tok)
    if code != 200:
        return False, f"read failed {code}"
    current = labels_on(issue)
    want = (current | set(add)) - set(remove)
    if want == current:
        return True, "unchanged"
    code, body = gh("PATCH", f"/repos/{repo}/issues/{number}", tok,
                    {"labels": sorted(want)})
    if code != 200:
        return False, f"write failed {code}: {(body or {}).get('message', '')}"
    return True, "ok"


def cmd_status(args):
    repo_dir = Path(args.repo_dir).resolve()
    repo = repo_from(repo_dir, args.repo)
    if not repo:
        raise SystemExit("repo must be owner/name (set config 'repo' or --repo)")
    tok = token(args)
    # Always send state explicitly: omitting it makes GitHub default to `open`,
    # so `--state all` would silently scan only open issues and report an empty
    # pipeline.
    q = f"&state={args.state}"
    code, issues = gh("GET", f"/repos/{repo}/issues?per_page=100{q}", tok)
    if code != 200:
        raise SystemExit(f"list issues failed {code}: {(issues or {}).get('message','')}")
    issues = [i for i in issues if "pull_request" not in i]

    buckets = {"awaiting_host_pass": [], "pending_ingest": [],
               "ingested": [], "ingested_more_pending": [], "unlabelled": []}
    for i in issues:
        lab = labels_on(i)
        row = {"number": i["number"], "state": i["state"],
               "title": i["title"][:80], "comments": i["comments"],
               "labels": sorted(lab)}
        pending, done = PENDING_LABEL in lab, INGESTED_LABEL in lab
        if pending and done:
            buckets["ingested_more_pending"].append(row)
        elif pending:
            buckets["pending_ingest"].append(row)
        elif done:
            buckets["ingested"].append(row)
        elif HOST_LABEL in lab:
            buckets["awaiting_host_pass"].append(row)
        else:
            buckets["unlabelled"].append(row)

    out = {"action": "status", "repo": repo, "issues_scanned": len(issues)}
    out.update({k: len(v) for k, v in buckets.items()})
    if args.verbose:
        out["details"] = buckets
    print(json.dumps(out, indent=2))
    return 0


def cmd_ensure(args):
    repo_dir = Path(args.repo_dir).resolve()
    repo = repo_from(repo_dir, args.repo)
    if not repo:
        raise SystemExit("repo must be owner/name (set config 'repo' or --repo)")
    tok = token(args)
    results = {}
    for name, (color, desc) in DEFINITIONS.items():
        code, _ = gh("GET", f"/repos/{repo}/labels/{name}", tok)
        if code == 200:
            results[name] = "exists"
            continue
        code, body = gh("POST", f"/repos/{repo}/labels", tok,
                        {"name": name, "color": color, "description": desc})
        results[name] = "created" if code == 201 else f"failed {code}: {(body or {}).get('message','')}"
    print(json.dumps({"action": "ensure", "repo": repo, "labels": results}, indent=2))
    bad = [k for k, v in results.items() if v.startswith("failed")]
    return 1 if bad else 0


def cmd_show(args):
    repo_dir = Path(args.repo_dir).resolve()
    repo = repo_from(repo_dir, args.repo)
    tok = token(args)
    code, issue = gh("GET", f"/repos/{repo}/issues/{args.issue}", tok)
    if code != 200:
        raise SystemExit(f"read failed {code}")
    lab = labels_on(issue)
    print(json.dumps({
        "action": "show", "repo": repo, "issue": args.issue,
        "title": issue["title"], "state": issue["state"], "labels": sorted(lab),
        "pipeline": ("pending_ingest" if PENDING_LABEL in lab else
                     "ingested" if INGESTED_LABEL in lab else
                     "awaiting_host_pass" if HOST_LABEL in lab else "unlabelled"),
    }, indent=2))
    return 0


def _local_ingested(repo_dir: Path):
    """Issue numbers with a thread file, or named in the ledger.

    Local files are the source of truth for what was actually ingested: a label
    is only a pointer to them, and can outlive them if a consolidation PR is
    closed without merging.
    """
    found = set()
    threads = repo_dir / ".agent" / "threads"
    if threads.is_dir():
        for p in threads.glob("*.md"):
            if p.stem.isdigit():
                found.add(int(p.stem))
    ledger = threads / ".ingested.json"
    if ledger.exists():
        try:
            state = json.loads(ledger.read_text())
        except json.JSONDecodeError:
            state = {}
        for key in list(state.get("issues", {})) + list(state.get("comments", {})):
            if str(key).isdigit():
                found.add(int(key))
    return found


def cmd_reconcile(args):
    """Repair label drift between GitHub and the local thread files.

    The nightly run labels an issue `memory-ingested` the moment it writes the
    thread file, but that file only reaches the default branch when a human
    merges the consolidation PR. If the PR is closed instead, the label stays and
    the content never arrives — and because that label is what keeps issues out of
    the queue, the issue becomes permanently invisible with no way back.

    Local files decide. `memory-ingested` without a file is stripped (the issue
    re-enters the pending queue); a file without the label gets it. Run before
    ingest so drift cannot accumulate.
    """
    repo_dir = Path(args.repo_dir).resolve()
    repo = repo_from(repo_dir, args.repo)
    if not repo:
        raise SystemExit("repo must be owner/name (set config 'repo' or --repo)")
    tok = token(args)
    local = _local_ingested(repo_dir)

    q = f"&state={args.state}&labels={INGESTED_LABEL}"
    code, labelled = gh("GET", f"/repos/{repo}/issues?per_page=100{q}", tok)
    if code != 200:
        raise SystemExit(f"list failed {code}: {(labelled or {}).get('message','')}")
    labelled = [i for i in (labelled or []) if "pull_request" not in i]

    orphans, relabelled = [], []
    for i in labelled:
        if i["number"] not in local:
            orphans.append(i["number"])
            if not args.dry_run:
                ok, detail = set_labels(repo, i["number"], tok,
                                        add=[PENDING_LABEL], remove=[INGESTED_LABEL])
                if not ok:
                    raise SystemExit(f"reclaim #{i['number']} failed: {detail}")

    if args.include_unlabelled:
        # Search API because the list endpoint has no exclude-label parameter.
        code, body = gh("GET",
                        f"/search/issues?q=repo:{repo}+is:issue+state:open"
                        f"+-label:{INGESTED_LABEL}&per_page=100", tok)
        if code == 200:
            for i in body.get("items", []):
                if i["number"] in local and INGESTED_LABEL not in labels_on(i):
                    relabelled.append(i["number"])
                    if not args.dry_run:
                        ok, detail = set_labels(repo, i["number"], tok,
                                                add=[INGESTED_LABEL])
                        if not ok:
                            raise SystemExit(
                                f"relabel #{i['number']} failed: {detail}")
        elif args.verbose:
            print(f"unlabelled scan skipped ({code})", file=sys.stderr)

    print(json.dumps({
        "action": "reconcile", "repo": repo, "local_thread_files": len(local),
        "labelled_ingested": len(labelled),
        "reclaimed_to_pending": orphans, "relabelled_ingested": relabelled,
        "dry_run": args.dry_run,
    }, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--repo")
    ap.add_argument("--token-file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="summarise pipeline state from labels")
    s.add_argument("--state", default="open")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_status)
    e = sub.add_parser("ensure", help="create the three labels if missing")
    e.set_defaults(func=cmd_ensure)
    w = sub.add_parser("show", help="one issue's pipeline state")
    w.add_argument("--issue", type=int, required=True)
    w.set_defaults(func=cmd_show)
    r = sub.add_parser("reconcile",
                       help="repair label drift against the local thread files")
    r.add_argument("--state", default="all", choices=["open", "closed", "all"])
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--include-unlabelled", action="store_true",
                   help="also label ingested issues that lost their label "
                        "(extra Search API call)")
    r.add_argument("--verbose", action="store_true")
    r.set_defaults(func=cmd_reconcile)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
