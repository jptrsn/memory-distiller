"""Patch a distill.py so running `surface` also writes a T2 file.

Simulates a distiller bug that reaches for adopted memory, which is the case the
nightly run's commit allowlist exists to catch. Used by tests/nightly.bash, which
installs it into the *clone's* copy of the skill (consolidate runs the scripts
from the clone), so the driver is exercised without a test hook in production code.

  python3 tests/inject_t2_leak.py <path-to-distill.py>
"""
import sys
import pathlib

MARKER = "_injected_t2_leak"
HOOK = '''

def _injected_t2_leak():  # simulated surface() bug
    pathlib.Path(".agent/invariants.md").write_text(
        "## Adopted by a bug\\n\\nnever written by a run\\n")
'''


def patch(path):
    target = pathlib.Path(path)
    src = target.read_text()
    if MARKER in src:
        return False
    needle = "    return args.func(args)"
    if needle not in src:
        raise SystemExit(f"{path}: expected dispatch line not found; "
                         "update tests/inject_t2_leak.py")
    # Define the helper immediately above main() so it exists at call time.
    anchor = "def main(argv=None):"
    if anchor not in src:
        raise SystemExit(f"{path}: expected main() not found; "
                         "update tests/inject_t2_leak.py")
    src = src.replace(anchor, "import pathlib  # injected\n" + HOOK + "\n\n" + anchor, 1)
    src = src.replace(
        needle,
        "    rc = args.func(args)\n"
        "    if getattr(args, 'cmd', None) == 'surface':\n"
        "        _injected_t2_leak()\n"
        "    return rc")
    target.write_text(src)
    return True


if __name__ == "__main__":
    sys.exit(0 if patch(sys.argv[1]) else 0)
