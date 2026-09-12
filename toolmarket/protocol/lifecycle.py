"""The resource lifecycle: a state machine, not a boolean flag.

Two axes, deliberately orthogonal and each traceable to a real system.

Enforcement axis — `ResourceState`
----------------------------------
Taken verbatim from `autoforge.tools.spec.ToolState`. This is the axis that
*autoforge enforces*: whether a resource may be trusted with context budget and
call routing.

    DRAFT ──verify──> PROBATION ──earn──> ACTIVE ──decay──> QUARANTINED
                         ^                    │                   │
                         └──────rehab─────────┴───────────────────┘
                                            └──retire──> RETIRED

Provenance axis — `VersionStatus`
---------------------------------
Taken verbatim from AGP (`autogenesis/version/types.py`: `VersionStatus` has
exactly three members). This is about a *version*, not a *resource*: is this
version the current one, superseded, or shelved?

Both are real. Neither is invented. `draft/probation/active/...` was NOT
specified by any "cap-protocol" (no such project exists) — it is autoforge's,
and this module says so.
"""
from __future__ import annotations

from enum import Enum


class ResourceState(str, Enum):
    """The enforcement lifecycle. Mirrors autoforge ToolState exactly."""

    DRAFT = "draft"            # generated, not yet verified
    PROBATION = "probation"    # verified, on trial — callable but flagged
    ACTIVE = "active"          # earned trust, injected into context by default
    QUARANTINED = "quarantined"  # degraded — hidden by default, still executable
    RETIRED = "retired"        # removed from service


class VersionStatus(str, Enum):
    """AGP's version status. Exactly three members — not nine."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


# The legal edges. Anything not listed here is a LifecycleError — the machine is
# closed, so an illegal transition is a bug, not a policy call.
#
#   draft       -> probation (verified), retired (abandoned before earning trust)
#   probation   -> active    (earned), quarantine (failed in the wild), retired
#   active      -> probation (a version change re-opens the trial), quarantine, retired
#   quarantined -> probation (rehabilitated back onto trial), retired
#   retired     -> (terminal)
LEGAL_TRANSITIONS: dict[ResourceState, frozenset[ResourceState]] = {
    ResourceState.DRAFT: frozenset({
        ResourceState.PROBATION,
        ResourceState.RETIRED,
    }),
    ResourceState.PROBATION: frozenset({
        ResourceState.ACTIVE,
        ResourceState.QUARANTINED,
        ResourceState.RETIRED,
    }),
    ResourceState.ACTIVE: frozenset({
        ResourceState.PROBATION,   # a new version re-opens the trial
        ResourceState.QUARANTINED,
        ResourceState.RETIRED,
    }),
    ResourceState.QUARANTINED: frozenset({
        ResourceState.PROBATION,   # rehab
        ResourceState.RETIRED,
    }),
    ResourceState.RETIRED: frozenset(),
}


class LifecycleError(ValueError):
    """Raised when a transition is asked for that the machine does not permit."""


def can_transition(src: ResourceState, dst: ResourceState) -> bool:
    return dst in LEGAL_TRANSITIONS.get(ResourceState(src), frozenset())


def transition(src: ResourceState, dst: ResourceState) -> ResourceState:
    """Return `dst` if the edge is legal, else raise. Pure; no side effects."""
    src = ResourceState(src)
    dst = ResourceState(dst)
    if src == dst:
        raise LifecycleError(f"no-op transition {src.value} -> {dst.value}")
    if not can_transition(src, dst):
        allowed = sorted(s.value for s in LEGAL_TRANSITIONS[src]) or ["<terminal>"]
        raise LifecycleError(
            f"illegal transition {src.value} -> {dst.value}; "
            f"from {src.value} you may go to: {', '.join(allowed)}"
        )
    return dst
