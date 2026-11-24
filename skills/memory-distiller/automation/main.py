"""Nightly memory consolidation for the software-factory repositories.

Drains the written-up report queue, surfaces T1 candidates, and opens or updates
one PR per repository.

No LLM runs here, deliberately. The work is deterministic, and keeping a model
out of this loop is what guarantees nothing gets promoted: promotion requires
`inbox.py adopt`, which refuses without a human turn. An LLM automation in this
seat could in principle talk its way past that, so it is excluded by design
rather than by instruction.

The scripts it runs live in each repository at
.agents/skills/memory-distiller/scripts/, so a repo stays the source of truth
for its own memory rules and this driver carries no memory logic of its own.

Set MEMORY_DRY_RUN=1 to do everything except push and open a PR.

Needs GITHUB_PERSONAL_ACCESS_TOKEN with Contents: write, Issues: write, and
Pull requests: write.
"""

import json
import os
import pathlib
import posixpath
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# No default: which repos consolidate is a deployment choice, and a public skill
# should not ship anyone's repo list. Set MEMORY_REPOS.
#
# Pin the base with @branch when the repo's factory convention differs from its
# default branch — e.g. work lands on `develop` while the remote HEAD is `main`.
DEFAULT_REPOS = []


def load_repos():
    """Resolve the repo list at run time, not import time.

    A plugin-spec run folds `.automation/config.json` into the environment inside
    main(), so reading the env at import time would miss it entirely.
    """
    return [r for r in os.environ.get("MEMORY_REPOS", "").split(",") if r.strip()] \
        or DEFAULT_REPOS


BRANCH = "openhands/memory-consolidation"
SCRIPTS = ".agents/skills/memory-distiller/scripts"


def render_body(work, counts):
    """PR body from the skill's own renderer, loaded out of the clone.

    The clone carries the scripts, so the body format stays defined in one place
    and a repo pinned to an older skill version gets the body its scripts
    actually support. Fall back to a summary if the clone predates review.py.
    """
    try:
        sys.path.insert(0, os.path.join(work, SCRIPTS))
        import review
        return review.render_body(pathlib.Path(work), counts)
    except Exception as exc:  # noqa: BLE001 - a missing body must not lose the PR
        lines = ["Nightly memory consolidation. Candidates are proposals, not policy.", ""]
        for k, v in counts.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append(f"(candidate table unavailable: {type(exc).__name__}: {exc})")
        return "\n".join(lines)
API = "https://api.github.com"


def apply_plugin_config():
    """Fold the plugin-spec config into the environment.

    `.automation/config.json` is written by the automation service for a
    plugin-spec run. Env wins over config so a dispatch can still be overridden
    ad hoc, and so the tests — which set only env — are unaffected when no
    config file exists.
    """
    path = os.environ.get("AUTOMATION_CONFIG_PATH") or os.path.join(
        os.environ.get("AUTOMATION_PLUGIN_ROOT", "."), ".automation", "config.json")
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return
    for key, value in cfg.items():
        env_key = f"MEMORY_{key.upper()}"
        if value in (None, "", []) or os.environ.get(env_key):
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        elif isinstance(value, bool):
            # Never write "0": the gates below this read the variable with a bare
            # truthiness test, and "0" is a truthy string. Setting
            # review_allow_automation: false would then have enabled it.
            if value:
                os.environ[env_key] = "1"
            continue
        else:
            value = str(value)
        os.environ[env_key] = value

# The nightly run may commit these and nothing else. Candidates are inert until
# adopted, threads/ holds the ingested host reports plus the ledger that keeps
# re-ingest idempotent, and the audit log records what surface() did. T2 files
# (invariants.md, blockers.md, memory/) are adoption targets and are excluded by
# construction — see the guard in consolidate().
ALLOWED_PREFIXES = (".agent/candidates/", ".agent/threads/")
ALLOWED_FILES = (".agent/memory-audit.md",)


def is_allowed(path):
    # Normalise before matching: a bare prefix test lets ".agent/candidates/../
    # invariants.md" through. Git canonicalises staged paths so this is not
    # reachable today, but the guard is the last line between an automated run
    # and adopted memory, so it should not depend on that.
    norm = posixpath.normpath(path)
    if norm != path or norm.startswith("..") or posixpath.isabs(path):
        return False
    return norm.startswith(ALLOWED_PREFIXES) or norm in ALLOWED_FILES


# A step that ran but did not do its job. Kept separate from a crash so the
# reason stays in structured fields rather than only in a free-text error.
def is_degraded(summary):
    """True when a repo's run needs a human look, even though it did not crash.

    A nightly job that reports success while its label writes fail is worse than
    one that fails loudly: the queue silently stops advancing and nobody
    re-tries. So a partial failure is reported as degraded, not as ok.

    A step recorded as `n/a` is not a failure — it did not run because the repo
    has no GitHub side to reach.
    """
    def ran_but_failed(value):
        if value is None:
            return False
        value = str(value)
        return value != "ok" and not value.startswith("n/a")

    if ran_but_failed(summary.get("labels")):
        return True
    if summary.get("threads_error") or summary.get("distill_error"):
        return True
    if summary.get("refused_paths"):
        return True
    pr = summary.get("pr") or ""
    return pr.startswith(("commit failed", "push failed", "create failed"))


def health(summary):
    """Classify one repo's outcome; `ok` is never the default."""
    if summary.get("status") == "skipped":
        return "skipped"
    return "degraded" if is_degraded(summary) else "ok"


DRY_RUN = os.environ.get("MEMORY_DRY_RUN", "") not in ("", "0", "false")


def repo_slug(repo):
    """Directory name for a repo, tolerant of local bare paths and nested groups."""
    tail = repo.rstrip("/").split("/")[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def repo_owner(repo):
    """Owner segment of an owner/name GitHub repo; '' for a local path."""
    parts = repo.rstrip("/").split("/")
    return parts[-2] if len(parts) >= 2 and not repo.startswith("/") else ""


def split_spec(spec):
    """`owner/repo[@branch]` -> (repo, branch or '').

    Pin `@branch` when consolidation must land somewhere other than the remote
    HEAD — a GitFlow repo whose factory base is `develop` while its default
    branch is `main`, for instance. Omit it to follow the remote HEAD."""
    if "@" in spec and not spec.startswith("/"):
        repo, _, branch = spec.rpartition("@")
        return repo, branch
    return spec, ""


def base_branch(repo, wanted, helper):
    """Where consolidation PRs land, resolved from the remote's own branches.

    `@branch` wins outright and is rejected if the remote lacks it; otherwise
    follow the remote HEAD, then fall back to main/master. Validating against
    the remote's real head list means a typo in a spec fails here with the
    available branches named, instead of failing later inside `git clone`.
    """
    g = Git(helper)
    code, out, _ = g("ls-remote", "--heads", clone_url(repo), check=False)
    # Strip exactly the refs/heads/ prefix: rsplit("/") would flatten
    # feature/x to x, and branch names with slashes are common in these repos.
    heads = [ln.split("\t")[1][len("refs/heads/"):]
             for ln in out.splitlines()
             if "\t" in ln and ln.split("\t")[1].startswith("refs/heads/")] \
        if code == 0 else []
    if wanted:
        if heads and wanted not in heads:
            raise RuntimeError(f"{repo} has no branch {wanted!r} (have: {', '.join(heads) or 'none'})")
        return wanted
    code, sym, _ = g("ls-remote", "--symref", "--heads", clone_url(repo), "HEAD",
                     check=False)
    if code == 0:
        for line in sym.splitlines():
            if line.startswith("ref:"):
                ref = line.split()[1]
                short = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
                if not heads or short in heads:
                    return short
    for guess in ("main", "master"):
        if guess in heads:
            return guess
    if heads:
        return heads[0]
    raise RuntimeError(f"cannot resolve a default branch for {repo}")


def clone_url(repo):
    return repo if repo.startswith("/") else f"https://github.com/{repo}.git"


def get_secret(name):
    """Fetch a named secret from the agent server, or the OpenHands API on cloud."""
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    key = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")
    if url:  # local Agent Canvas
        url = f"{url}/api/settings/secrets/{name}"
    else:  # OpenHands Cloud / Enterprise
        api = os.environ["OPENHANDS_CLOUD_API_URL"].rstrip("/")
        url = f"{api}/api/v1/sandboxes/{os.environ['SANDBOX_ID']}/settings/secrets/{name}"
    req = urllib.request.Request(url, headers={"X-Session-API-Key": key})
    with urllib.request.urlopen(req) as r:
        return r.read().decode().strip()


def fire_callback(status="COMPLETED", error=None):
    """Signal run completion. Must run on every exit path, success and error."""
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}"}))
    except Exception as exc:  # noqa: BLE001 - a lost callback must not mask the result
        print(f"Callback error: {exc}")


# In an automation run the token arrives from the agent server's secret store;
# in a local/test run it comes straight from the environment.
TOKEN = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or ""
if not TOKEN:
    try:
        TOKEN = get_secret("GITHUB_PERSONAL_ACCESS_TOKEN")
    except Exception:  # noqa: BLE001 - no secret server locally; main() reports it
        TOKEN = ""


def env():
    # No .pyc files: they regenerate on every run, and a tracked one reappears
    # as modified each night, which would force a pointless commit forever.
    return dict(os.environ, GH_TOKEN=TOKEN, GITHUB_PERSONAL_ACCESS_TOKEN=TOKEN,
                PYTHONDONTWRITEBYTECODE="1")


def run(argv, cwd=None, check=True):
    p = subprocess.run(argv, cwd=cwd, env=env(), capture_output=True, text=True)
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if check and p.returncode != 0:
        shown = " ".join(a if not a.startswith("-c") else a for a in argv[:4])
        raise RuntimeError(f"{shown} failed ({p.returncode}): {err or out}")
    return p.returncode, out, err


def gh(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "memory-distiller-nightly"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "null")


def write_cred_helper(work_root):
    """A helper file, so the token stays in the environment and never reaches
    argv or .git/config where `ps` or a committed config could expose it."""
    os.makedirs(work_root, exist_ok=True)
    path = os.path.join(work_root, "git-cred.sh")
    with open(path, "w") as fh:
        fh.write('#!/bin/sh\necho username=x-access-token\necho "password=$GH_TOKEN"\n')
    os.chmod(path, 0o700)
    return path


class Git:
    def __init__(self, helper, cwd=None):
        self.prefix = ["git", "-c", f"credential.helper={helper}"]
        self.cwd = cwd

    def __call__(self, *args, cwd=None, check=True):
        return run(self.prefix + list(args), cwd=cwd or self.cwd, check=check)


def clone(repo, work, work_root, helper, base):
    """Fresh clone of the base branch; a stale directory from an earlier run
    would carry old state.

    Always the base, never the consolidation branch. Cloning the branch keeps
    surface() from regenerating candidates it already proposed, but it also pins
    the T0 corpus to the commit that branch was cut from: every report merged to
    base afterwards is invisible, and the run reports `unchanged` forever. The
    candidate de-duplication is instead restored per-path in restore_state(), so
    freshness and idempotence are both kept.

    Returns the consolidation branch's ref when it exists, None otherwise.
    """
    dest = os.path.join(work_root, work)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    g = Git(helper)
    url = clone_url(repo)
    g("clone", "--depth", "50", "--branch", base, url, dest, cwd=work_root)
    # --depth implies --single-branch, so origin/<branch> is never created for a
    # branch other than base and an explicit refspec would be needed. FETCH_HEAD
    # is written by the fetch above; pin it to a SHA now rather than trusting the
    # pseudo-ref to survive the rest of the run.
    code, _, _ = g("fetch", "--depth", "50", "origin", BRANCH, cwd=dest, check=False)
    if code != 0:
        return None
    code, sha, _ = g("rev-parse", "--verify", "--quiet", "FETCH_HEAD", cwd=dest,
                     check=False)
    return sha.strip() or None


# State that must survive across runs, restored from the consolidation branch.
# candidates/ is what makes surface() idempotent; threads/ carries the ingest
# ledger, which is the only thing standing between a fresh clone and re-ingesting
# every issue comment and appending duplicate candidates each night; the audit log
# accumulates, so leaving it out would read as a deletion against the branch and
# the nightly commit would erase the record.
STATE_PATHS = (".agent/candidates", ".agent/threads", ".agent/memory-audit.md")


def restore_state(work, ref, g):
    """Overlay the previous run's memory state onto a fresh base clone.

    Confined to STATE_PATHS by construction: the reports themselves, and every T2
    file, stay exactly as base has them.
    """
    if not ref:
        return []
    restored = []
    for path in STATE_PATHS:
        code, _, _ = g("checkout", ref, "--", path, cwd=work, check=False)
        if code == 0:
            restored.append(path)
    return restored


def unstage(g, work, path, baseline="HEAD"):
    """Drop a path from the nightly commit, index and worktree alike.

    Reverting to `baseline` rather than just unstaging: a disallowed file that
    surface() newly created is untracked in baseline, so unstaging alone would
    leave it on disk and `add -A` would pick it up again on the next pass.
    """
    code, _, _ = g("checkout", baseline, "--", path, cwd=work, check=False)
    if code != 0:
        g("rm", "-f", "--cached", "--", path, cwd=work, check=False)
        target = os.path.join(work, path)
        if os.path.isfile(target):
            os.remove(target)


def _consolidate(spec, work_root, helper):
    repo, wanted = split_spec(spec)
    name = repo_slug(repo)
    base = base_branch(repo, wanted, helper)
    g = Git(helper)
    state_ref = clone(repo, name, work_root, helper, base)
    summary = {"repo": repo, "base_branch": base}

    if not os.path.isdir(os.path.join(work_root, name, SCRIPTS)):
        # Not a failure: the repo simply has no memory pipeline yet. Reported so
        # the gap is visible rather than silently skipped.
        summary["status"] = "skipped"
        summary["reason"] = f"{SCRIPTS} not installed"
        return summary
    # Filled in at the end by health(); an early return above is always a
    # deliberate skip, never an unexamined "ok".
    work = os.path.join(work_root, name)
    # Must happen before threads.py and surface() run: both de-duplicate against
    # what is already on disk, and the clone is fresh off base.
    summary["state_restored"] = restore_state(work, state_ref, g)

    # Labels and thread ingest both call the GitHub API, so on a local-path repo
    # (no owner) they are not applicable rather than failed — the same
    # distinction the PR step draws. Without this, a local run would be reported
    # degraded for the absence of a service it was never meant to reach, and the
    # operator would learn to ignore the status.
    local = not repo_owner(repo)

    if local:
        summary["labels"] = "n/a (local repo)"
        summary["threads_ingested"] = 0
        summary["threads"] = "n/a (local repo)"
    elif DRY_RUN:
        # Dry run must mean dry everywhere. `ensure` creates labels and `threads`
        # flips memory-needs-review -> memory-ingested; doing either without the
        # consolidation PR behind it is exactly the orphaned-label drift that
        # `labels.py reconcile` exists to repair, so a preview run would damage
        # the live queue it was only meant to inspect. surface() still runs for
        # real: it writes only into this throwaway clone, which the next run
        # deletes, and its output is what the previewed PR body is built from.
        summary["labels"] = "n/a (dry run)"
        summary["threads"] = "n/a (dry run)"
        code, out, err = run(
            [sys.executable, f"{SCRIPTS}/threads.py", "--repo-dir", ".", "--dry-run"],
            cwd=work, check=False)
        summary["threads_ingested"] = (
            json.loads(out).get("threads_written", 0) if code == 0 else 0)
        if code != 0:
            summary["threads_error"] = err[:160]
    else:
        # ensure is idempotent and argument-light, so a repo whose labels were
        # never created still self-starts on its first run.
        code, _, err = run([sys.executable, f"{SCRIPTS}/labels.py", "--repo-dir", ".",
                            "ensure"], cwd=work, check=False)
        summary["labels"] = "ok" if code == 0 else f"failed: {err[:160]}"

        code, out, err = run(
            [sys.executable, f"{SCRIPTS}/threads.py", "--repo-dir", "."],
            cwd=work, check=False)
        if code == 0:
            summary["threads_ingested"] = json.loads(out).get("threads_written", 0)
        else:
            summary["threads_ingested"] = 0
            summary["threads_error"] = err[:160]

    code, out, err = run([sys.executable, f"{SCRIPTS}/distill.py", "--repo-dir", ".",
                          "surface"], cwd=work, check=False)
    if code == 0:
        d = json.loads(out)
        summary["candidates_written"] = d.get("written", 0)
        summary["host_bound_quarantined"] = d.get("host_bound_quarantined", 0)
        summary["noise_rejected"] = d.get("noise_rejected", 0)
    else:
        summary["candidates_written"] = 0
        summary["distill_error"] = err[:160]

    g("checkout", "-B", BRANCH, cwd=work)
    # Stage only the memory data area. The driver reads the skill scripts under
    # .agents/ but never writes them, so staging that tree would only ever pick
    # up byte-compiled droppings — which, once committed, return as modified on
    # every subsequent run and defeat the unchanged check permanently.
    g("add", "-A", "--", ".agent", cwd=work, check=False)
    # Two baselines, deliberately: the commit is built on fresh base so the PR
    # carries only the memory delta, but "did anything change" is answered against
    # the last nightly commit. Diffing against base would call every restored
    # candidate a new addition forever, since base has never seen them, and the
    # run would never again report `unchanged`. With no prior commit, base is the
    # only baseline available.
    baseline = state_ref or "HEAD"

    def staged_paths():
        # Decide from the index, not `status --porcelain`: porcelain also reports
        # untracked files this job will never commit (regenerated .pyc, editor
        # droppings), which would make every run look dirty.
        _, out, _ = g("diff", "--cached", "--name-only", baseline, "--", ".agent",
                      cwd=work)
        return [ln for ln in out.splitlines() if ln.strip()]

    changed = staged_paths()
    # Boundary enforced, not assumed: a bug in surface() that wrote a T2 file, or
    # a stray edit left in the tree, must not ride out on an automated PR. Drop
    # the offending paths rather than abort the run — one bad path should not
    # cost the other repos their consolidation.
    disallowed = [ln for ln in changed if not is_allowed(ln)]
    if disallowed:
        summary["refused_paths"] = disallowed
        for path in disallowed:
            unstage(g, work, path)
        changed = staged_paths()
    summary["files_changed"] = len(changed)
    if not changed:
        summary["pr"] = ("refused (only disallowed paths staged)" if disallowed
                         else "unchanged")
        return summary
        return summary

    msg = (
        "chore(memory): nightly consolidation\n\n"
        f"threads ingested: {summary.get('threads_ingested', 0)}\n"
        f"candidates written: {summary.get('candidates_written', 0)}\n"
        f"host-bound quarantined: {summary.get('host_bound_quarantined', 0)}\n\n"
        "Generated by the memory-distiller nightly run. Nothing here is binding: "
        "candidates are adopted with `inbox.py adopt` from an interactive "
        "session, which requires a human turn."
    )
    code, _, err = run(["git", "-c", "user.name=openhands",
                        "-c", "user.email=openhands@all-hands.dev",
                        "commit", "-m", msg], cwd=work, check=False)
    if code != 0:
        summary["pr"] = f"commit failed: {err[:160]}"
        return summary

    if DRY_RUN:
        summary["pr"] = "dry-run (not pushed)"
        return summary

    code, _, err = g("push", "--force", "origin", BRANCH, cwd=work, check=False)
    if code != 0:
        summary["pr"] = f"push failed: {err[:200]}"
        return summary

    owner = repo_owner(repo)
    if repo.startswith("/"):
        summary["pr"] = "local repo (PR step skipped)"
        return summary

    code, existing = gh("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{BRANCH}")
    summary["pr"] = None
    if code == 200 and existing:
        # Force-push above already refreshed the open PR; reuse it rather than
        # opening a second one for the same branch.
        summary["pr"] = existing[0]["html_url"]
        summary["pr_updated"] = True
    else:
        body = render_body(work, {
            "threads_ingested": summary.get("threads_ingested", 0),
            "candidates_written": summary.get("candidates_written", 0),
            "host_bound_quarantined": summary.get("host_bound_quarantined", 0),
            "noise_rejected": summary.get("noise_rejected", 0)})
        body += f"\n\n<sub>{len(changed)} files changed. Review here; comment your " \
                "decisions and an OpenHands session applies them.</sub>"
        code, pr = gh("POST", f"/repos/{repo}/pulls", {
            "title": "chore(memory): nightly consolidation",
            "head": BRANCH, "base": base, "body": body})
        summary["pr"] = (pr or {}).get("html_url", f"create failed {code}")
    return summary


def consolidate(spec, work_root, helper):
    """Run one repo and classify the outcome.

    Wrapping instead of setting status inside _consolidate means every return
    path is classified, including any added later: an early return cannot come
    back as an unexamined success.
    """
    summary = _consolidate(spec, work_root, helper)
    summary["status"] = health(summary)
    return summary


def main():
    global DRY_RUN
    apply_plugin_config()
    # Re-read after the config lands: both of these are module constants, so a
    # plugin-spec config.json would otherwise be folded into env too late to see.
    DRY_RUN = os.environ.get("MEMORY_DRY_RUN", "") not in ("", "0", "false")
    repos = load_repos()
    if not TOKEN:
        raise RuntimeError("GITHUB_PERSONAL_ACCESS_TOKEN is not set")
    # Fail fast rather than consolidate nothing and exit 0: a scheduled run with
    # an unset MEMORY_REPOS would otherwise report COMPLETED having touched no
    # repo — the same silent-green failure this driver refuses elsewhere. Lives
    # here, not at module scope, so `import main` for the health classifier and
    # other diagnostics still works without a configured repo list.
    if not repos:
        raise SystemExit(
            "MEMORY_REPOS is empty and no default is configured; refusing to run "
            "a consolidation pass over zero repositories. Set MEMORY_REPOS to a "
            "comma-separated owner/repo list (optionally owner/repo@branch).")
    work_root = os.path.join(os.environ.get("TMPDIR", "/tmp"), "memory-nightly")
    helper = write_cred_helper(work_root)
    results = []
    for spec in repos:
        try:
            # consolidate() clones its own working tree: the clone target depends
            # on the base branch, which is only known once the @branch suffix and
            # the remote's heads are both in hand.
            results.append(consolidate(spec, work_root, helper))
        except Exception as exc:  # noqa: BLE001 - one repo must not abort the rest
            results.append({"repo": spec, "status": "error", "error": str(exc)[:400]})

    # Any repo that crashed or degraded fails the run. Reporting COMPLETED while
    # one repo's queue was stuck is the failure mode that matters here: the
    # operator sees green, stops reading the output, and the un-ingested reports
    # quietly age out. `skipped` is not a failure — it means the skill is simply
    # not installed in that repo.
    broken = [r["repo"] for r in results
              if r.get("status") in ("error", "degraded")]
    counts = {}
    for r in results:
        counts[r.get("status", "ok")] = counts.get(r.get("status", "ok"), 0) + 1
    print(json.dumps({"ran_at": datetime.now(timezone.utc).isoformat(),
                      "dry_run": DRY_RUN, "summary": counts,
                      "broken": broken, "results": results}, indent=2))
    if broken:
        fire_callback("FAILED", f"{len(broken)}/{len(repos)} repos need attention: "
                                f"{', '.join(str(b) for b in broken)}")
        return 1
    fire_callback("COMPLETED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        fire_callback("FAILED", str(exc))
        sys.exit(1)
