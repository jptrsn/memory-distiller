#!/usr/bin/env python3
"""Work the T1 inbox one candidate at a time: adopt / dismiss / correct.

Every verb acts on exactly one candidate, refuses to run unattended (no
adopt/dismiss/correct without a human turn on record), never edits T0, and
appends one line to the audit log. The T2 file is edited by the agent under the
skill's procedure; this tool records the decision and keeps the inbox, ledger,
and audit consistent.

Stdlib only. argparse in, one JSON document out.

  inbox.py show     [--limit N]                     # next pending, best first
  inbox.py adopt    <id> --target PATH --entry TEXT|@file [--supersedes N]
  inbox.py dismiss  <id> --reason TEXT
  inbox.py correct  <id> --corrected TEXT|@file
  inbox.py requeue  <id>
  inbox.py retire   <source> [--reason T] [--comment T] [--promoted T]
  inbox.py unretire <source> --comment TEXT
  inbox.py ledger
"""

import argparse
import glob as _glob
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tiers import (  # noqa: E402
    ARCHIVE_DIR,
    ARCHIVE_LEDGER,
    AUDIT_LOG,
    CANDIDATES_DIR,
    archive_rel,
    digest,
)

# Decisions that change what future agents are told, so they need a human on
# record. Mirrors AI-DLC's human-presence guard and "no override path".
HUMAN_TURN = ".agent/memory-human-turn"
TERMINAL = {"adopted", "dismissed", "corrected"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(repo_dir: Path, record: dict):
    p = repo_dir / AUDIT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("# Memory audit\n\nAppend-only. One JSON line per action.\n\n")
    record.setdefault("ts", now())
    with p.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def require_human_turn(repo_dir: Path, verb: str, force: bool):
    """Refuse a memory-altering decision unless a human turn is on record."""
    if force:
        return
    f = repo_dir / HUMAN_TURN
    if not f.exists():
        raise SystemExit(
            f"refusing '{verb}': no human turn recorded ({HUMAN_TURN}).\n"
            "Memory decisions belong to a person. Record the turn in a supervised "
            "session, or pass --force only on an explicit human instruction."
        )
    age_days = (datetime.now(timezone.utc).timestamp() - f.stat().st_mtime) / 86400
    if age_days > 1:
        raise SystemExit(f"refusing '{verb}': human turn is {age_days:.1f} days old (>1).")


def cand_path(repo_dir: Path, cid: str) -> Path:
    for cand in (repo_dir / CANDIDATES_DIR / f"{cid}.md", Path(cid)):
        if cand.exists():
            return cand
    raise SystemExit(f"no candidate matching '{cid}' in {repo_dir / CANDIDATES_DIR}")


def read_front(text: str):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise SystemExit("candidate has no YAML frontmatter block")
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def set_fields(text: str, updates: dict) -> str:
    m = re.match(r"^(---\n.*?\n---\n)(.*)$", text, re.S)
    if not m:
        raise SystemExit("candidate has no YAML frontmatter block")
    head, body = m.groups()
    lines = head.splitlines()
    for k, v in updates.items():
        val = "null" if v is None else str(v)
        for i, line in enumerate(lines):
            if re.match(rf"^{re.escape(k)}\s*:", line):
                lines[i] = f"{k}: {val}"
                break
        else:
            lines.insert(len(lines) - 1, f"{k}: {val}")
    return "\n".join(lines) + "\n" + body


def read_text_arg(val: str) -> str:
    if val and val.startswith("@"):
        return Path(val[1:]).read_text().strip()
    return (val or "").strip()


_INV_INDEX = re.compile(r"^\|\s*(\d+)\s*\|", re.M)
_INV_HEADER = re.compile(r"^\|\s*#\s*\|", re.M)
_INV_SEP = re.compile(r"^\|[\s:-]*-[|:\s-]*\|\s*$", re.M)


def next_number(text: str) -> int:
    nums = [int(m.group(1)) for m in _INV_INDEX.finditer(text)]
    return (max(nums) + 1) if nums else 1


def insert_index_row(text: str, num: int, entry: str, area: str) -> str:
    """Insert one index row after the last row the table already has.

    Surgical: the only change to the file is the inserted line, so an existing
    index keeps its bytes and its original trailing-newline state. Handles a
    seeded table with no numbered rows yet (anchor is the header separator) and
    a repo with no index table at all (returns the text unchanged; the body
    entry still lands).
    """
    row = f"| {num} | {entry[:110]} | {area} |"
    if row in text:
        return text  # already recorded (re-adoption)
    anchor = None
    for m in _INV_INDEX.finditer(text):
        anchor = m
    if anchor is None:
        for m in _INV_SEP.finditer(text):
            anchor = m
        if anchor is None:
            for m in _INV_HEADER.finditer(text):
                anchor = m
    if anchor is None:
        return text
    end = text.find("\n", anchor.start())
    if end == -1:
        return text.rstrip("\n") + "\n" + row + "\n"
    return text[: end + 1] + row + "\n" + text[end + 1 :]


def append_invariant(target: Path, entry: str, supersedes, fields) -> int:
    """Append a numbered invariant; never renumber or rewrite an existing one.

    The repos' rule is 'append new entries at the end; never renumber', so a
    superseded invariant keeps its number and gains a note rather than being
    edited away.
    """
    text = target.read_text()
    nxt = next_number(text)
    prov = (f"_promoted from `{fields.get('source_file', '?')}` "
            f"§{fields.get('section', '?')} ({fields.get('source_digest', '?')})_")
    text = insert_index_row(text, nxt, entry.replace("|", "/"),
                            fields.get("area", "learned"))
    # Pure append: never re-strip or rewrite the bytes already in the file.
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    text = text + sep + f"**{nxt}. {entry}**\nWHY: {prov}\n"
    if supersedes is not None:
        text += (f"\n> Supersedes invariant {supersedes} — see candidate "
                 f"{fields.get('id')}; original kept for provenance.\n")
    target.write_text(text)
    return nxt


def cmd_show(args):
    repo_dir = Path(args.repo_dir).resolve()
    d = repo_dir / CANDIDATES_DIR
    if not d.exists():
        print(json.dumps({"pending": 0, "message": "inbox is empty"}))
        return 0
    rows = []
    for f in sorted(d.glob("*.md")):
        fields = read_front(f.read_text(errors="replace"))
        if fields.get("status", "pending") != "pending":
            continue
        rows.append({
            "id": fields.get("id"),
            "evidence_class": fields.get("evidence_class"),
            "promotability": fields.get("promotability"),
            "durability_score": int(fields.get("durability_score") or 0),
            "source": fields.get("source_file"),
            "section": fields.get("section"),
            "file": str(f.relative_to(repo_dir)),
        })
    rows.sort(key=lambda r: (-r["durability_score"], r["source"] or ""))
    print(json.dumps({"pending": len(rows), "next": rows[: args.limit]}, indent=2))
    return 0


def cmd_adopt(args):
    repo_dir = Path(args.repo_dir).resolve()
    require_human_turn(repo_dir, "adopt", args.force)
    f = cand_path(repo_dir, args.id)
    text = f.read_text()
    fields = read_front(text)
    if fields.get("status") in TERMINAL and not args.force:
        raise SystemExit(f"{args.id} already {fields['status']}; pass --force to re-decide")
    entry = read_text_arg(args.entry)
    if not entry:
        raise SystemExit("--entry is required (the verbatim or corrected claim)")
    target = repo_dir / args.target
    if not target.exists():
        raise SystemExit(f"T2 target does not exist: {target}. Seed it first.")
    num = append_invariant(target, entry, args.supersedes, fields)
    f.write_text(set_fields(text, {
        "status": "adopted",
        "adopted_to": f"{args.target}#inv-{num}",
        "adopted_at": now(),
    }))
    audit(repo_dir, {"action": "adopt", "id": fields.get("id"), "target": args.target,
                     "invariant": num, "source": fields.get("source_file"),
                     "supersedes": args.supersedes})
    print(json.dumps({"adopted": fields.get("id"), "target": args.target,
                      "invariant": num}, indent=2))
    return 0


def cmd_dismiss(args):
    repo_dir = Path(args.repo_dir).resolve()
    require_human_turn(repo_dir, "dismiss", args.force)
    f = cand_path(repo_dir, args.id)
    text = f.read_text()
    fields = read_front(text)
    reason = read_text_arg(args.reason)
    if not reason:
        raise SystemExit("--reason is required for a dismissal (it is the record)")
    f.write_text(set_fields(text, {"status": "dismissed", "dismissed_reason": reason,
                                   "dismissed_at": now()}))
    audit(repo_dir, {"action": "dismiss", "id": fields.get("id"), "reason": reason,
                     "source": fields.get("source_file")})
    print(json.dumps({"dismissed": fields.get("id")}, indent=2))
    return 0


def cmd_correct(args):
    """Record a correction and leave the original quote intact as the record."""
    repo_dir = Path(args.repo_dir).resolve()
    require_human_turn(repo_dir, "correct", args.force)
    f = cand_path(repo_dir, args.id)
    text = f.read_text()
    fields = read_front(text)
    corrected = read_text_arg(args.corrected)
    if not corrected:
        raise SystemExit("--corrected is required")
    if "## Correction" not in text:
        text = text.rstrip("\n") + f"\n\n## Correction ({now()})\n\n> {corrected}\n"
    f.write_text(set_fields(text, {"status": "corrected",
                                   "corrected_text": corrected[:400],
                                   "corrected_at": now()}))
    audit(repo_dir, {"action": "correct", "id": fields.get("id"),
                     "source": fields.get("source_file")})
    print(json.dumps({"corrected": fields.get("id"),
                      "next": "run adopt --entry with the corrected text"}, indent=2))
    return 0


def cmd_requeue(args):
    repo_dir = Path(args.repo_dir).resolve()
    f = cand_path(repo_dir, args.id)
    fields = read_front(f.read_text())
    f.write_text(set_fields(f.read_text(), {"status": "pending", "adopted_to": None,
                                            "dismissed_reason": None}))
    audit(repo_dir, {"action": "requeue", "id": fields.get("id")})
    print(json.dumps({"requeued": fields.get("id")}))
    return 0


def cmd_retire(args):
    """Retire a T0 report the way an issue is closed: move, keep a ledger row.

    Reason and comment are optional and both recorded. The file is moved, never
    deleted — a report cited by a live invariant must stay retrievable.
    """
    repo_dir = Path(args.repo_dir).resolve()
    src = Path(args.source) if Path(args.source).is_absolute() else repo_dir / args.source
    if not src.exists():
        raise SystemExit(f"no such report: {src}")
    try:
        rel = src.relative_to(repo_dir)
    except ValueError:
        raise SystemExit(f"{src} is outside {repo_dir}")
    dest = repo_dir / archive_rel(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise SystemExit(f"already archived: {dest}")
    body = src.read_text(errors="replace")
    shutil.move(str(src), str(dest))
    ledger = repo_dir / ARCHIVE_LEDGER
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if not ledger.exists():
        ledger.write_text("# Report archive ledger\n\nAppend-only. Retiring keeps the "
                          "file under `.agent/archive/` and records why. Never delete a report "
                          "an invariant cites.\n\n")
    with ledger.open("a") as fh:
        fh.write(f"## {now()} — {rel}\n")
        fh.write(f"- archived_to: `{archive_rel(rel)}`\n")
        fh.write(f"- reason: {args.reason or '(none given)'}\n")
        fh.write(f"- comment: {args.comment or '(none given)'}\n")
        fh.write(f"- digest: `{digest(body)}`\n")
        fh.write(f"- promoted: {args.promoted or 'not stated'}\n\n")
    audit(repo_dir, {"action": "retire", "source": str(rel), "reason": args.reason,
                     "comment": args.comment, "digest": digest(body)})
    print(json.dumps({"retired": str(rel),
                      "archived_to": str(archive_rel(rel))}, indent=2))
    return 0


def cmd_unretire(args):
    repo_dir = Path(args.repo_dir).resolve()
    name = Path(args.source).name
    arch = repo_dir / ARCHIVE_DIR
    # Archive names are flattened as "<parent>--<file>", so match the suffix.
    matches = (sorted(arch.glob(f"*--{name}")) + sorted(arch.glob(name))) \
        if arch.exists() else []
    if not matches:
        raise SystemExit(f"not archived: {name}")
    matches = list(dict.fromkeys(matches))
    if len(matches) > 1:
        raise SystemExit("ambiguous: pass the archived path, matches: "
                         + ", ".join(str(m.relative_to(repo_dir)) for m in matches))
    src = matches[0]
    cfg = json.loads((repo_dir / args.config).read_text())
    # Expand each root glob rather than string-stripping it, so a root with an
    # intermediate wildcard (.ai-dlc/*/reports/*.md) resolves to a real dir.
    dest = None
    for spec in cfg["roots"].values():
        pattern = spec.get("glob") or ""
        parent = os.path.dirname(pattern)
        dirs = _glob.glob(str(repo_dir / parent)) if parent else [str(repo_dir)]
        live = [d for d in dirs if os.path.isdir(d)]
        if live:
            dest = Path(live[0]) / name
            break
    if dest is None:
        raise SystemExit("no existing T0 root matches; restore by hand")
    shutil.move(str(src), str(dest))
    with (repo_dir / ARCHIVE_LEDGER).open("a") as fh:
        fh.write(f"## {now()} — UNRETIRE {dest.relative_to(repo_dir)}\n"
                 f"- comment: {args.comment or '(none given)'}\n\n")
    audit(repo_dir, {"action": "unretire", "source": str(dest.relative_to(repo_dir)),
                     "comment": args.comment})
    print(json.dumps({"unretired": str(dest.relative_to(repo_dir))}, indent=2))
    return 0


def cmd_ledger(args):
    repo_dir = Path(args.repo_dir).resolve()
    p = repo_dir / ARCHIVE_LEDGER
    print(p.read_text() if p.exists() else json.dumps({"ledger": "none yet"}))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--config", default=".agent/memory-config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_show)

    a = sub.add_parser("adopt")
    a.add_argument("id")
    a.add_argument("--target", required=True)
    a.add_argument("--entry", required=True)
    a.add_argument("--supersedes", type=int)
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=cmd_adopt)

    d = sub.add_parser("dismiss")
    d.add_argument("id")
    d.add_argument("--reason", required=True)
    d.add_argument("--force", action="store_true")
    d.set_defaults(func=cmd_dismiss)

    c = sub.add_parser("correct")
    c.add_argument("id")
    c.add_argument("--corrected", required=True)
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_correct)

    r = sub.add_parser("requeue")
    r.add_argument("id")
    r.set_defaults(func=cmd_requeue)

    rt = sub.add_parser("retire")
    rt.add_argument("source")
    rt.add_argument("--reason")
    rt.add_argument("--comment")
    rt.add_argument("--promoted")
    rt.set_defaults(func=cmd_retire)

    u = sub.add_parser("unretire")
    u.add_argument("source")
    u.add_argument("--comment", required=True)
    u.set_defaults(func=cmd_unretire)

    lg = sub.add_parser("ledger")
    lg.set_defaults(func=cmd_ledger)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
