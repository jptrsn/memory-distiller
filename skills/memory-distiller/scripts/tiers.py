"""Shared tier map, report classification, and digest helpers.

Stdlib only. Imported by distill.py and inbox.py so both agree on where the
tiers live and on how a report retires.

Tiers (borrowed from AI-DLC's learnings ritual, mapped onto the conventions
these repos already use):

  T0  raw per-unit / per-issue evidence   (append-only, retirable, never edited)
  T1  proposed-but-not-binding            (the inbox; one file per candidate)
  T2  binding, curated, injected           (human-adopted only, never by a run)
  AX  retired T0 + the archive ledger
"""

import hashlib
import re
from pathlib import Path

CANDIDATES_DIR = ".agent/candidates"
ARCHIVE_DIR = ".agent/archive"
ARCHIVE_LEDGER = ".agent/reports/archive-log.md"
AUDIT_LOG = ".agent/memory-audit.md"
THREADS_DIR = ".agent/threads"

# Section name -> (evidence class, candidate?).
#
# Only the EPISTEMIC sections are candidate sources. OBSERVED is deliberately
# NOT a candidate: it is per-test evidence ("test_health_returns_ok... -> 200"),
# which these repos forbid from memory ("Do NOT add issue-specific information"),
# and it is attached to candidates as corroboration instead. Empirically, of the
# 31 live invariants in one repo, exactly one traces to an OBSERVED line while the
# largest group traces to build-step narrative and review-round dispositions.
SECTIONS = {
    "OBSERVED": ("observed", False),
    "NOT VERIFIED": ("unverified", True),
    "DEVIATIONS": ("deviation", True),
    "ASSUMPTIONS": ("assumption", True),
    "UNKNOWN": ("unknown", True),
    "BLOCKERS": ("blocker", True),
    "CORRECTIONS": ("correction", True),
    "RESOLVED FINDINGS": ("finding", True),
    "FINDINGS AND ADJUDICATION": ("finding", True),
    "GO/NO-GO CHECK": ("go-no-go", False),
    "GATES": ("gate", False),
    "SUMMARY": ("summary", False),
    "SUPERVISED PASS": ("supervised", True),
    "STEP": ("narrative", True),
}

# Host-thread RCA vocabulary. A written-up host/device report does not use the
# factory report headings; it uses an RCA shape, and its highest-value content
# has no counterpart in the SECTIONS map. These are matched as a phrase anywhere
# in the heading (the corpus writes "### The confirmed culprit (one site)"),
# unlike the strict-prefix SECTIONS headings.
#
# "Why the suite stayed green" is deliberately candidate-bearing: it is the
# durable lesson of a host pass — the gap that let a shipped defect through —
# and it generalises into a testing rule rather than an issue detail.
RCA_SECTIONS = [
    (r"confirmed culprit|root cause|single culprit|the culprit", "root-cause", True),
    (r"mechanism", "root-cause", True),
    (r"why the suite stayed green|why (?:the|our) suite|test blind spot|"
     r"why it (?:was|wasn.t|stayed) (?:green|caught)", "test-blind-spot", True),
    (r"answers? to the triage|triage questions", "triage-answer", True),
    # Evidence containers: durable context, not memory. Repro steps and the
    # captured stack are issue-specific by nature, and a "plan for completion"
    # is forward-looking work, which is not what T2 holds.
    (r"^reproduction\b|repro steps|steps to reproduce", "reproduction", False),
    (r"full stack|stack trace|traceback", "stack", False),
    (r"plan for completion|next steps|plan of record|^plan\b", "plan", False),
    (r"^evidence\b|artifacts?\b", "evidence", False),
]
_RCA = [(re.compile(pat, re.I), cls, ok) for pat, cls, ok in RCA_SECTIONS]

# Any ATX heading closes the current section; only known ones open a new one.
# Without this, an unlisted heading (## Commits) is absorbed as body text.
_ANY_HEADING = re.compile(r"^#{1,6}\s+\S")

# Heading levels are inconsistent across the corpus (## OBSERVED vs ###
# OBSERVED, and trailing em-dash prose), so match on the leading token of any
# ATX heading rather than an exact line.
_HEADINGS = {k: re.compile(rf"^#{{1,6}}\s+{re.escape(k)}\b", re.I) for k in SECTIONS}
_REVIEW_ROUND = re.compile(r"^#{1,6}\s+Review round\b", re.I)
# `FINDING <id>: fixed (SHA) | rebutted (evidence)` — the review-round verdict
# line both repos converged on.
_FINDING = re.compile(r"FINDING\s+(\S+)\s*:\s*(fixed|rebutted)\b[^\n]*", re.I)


def archive_rel(rel: Path) -> Path:
    """Where a retired T0 file lands: flat, collision-free, greppable.

    Reports are named by issue number, so two nights that both covered issue
    #40 would collide in a flat archive; flattening the source parent into the
    filename keeps it one directory deep and unique. The archive sits outside
    every T0 glob, so a retired file can never be re-surfaced.
    """
    parent = re.sub(r"[^A-Za-z0-9._-]+", "-", str(rel.parent)).strip("-.")
    stem = f"{parent}--{rel.name}" if parent and parent != "." else rel.name
    return Path(ARCHIVE_DIR) / stem


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def split_sections(text: str):
    """Return a list of (section_key, class, is_candidate, body, start_line).

    Any ATX heading terminates the open section, so an unrecognised heading
    (e.g. `## Commits`) is never absorbed into a candidate-bearing body.
    Unclassified text before the first heading becomes a `preamble` section so
    nothing is silently dropped.
    """
    lines = text.splitlines()
    marks = []
    for i, line in enumerate(lines):
        if not _ANY_HEADING.match(line):
            continue
        for key, rx in _HEADINGS.items():
            if rx.match(line):
                marks.append((i, key))
                break
        else:
            heading = line.lstrip("#").strip()
            # RCA/host-report headings are matched by phrase, not prefix.
            for rx, cls, _ok in _RCA:
                if rx.search(heading):
                    marks.append((i, f"{cls}::{heading[:60]}"))
                    break
            else:
                # Unrecognised heading: closes the open section, opens nothing.
                marks.append((i, "REVIEW ROUND" if _REVIEW_ROUND.match(line) else None))
    if not marks:
        return [("preamble", "unclassified", False, text, 1)]
    out = []
    if marks[0][0] > 0:
        pre = "\n".join(lines[: marks[0][0]]).strip()
        if pre:
            out.append(("preamble", "unclassified", False, pre, 1))
    for idx, (start, key) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        body = "\n".join(lines[start + 1 : end]).strip()
        if key is None:
            continue
        if key == "REVIEW ROUND":
            key, cls, ok = "REVIEW ROUND", "review-round", True
        elif "::" in key:  # RCA heading: "<class>::<heading>"
            cls, key = key.split("::", 1)
            ok = dict((c, o) for _p, c, o in RCA_SECTIONS)[cls]
        else:
            cls, ok = SECTIONS[key]
        out.append((key, cls, ok, body, start + 2))
    return out


def corroboration(text: str):
    """Evidence that backs a claim from the same report: OBSERVED + GATES.

    Returned as short strings to attach to a candidate's Provenance, so the
    reviewer sees the proof without opening the report.
    """
    bits = []
    for key, cls, _ok, body, _line in split_sections(text):
        if cls in ("observed", "supervised") or key == "GATES":
            for item in bullets(body)[:8]:
                bits.append(f"{key}: {item[:220]}")
    return bits[:12]


_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def bullets(body: str):
    """Split a section body into list items, folding continuation lines.

    Text that is not a list becomes a single item, so a prose section still
    yields something reviewable rather than nothing.
    """
    items, prose = [], []
    for line in body.splitlines():
        if _BULLET.match(line):
            if prose:
                items.append(" ".join(prose).strip())
                prose = []
            items.append(_BULLET.sub("", line).strip())
        elif line.strip():
            if items:
                items[-1] = (items[-1] + " " + line.strip()).strip()
            else:
                prose.append(line.strip())
    if prose:
        items.append(" ".join(prose).strip())
    return [i for i in (x.strip() for x in items) if i]


def findings_in(body: str):
    return [(m.group(1), m.group(2).lower(), m.group(0)) for m in _FINDING.finditer(body)]

# Host-only surfaces, and the deferral language that makes them a problem.
#
# Naming a device is not the issue — a test-backed claim may mention `pixel` or
# an AVD as incidental context. The thing this design refuses is a candidate
# whose settlement is *deferred* to a host run, which would make a human the
# dependency that closes it. Your reports state that deferral in plain words
# ("deferred to the morning", "could not run", "manual (human) per the
# device policy"), so the gate requires a marker AND deferral language.
HOST_MARKERS = [
    "pixel", "real hardware", "on-device", "on device", "physical device",
    "avd", "adb ", "emulator", "uiautomator", "idf.py", "esp-idf", "esp32",
    "dev board", "docker build", "docker compose up", "container startup",
    "real home assistant", "real instance", "live instance", "supervisor proxy",
    "bluetooth", "airplay",
]
DEFERRAL_MARKERS = [
    "manual (human)", "manual (device)", "deferred", "not run", "was not run",
    "not exercised", "could not run", "cannot run", "skipped", "not verified",
    "unverified on", "requires a device", "needs a device", "no device",
    "device policy", "device session", "host verification", "pending a run",
]
# Classes that record a human-witnessed OUTCOME — exempt, the run happened.
WITNESSED_CLASSES = {"correction", "supervised", "blocker"}


def host_bound(text: str, extra_markers=None):
    """Return the markers that make this item host-deferred, or [].

    Requires both a host surface and deferral language, so a settled claim that
    merely mentions a device stays a candidate.
    """
    t = text.lower()
    hits = sorted({m for m in (HOST_MARKERS + list(extra_markers or [])) if m in t})
    if not hits:
        return []
    if any(d in t for d in DEFERRAL_MARKERS):
        return hits
    return []
