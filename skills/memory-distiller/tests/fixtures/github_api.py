#!/usr/bin/env python3
"""Minimal fake GitHub API for the label state machine.

State lives in a JSON file so mutations persist across invocations within one
test run. Implements only the endpoints threads.py and labels.py use:

  GET   /repos/{o}/{r}/issues?labels=&state=&per_page=&sort=
  GET   /repos/{o}/{r}/issues/{n}
  GET   /repos/{o}/{r}/issues/{n}/comments
  PATCH /repos/{o}/{r}/issues/{n}        (labels: replaces the whole set)
  GET   /repos/{o}/{r}/labels/{name}
  POST  /repos/{o}/{r}/labels

Usage: github_api.py --state FILE [--port N] [--seed SEED.json]
Prints the bound port on stdout, then serves forever.
"""

import argparse
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

ISSUE_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/issues/(\d+)$")
LIST_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/issues$")
COMMENTS_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/issues/(\d+)/comments$")
LABEL_ONE_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/labels/([^/]+)$")
LABELS_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/labels$")
SEARCH_RE = re.compile(r"^/search/issues$")
PR_LIST_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/pulls$")
PR_COMMENTS_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/pulls/(\d+)/comments$")

# Long enough to clear MIN_REPORT_CHARS and rich enough in report markers, so an
# issue with this comment is genuinely selectable by the ingest heuristics.
LONG_REPORT = (
    "## Host device pass 2026-11-23 — cause identified\n\n"
    "Reproduced live on the Pixel 6 in a debug run; the verified environment was\n"
    "the shared test server. Diagnosis only — no fix landed.\n\n"
    "### The confirmed culprit (one site)\n\n"
    "`_open` passes a closure that reads providers through the drawer element's own\n"
    "`BuildContext`, which the pushed route evaluates lazily and repeatedly.\n\n"
    "### Mechanism\n\n"
    "1. Drawer opened from a pushed screen; `popUntil` pops the route owning the\n"
    "   captured context.\n"
    "2. Every later rebuild re-executes the closure on a defunct element.\n\n"
    "### Reproduction (verified twice; deterministic)\n\n"
    "1. Debug build on the device, connected to a server.\n"
    "2. Open drawer on root, tap Queue.\n\n"
    "### Why the suite stayed green\n\n"
    "No test taps Settings from a pushed route, and none rebuilds a pushed route's\n"
    "content after the pop settles, so `takeException()` never reaches the failing\n"
    "frame. Observed across three sessions, all with the same environment.\n\n"
    "### Answers to the triage questions\n\n"
    "1. Severity stays `low`: the assert compiles out in release builds.\n\n"
    "### Plan for completion\n\n1. Read the providers eagerly at tap time.\n"
)


class Handler(BaseHTTPRequestHandler):
    state_file = None

    def log_message(self, *args):
        pass

    def _load(self):
        with open(self.state_file) as fh:
            return json.load(fh)

    def _save(self, st):
        with open(self.state_file, "w") as fh:
            json.dump(st, fh, indent=2)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        st = self._load()

        m = COMMENTS_RE.match(u.path)
        if m:
            return self._send(200, st["comments"].get(m.group(3), []))

        m = ISSUE_RE.match(u.path)
        if m:
            n = m.group(3)
            if n in st["issues"]:
                return self._send(200, st["issues"][n])
            return self._send(404, {"message": "Not Found"})

        m = LIST_RE.match(u.path)
        if m:
            want = q.get("labels", [""])[0]
            state = q.get("state", ["open"])[0]
            out = []
            for issue in st["issues"].values():
                names = {l["name"] for l in issue["labels"]}
                if want and want not in names:
                    continue
                if state != "all" and issue["state"] != state:
                    continue
                out.append(issue)
            return self._send(200, out)

        m = LABEL_ONE_RE.match(u.path)
        if m:
            name = urllib.parse.unquote(m.group(3))
            if name in st["labels"]:
                return self._send(200, st["labels"][name])
            return self._send(404, {"message": "Not Found"})

        if LABELS_RE.match(u.path):
            return self._send(200, list(st["labels"].values()))

        m = PR_COMMENTS_RE.match(u.path)
        if m:
            return self._send(200, st.get("pr_comments", {}).get(m.group(3), []))

        if PR_LIST_RE.match(u.path):
            want = q.get("head", [""])[0]
            out = [pr for pr in st.get("pulls", [])
                   if not want or pr["head"]["label"] == want]
            return self._send(200, out)

        if SEARCH_RE.match(u.path):
            # Emulates the `-label:` exclusion the reconcile scan depends on,
            # which the list endpoint cannot express.
            query = q.get("q", [""])[0]
            want_state = "open" if "state:open" in query else "all"
            out = []
            for issue in st["issues"].values():
                names = {l["name"] for l in issue["labels"]}
                if want_state != "all" and issue["state"] != want_state:
                    continue
                if any(tok.startswith("-label:") and tok[7:] in names
                       for tok in query.split("+")):
                    continue
                out.append(issue)
            return self._send(200, {"total_count": len(out), "items": out})

        return self._send(404, {"message": "Not Found"})

    def do_PATCH(self):
        st = self._load()
        m = ISSUE_RE.match(urllib.parse.urlparse(self.path).path)
        if not m or m.group(3) not in st["issues"]:
            return self._send(404, {"message": "Not Found"})
        n = m.group(3)
        body = self._read()
        if "labels" in body:
            # GitHub replaces the entire set on this call — the behaviour the
            # test exists to pin down.
            st["issues"][n]["labels"] = [{"name": x} for x in body["labels"]]
        self._save(st)
        return self._send(200, st["issues"][n])

    def do_POST(self):
        st = self._load()
        m = LABELS_RE.match(urllib.parse.urlparse(self.path).path)
        if not m:
            return self._send(404, {"message": "Not Found"})
        body = self._read()
        name = body.get("name")
        if not name:
            return self._send(422, {"message": "name required"})
        st["labels"][name] = {"name": name, "color": body.get("color", ""),
                              "description": body.get("description", "")}
        self._save(st)
        return self._send(201, st["labels"][name])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--seed", default=None)
    args = ap.parse_args()

    if args.seed:
        with open(args.seed) as fh:
            st = json.load(fh)
    else:
        st = {"issues": {}, "comments": {}, "labels": {}, "pulls": [],
              "pr_comments": {}}
    st.setdefault("issues", {})
    st.setdefault("comments", {})
    st.setdefault("labels", {})
    for n in st["issues"]:
        st["comments"].setdefault(n, [{
            "id": 900 + int(n), "user": {"login": "jptrsn"},
            "created_at": "2026-11-23T16:54:23Z", "body": LONG_REPORT}])
    with open(args.state, "w") as fh:
        json.dump(st, fh, indent=2)

    Handler.state_file = args.state
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    print(srv.server_address[1], flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
