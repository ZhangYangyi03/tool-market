"""Assert the engine that actually got installed is the commit the pin names.

`pyproject.toml` pins autoforge to a 40-character commit so that a fresh clone
reproduces the README's measurements rather than whatever `main` looks like
today. A pin that silently resolves to something else is worse than no pin: the
reproduction instructions would appear to work while measuring a different
engine.

This is a CI check rather than a test because a local dev checkout installs the
engine editable from the sibling directory (see the README quickstart), which
has no recorded VCS origin to compare against. In CI the engine always arrives
from GitHub through the pin, so the comparison is meaningful there.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution

PIN = re.compile(r"autoforge\s*@\s*git\+[^@]+@([0-9a-f]{40})")


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
    try:
        raw = distribution("autoforge").read_text("direct_url.json")
    except PackageNotFoundError:
        print("autoforge is not installed", file=sys.stderr)
        return 2
    if not raw:
        print(
            "autoforge is installed but records no VCS origin -- it came from a "
            "local path, so this check cannot tell you which commit you have.",
            file=sys.stderr,
        )
        return 2

    direct_url = json.loads(raw)
    resolved = (direct_url.get("vcs_info") or {}).get("commit_id")
    print(f"pinned   {declared}")
    print(f"resolved {resolved}")
    print(f"source   {direct_url.get('url')}")

    if resolved != declared:
        print(
            "\nFATAL: the installed engine is not the pinned commit. The "
            "reproduction below would be measuring a different engine.",
            file=sys.stderr,
        )
        return 1

    print("\nok: the installed engine is the pinned commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
