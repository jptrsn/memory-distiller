#!/usr/bin/env python3
"""Surface distillation candidates from T0 reports into the T1 inbox.

Reads the repo's T0 reports, splits them into sections, and writes one small
Markdown file per candidate under .agent/candidates/. Deterministic: no LLM
call lives in this tool. Re-running is safe — candidate ids are content
digests, so a report that has already been surfaced produces no duplicates and
an edited report produces exactly one new candidate.

Stdlib only, in the house style of scripts/emit_unit_issues.py: argparse in,
one JSON document out, non-zero only on its own errors.

  distill.py surface [--repo-dir D] [--config F] [--since DATE] [--dry-run]
  distill.py status  [--repo-dir D] [--config F]
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiers import (  # noqa: E402
    CANDIDATES_DIR,
    WITNESSED_CLASSES,
    bullets,
    corroboration,
    digest,
    findings_in,
    host_bound,
    split_sections,
)

DEFAULT_CONFIG = ".agent/memory-config.json"
QUARANTINE_DIR = ".agent/candidates/quarantined"

# A candidate is only worth a human's time if it reads like a durable claim,
# not a bookkeeping line. These reject the obvious noise; the human still
# decides on everything that survives.
SKIP = [
    re.compile(r"^\s*$"),
    re.compile(r"^\s*(none|n/?a|none\.|nothing)\b", re.I),
    re.compile(r"^(all|both) gates? (green|pass)", re.I),
    re.compile(r"^no (changes|findings|deviations|issues) \b", re.I),
    re.compile(r"^\s*(see|per) (the )?(report|section|spec|PR)\b", re.I),
    re.compile(r"^commit \b", re.I),
    # Review-round disposition lines are bookkeeping ("fixed (SHA)"), not claims;
    # the reviewer's finding is the candidate and it arrives via the review
    # station, not by archaeology through three rounds of a report.
    re.compile(r"^FINDING\s+\S+\s*(\(|:)", re.I),
    re.compile(r"^`?[0-9a-f]{7,40}`?\s+[a-z]+\(", re.I),  # conventional-commit line
    re.compile(r"^https?://\S+$"),
]
# Signals that a claim is durable enough to become an invariant.
DURABLE = [
    (re.compile(r"\b(always|never|must|must not|forbidden|only|sole|single source)\b", re.I), 3),
    (re.compile(r"\b(root cause|stale|drift|tripwire|regression|invariant)\b", re.I), 2),
    (re.compile(r"\b(pre-existing|flaky|ephemeral|manual|human)\b", re.I), 1),
]


def load_config(repo_dir: Path, config: str):
    path = repo_dir / config
    if not path.exists():
        raise SystemExit(f"config not found: {path}")
    cfg = json.loads(path.read_text())
    if "roots" not in cfg:
        raise SystemExit(f"{path}: missing 'roots'")
    return cfg


def t0_files(repo_dir: Path, cfg):
    out = []
    for origin, spec in cfg["roots"].items():
        if spec.get("tier") != "T0":
            continue
        base = spec.get("glob") or spec.get("path")
        if not base:
            continue
        pat = base if "*" in base else base.rstrip("/") + "/**/*.md"
        for p in sorted(repo_dir.glob(pat)):
            if not p.is_file() or "archived" in p.parts:
                continue
            out.append((origin, spec, p))
    return out


def score(text: str) -> int:
    return sum(w for rx, w in DURABLE if rx.search(text))


def is_noise(text: str) -> bool:
    return any(rx.search(text) for rx in SKIP)


def candidate_id(origin: str, rel: str, section: str, item: str) -> str:
    h = digest(f"{origin}|{rel}|{section}|{item}")
    return h.split(":")[1][:12]


def read_inbox(repo_dir: Path):
    d = repo_dir / CANDIDATES_DIR
    seen = {}
    if not d.exists():
        return seen
    for f in d.glob("*.md"):
        head = f.read_text(errors="replace")
        m = re.search(r"^id:\s*(\S+)", head, re.M)
        if m:
            seen[m.group(1)] = f
        st = re.search(r"^status:\s*(\S+)", head, re.M)
        if st and m:
            seen[m.group(1)] = f
    return seen


def cmd_surface(args):
    repo_dir = Path(args.repo_dir).resolve()
    cfg = load_config(repo_dir, args.config)
    inbox = read_inbox(repo_dir)
    since = date.fromisoformat(args.since) if args.since else None

    include = set(cfg.get("include_classes") or
                   ["unverified", "deviation", "assumption", "unknown",
                    "blocker", "correction", "finding", "review-round", "supervised", "root-cause",
                    "test-blind-spot", "triage-answer"])
    written, skipped_dup, skipped_noise, quarantined = [], 0, 0, []
    for origin, spec, path in t0_files(repo_dir, cfg):
        rel = str(path.relative_to(repo_dir))
        if since is not None and date.fromtimestamp(path.stat().st_mtime) < since:
            continue
        text = path.read_text(errors="replace")
        src_digest = digest(text)
        unit = spec.get("kind", "report")
        corrob = corroboration(text)
        # Build-step narration is over half the volume and is usually not a
        # durable claim, so it is opt-in per repo via include_classes.
        for section, cls, is_candidate, body, line in split_sections(text):
            if not is_candidate or cls not in include:
                continue
            for item in bullets(body):
                if is_noise(item):
                    skipped_noise += 1
                    continue
                # Refuse to create a candidate that can only be settled by a
                # device/host run: such claims must arrive written-up (an issue
                # thread report), never as work deferred to a human. Witnessed
                # outcome classes are exempt — the run already happened.
                markers = [] if cls in WITNESSED_CLASSES else host_bound(
                    item, cfg.get("host_markers"))
                if markers and not args.keep_host_bound:
                    quarantined.append({"id": candidate_id(origin, rel, section, item),
                                        "source": rel, "section": section,
                                        "evidence_class": cls, "markers": markers,
                                        "chars": len(item)})
                    continue
                cid = candidate_id(origin, rel, section, item)
                if cid in inbox:
                    skipped_dup += 1
                    continue
                if len(item) < args.min_chars:
                    continue
                prov = {
                    "repo": cfg.get("repo", repo_dir.name),
                    "corroboration": corrob,
                    "source_file": rel,
                    "source_digest": src_digest,
                    "section": section,
                    "line": line,
                    "origin": origin,
                    "unit": unit,
                    "evidence_class": cls,
                }
                finding = findings_in(body)
                if finding:
                    prov["findings"] = [f[2] for f in finding][:5]
                extra = cfg.get("provenance", {}).get(origin, [])
                body_md = render_candidate(cid, item, cls, prov, score(item), extra)
                target = repo_dir / CANDIDATES_DIR / f"{cid}.md"
                if not args.dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(body_md)
                written.append(
                    {"id": cid, "source": rel, "section": section,
                     "evidence_class": cls, "chars": len(item)}
                )
    audit = {
        "action": "surface",
        "repo": cfg.get("repo", repo_dir.name),
        "date": date.today().isoformat(),
        "written": len(written),
        "already_in_inbox": skipped_dup,
        "noise_rejected": skipped_noise,
        "host_bound_quarantined": len(quarantined),
    }
    if not args.dry_run and written:
        append_audit(repo_dir, audit)
    if quarantined and not args.dry_run:
        write_quarantine(repo_dir, quarantined)
    print(json.dumps({**audit, "dry_run": args.dry_run, "candidates": written,
                      "quarantined": quarantined}, indent=2))
    return 0


def write_quarantine(repo_dir: Path, items):
    """Quarantine is a queue, not a verdict.

    These are claims that name a host-only surface (a device, a container, an
    IDF build). They are kept so the gap is visible and auditable, but they are
    never adoptable: the fix is a written-up report on the issue, not a human
    becoming a dependency of the memory system.
    """
    d = repo_dir / QUARANTINE_DIR
    d.mkdir(parents=True, exist_ok=True)
    for q in items:
        f = d / f"{q['id']}.md"
        if f.exists():
            continue
        f.write_text(
            "---\n"
            f"id: {q['id']}\n"
            "status: quarantined_host_bound\n"
            f"evidence_class: {q['evidence_class']}\n"
            f"repo: {q['source'].split('/')[0]}\n"
            f"source_file: {q['source']}\n"
            f"section: {q['section']}\n"
            f"host_markers: {', '.join(q['markers'])}\n"
            "surfaced: " + date.today().isoformat() + "\n"
            "---\n\n"
            "# Host-bound — not adoptable\n\n"
            "This candidate names a surface that only a host/device run can "
            "settle.\nDo not adopt it. Post the run's report to the issue thread "
            "and let\n`surface` pick it up as an ordinary candidate.\n\n"
            f"Markers: {', '.join('`%s`' % m for m in q['markers'])}\n"
        )


def render_candidate(cid, item, cls, prov, s, extra_fields):
    """A T1 candidate: small, self-contained, carrying its own provenance.

    The claim is quoted verbatim — the distiller never rewrites a claim, only
    classifies it. Provenance is what makes an adopt/dismiss decision
    reviewable months later.
    """
    lines = [
        "---",
        f"id: {cid}",
        "status: pending",
        f"evidence_class: {cls}",
        f"promotability: {'promotable' if cls in ('observed', 'correction', 'supervised', 'blocker', 'review-round') else 'provisional'}",
        f"durability_score: {s}",
        f"repo: {prov['repo']}",
        f"source_file: {prov['source_file']}",
        f"source_digest: {prov['source_digest']}",
        f"section: {prov['section']}",
        f"line: {prov['line']}",
        f"origin: {prov['origin']}",
        "surfaced: " + date.today().isoformat(),
        "adopted_to: null",
        "dismissed_reason: null",
        "---",
        "",
        f"# {prov['unit']} — {prov['section']}",
        "",
        "> " + item.replace("\n", " "),
        "",
        "## Adopt / dismiss / correct",
        "",
        "- `adopt` — promote the quote below verbatim into the T2 target named",
        "  in the skill's tier map, then set `status: adopted` and `adopted_to:`.",
        "- `dismiss` — set `status: dismissed` with `dismissed_reason:` (required).",
        "- `correct` — set `status: corrected`, then adopt the corrected wording,",
        "  keeping the quote above untouched as the record of what was reported.",
        "",
        "## Provenance",
        "",
        f"- source: `{prov['source_file']}` § {prov['section']} (line {prov['line']})",
        f"- source digest: `{prov['source_digest']}`",
        f"- evidence class: `{cls}` — {'action taken, result seen' if cls == 'observed' else 'not an OBSERVED entry; provisional only'}",
    ]
    if prov.get("findings"):
        lines.append("- review verdicts: " + "; ".join(prov["findings"]))
    for bit in prov.get("corroboration", [])[:4]:
        lines.append(f"- corroboration — {bit}")
    for key in extra_fields:
        if key in prov:
            lines.append(f"- {key}: {prov[key]}")
    return "\n".join(lines) + "\n"


def append_audit(repo_dir: Path, record: dict):
    from tiers import AUDIT_LOG

    p = repo_dir / AUDIT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("# Memory audit\n\nAppend-only. One line per distiller/inbox action.\n\n")
    with p.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def cmd_status(args):
    repo_dir = Path(args.repo_dir).resolve()
    cfg = load_config(repo_dir, args.config)
    d = repo_dir / CANDIDATES_DIR
    counts, pending = {}, []
    if d.exists():
        for f in sorted(d.glob("*.md")):
            t = f.read_text(errors="replace")
            st = (re.search(r"^status:\s*(\S+)", t, re.M) or [None, "pending"])[1]
            counts[st] = counts.get(st, 0) + 1
            if st == "pending":
                eid = (re.search(r"^evidence_class:\s*(\S+)", t, re.M) or [None, "?"])[1]
                src = (re.search(r"^source_file:\s*(\S+)", t, re.M) or [None, "?"])[1]
                cid = (re.search(r"^id:\s*(\S+)", t, re.M) or [None, "?"])[1]
                pending.append({"id": cid, "evidence_class": eid, "source": src})
    t0 = len(t0_files(repo_dir, cfg))
    retired = len(list((repo_dir / ".agent/reports/archived").glob("*.md"))) if (
        repo_dir / ".agent/reports/archived"
    ).exists() else 0
    q = repo_dir / QUARANTINE_DIR
    qn = len(list(q.glob("*.md"))) if q.exists() else 0
    print(json.dumps({"repo": cfg.get("repo"), "t0_reports": t0,
                      "retired_reports": retired, "inbox": counts,
                      "host_bound_quarantined": qn, "pending": pending}, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("surface")
    s.add_argument("--since", help="ISO date; skip reports modified earlier")
    s.add_argument("--min-chars", type=int, default=40)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--keep-host-bound", action="store_true",
                   help="surface host-bound candidates instead of quarantining")
    s.set_defaults(func=cmd_surface)
    st = sub.add_parser("status")
    st.set_defaults(func=cmd_status)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
