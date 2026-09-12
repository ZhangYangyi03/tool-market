"""The pin checker's verdicts.

`tools/check_pinned_engine.py` guards the reproduction steps above: if the engine
pip installed is not the commit `pyproject.toml` pins, the README's numbers were
measured against something else.

It has three outcomes and the distinction is the whole point. "Different
engine" is a red build. "No recorded origin" is *not* — that is what a developer
with an editable sibling checkout sees, and reporting it as a mismatch would cry
wolf on the normal path until people learned to ignore the check. These pin that
boundary, because it is the kind of thing that gets flattened by a later
"simplification".
"""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _checker():
    spec = importlib.util.spec_from_file_location(
        "check_pinned_engine", ROOT / "tools" / "check_pinned_engine.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pinned() -> str:
    """Read the pin out of pyproject.toml, via the checker's own parser.

    Deliberately not a literal in this file: a copy of the commit here would be
    a second source of truth for a number that already has one, and it would go
    stale the first time somebody bumped the pin, at which point this test would
    pass while checking the wrong value.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return _checker().PIN.search(text).group(1)


def test_the_pinned_commit_verifies():
    pinned = _pinned()
    code, message = _checker().classify(
        pinned, {"url": "https://github.com/x/y.git", "vcs_info": {"commit_id": pinned}}
    )
    assert code == 0, message


def test_a_different_commit_fails_the_build():
    code, message = _checker().classify(
        _pinned(),
        {"url": "https://github.com/x/y.git", "vcs_info": {"commit_id": "0" * 40}},
    )
    assert code == 1, message
    assert "FATAL" in message


def test_an_editable_local_install_says_cannot_tell_not_wrong_engine():
    """The developer path. A false mismatch here is worse than no check."""
    code, message = _checker().classify(
        _pinned(), {"url": "file:///home/dev/autoforge", "dir_info": {"editable": True}}
    )
    assert code == 2, message
    assert "cannot tell" in message
    assert "FATAL" not in message


def test_no_recorded_origin_at_all_is_also_cannot_tell():
    code, message = _checker().classify(_pinned(), None)
    assert code == 2, message
    assert "cannot tell" in message
