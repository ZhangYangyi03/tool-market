"""Assert the engine that actually got installed is the commit the pin names.

`pyproject.toml` pins autoforge to a 40-character commit so that a fresh clone
reproduces the README's measurements rather than whatever `main` looks like
today. A pin that silently resolves to something else is worse than no pin: the
reproduction instructions would appear to work while measuring a different
engine.

Three outcomes, and the distinction between them is the point:

  0  the installed engine is the pinned commit
  1  it is a *different* commit -- a red build
  2  it records no VCS origin, so the question cannot be answered

Outcome 2 is normal for a developer, who installs the engine editable from the
sibling checkout (see the README quickstart) and has no recorded origin to
compare. In CI the engine always arrives from GitHub through the pin, so 2 there
means something is genuinely wrong -- which is why the workflow treats any
non-zero exit as a failure, and the message, not the exit code, is what tells a
human which of the two they are looking at.

Run from the repository root: python tools/check_pinned_engine.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

PIN = re.compile(r"autoforge\s*@\s*git\+[^@]+@([0-9a-f]{40})")


def classify(declared: str, direct_url: dict | None) -> tuple[int, str]:
    """Decide the verdict from the pinned commit and the recorded origin.

    `direct_url` is the parsed `direct_url.json` of the installed distribution,
    or None when it records none at all. Split out from the environment probe so
    the decision can be tested without a pip install.
    """
    if direct_url is None:
        return 2, (
            "autoforge records no origin -- it was installed from a local path "
            "and this check cannot tell you which commit you have"
        )

    resolved = (direct_url.get("vcs_info") or {}).get("commit_id")
    if resolved is None:
        # An editable local install is the common case here. Say so plainly
        # rather than reporting a mismatch that has not been established: a
        # check that cries wolf on the normal developer path is a check that
        # gets ignored.
        return 2, (
            f"installed from {direct_url.get('url')} with no VCS origin -- this "
            "check cannot tell you which commit you have"
        )

    if resolved != declared:
        return 1, (
            f"FATAL: the installed engine is {resolved}, but pyproject.toml "
            f"pins {declared}. The reproduction would be measuring a different "
            "engine."
        )

    return 0, f"ok: the installed engine is the pinned commit ({declared})"


def main() -> int:
    pyproject = pathlib.Path("pyproject.toml")
    if not pyproject.exists():
        print("run me from the repository root", file=sys.stderr)
        return 2

    m = PIN.search(pyproject.read_text(encoding="utf-8"))
    if not m:
        print("pyproject.toml has no pinned autoforge commit", file=sys.stderr)
        return 2
    declared = m.group(1)

    # Ask the installed metadata where this engine came from, rather than
    # guessing at a path: site-packages is outside the repo, so a glob from the
    # working directory would silently find nothing.
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        raw = distribution("autoforge").read_text("direct_url.json")
    except PackageNotFoundError:
        print("autoforge is not installed", file=sys.stderr)
        return 2

    direct_url = json.loads(raw) if raw else None
    code, message = classify(declared, direct_url)

    print(f"pinned   {declared}")
    print(f"resolved {(direct_url or {}).get('vcs_info', {}).get('commit_id') if direct_url else None}")
    print(f"source   {(direct_url or {}).get('url')}")
    print()
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
