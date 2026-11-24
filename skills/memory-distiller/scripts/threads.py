#!/usr/bin/env python3
"""Ingest written-up host reports from issue threads into the T0 corpus.

This is what closes the device-verification gap without making a human a
dependency of the memory system. When a bug can only be reproduced on a device
or another host, the run is done out-of-band and its report is posted to the
issue; this tool pulls those reports into `.agent/threads/<n>.md` so the next
`surface` pass treats them as ordinary T0 evidence.

Stdlib only. Requires GITHUB_PERSONAL_ACCESS_TOKEN in the environment (or pass
--token-file). Writes one file per issue, idempotently, and records the comment
ids it has already ingested so a re-run only fetches new comments.

  threads.py --repo-dir D [--config F] [--label memory-report] [--state all]
             [--limit N] [--dry-run] [--token-file PATH]
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from labels import INGESTED_LABEL, PENDING_LABEL, set_labels

THREADS_DIR = ".agent/threads"
STATE_FILE = ".agent/threads/.ingested.json"
# Overridable for GitHub Enterprise and for the fixture server in tests.
API = os.environ.get("GH_API_BASE", "https://api.github.com").rstrip("/")

# A thread report is only evidence if it reads like a report. These markers are
# the minimum: a bare "confirmed fixed" comment must not become an invariant.
REPORT_MARKERS = ("observed", "gates", "verified", "reproduc", "root cause",
                  "steps to reproduce", "environment", "device", "host")
MIN_REPORT_CHARS = 600


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_get(path: str, token: str):
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "memory-distiller"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def load_state(repo_dir: Path) -> dict:
    p = repo_dir / STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"issues": {}, "comments": {}}


def save_state(repo_dir: Path, state: dict):
    p = repo_dir / STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def looks_like_report(body: str) -> bool:
    if len(body) < MIN_REPORT_CHARS:
        return False
    t = body.lower()
    return sum(1 for m in REPORT_MARKERS if m in t) >= 2


def token_from(args) -> str:
    if args.token_file:
        return Path(args.token_file).read_text().strip()
    tok = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
    if not tok:
        raise SystemExit(
            "GITHUB_PERSONAL_ACCESS_TOKEN is not set. The thread ingest reads "
            "issue comments, so it needs a token with Issues: read.")
    return tok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--repo", help="owner/name (default: config 'repo')")
    ap.add_argument("--label", default=None,
                    help=f"queue label (default {PENDING_LABEL})")
    ap.add_argument("--no-labels", action="store_true",
                    help="ignore labels entirely: select by state and do not "
                         "relabel. For a first backfill only.")
    ap.add_argument("--skip-labeling", action="store_true",
                    help="select by the queue label but do not relabel")
    ap.add_argument("--state", default="all", choices=["open", "closed", "all"])
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--token-file")
    args = ap.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()
    cfg_path = repo_dir / ".agent/memory-config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    repo = args.repo or cfg.get("repo")
    if not repo or "/" not in repo:
        raise SystemExit("repo must be owner/name (set config 'repo' or --repo)")
    token = token_from(args)
    state = load_state(repo_dir)

    # Default selection is the pending queue: `memory-needs-review`. Because a
    # successful run swaps that for `memory-ingested`, already-processed issues
    # drop out of the query with no exclusion filter needed. `--label` overrides
    # the queue label; `--no-labels` selects by state alone for a first backfill.
    q = f"&state={args.state}"
    queue_label = None if args.no_labels else (args.label or PENDING_LABEL)
    if queue_label:
        q += f"&labels={queue_label}"
    issues = gh_get(f"/repos/{repo}/issues?per_page={args.limit}{q}&sort=updated",
                    token)
    d = repo_dir / THREADS_DIR
    if not args.dry_run:
        d.mkdir(parents=True, exist_ok=True)

    written, skipped_short, dup = [], 0, 0
    for it in issues:
        if "pull_request" in it:
            continue
        number = it["number"]
        comments = gh_get(f"/repos/{repo}/issues/{number}/comments?per_page=100",
                          token)
        reports = [c for c in comments if looks_like_report(c.get("body") or "")]
        if not reports:
            continue
        seen = set(state["comments"].get(str(number), []))
        new = [c for c in reports if str(c["id"]) not in seen]
        if not new:
            dup += 1
            continue
        skipped_short += len(reports) - len(new)
        title = it["title"]
        header = [
            f"# Issue thread report — #{number} {title}",
            "",
            f"source: https://github.com/{repo}/issues/{number}",
            f"repo: {repo}",
            f"state: {it['state']}",
            f"labels: {', '.join(l['name'] for l in it.get('labels', [])) or '(none)'}",
            f"ingested: {now()}",
            "",
            "Written up out-of-band (a host, a device, another machine) and posted",
            "to the issue thread. Treat as ordinary T0 evidence: the run already",
            "happened, so claims here are settleable without a new dependency.",
            "",
        ]
        # Built once per comment and joined whole, so the append path never has
        # to re-derive or search for a header string.
        blocks = ["\n".join([
            f"## Comment {c['id']} — {c['user']['login']} {c['created_at']}",
            "",
            c["body"].strip(),
            "",
        ]) for c in new]
        f = d / f"{number}.md"
        if not args.dry_run:
            # Append new comments rather than overwriting: earlier comments in
            # the same thread may already be cited by a live invariant.
            if f.exists():
                with f.open("a") as fh:
                    fh.write("\n" + "\n".join(blocks).rstrip("\n") + "\n")
            else:
                f.write_text("\n".join(header + blocks) + "\n")
            state["comments"].setdefault(str(number), []).extend(
                str(c["id"]) for c in new)
            state["issues"][str(number)] = {"title": title, "ingested_at": now(),
                                            "comments": len(state["comments"][str(number)])}
        entry = {"issue": number, "new_comments": len(new),
                 "chars": sum(len(c["body"]) for c in new)}
        if not args.dry_run and not args.no_labels and not args.skip_labeling:
            # The queue label comes off and the ingested one goes on, so the
            # next run's query excludes this issue without an exclusion filter.
            ok, detail = set_labels(repo, number, token,
                                    add=[INGESTED_LABEL], remove=[PENDING_LABEL])
            entry["labels"] = "ok" if ok else f"FAILED: {detail}"
        written.append(entry)

    if not args.dry_run:
        save_state(repo_dir, state)
    print(json.dumps({
        "action": "threads", "repo": repo,
        "queue_label": queue_label,
        "labels_written": not (args.no_labels or args.skip_labeling),
        "issues_scanned": len([i for i in issues if "pull_request" not in i]),
        "threads_written": len(written), "already_ingested": dup,
        "threads_dir": THREADS_DIR, "dry_run": args.dry_run,
        "details": written,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
