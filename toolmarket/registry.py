"""ResourceRegistry — the substrate's front door.

Everything a caller can do to a resource goes through here, and every mutation
leaves both an event and (where relevant) a lineage node. The registry owns the
three pieces of protocol state — records, the event log, the lineage DAG — and
keeps them consistent with each other and with the backing `autoforge` registry.

Enforcement is not optional here. `register` admits a resource at DRAFT; it
becomes callable-as-trusted only by walking the lifecycle. `replace_live` (the
commit step of evolution) re-opens the trial: a new version of an ACTIVE tool
drops back to PROBATION until it has re-earned trust. That single line is the
difference between a registry and a marketplace with a badge.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from toolmarket.protocol.events import EventKind, EventLog
from toolmarket.protocol.lifecycle import (
    LEGAL_TRANSITIONS,
    LifecycleError,
    ResourceState,
    transition,
)
from toolmarket.protocol.lineage import LineageGraph, LineageNode
from toolmarket.protocol.resources import ResourceRecord, ToolContract

try:  # pragma: no cover
    from autoforge.tools.registry import ToolRegistry, ToolResult
    from autoforge.tools.spec import ToolSpec
    from autoforge.forge.validity import FrozenBaseline
    from autoforge.core.llm import MockLLMClient
    from autoforge.forge.verifier import ToolVerifier
    _HAVE = True
except Exception as exc:  # noqa: BLE001
    _HAVE = False
    _ERR = exc


def _require() -> None:
    if not _HAVE:
        raise RuntimeError(
            "toolmarket requires autoforge for its enforcement layer; import "
            f"failed: {_ERR!r}. Install with: pip install -e <path>/autoforge"
        ) from _ERR


class ResourceRegistry:
    """Registers, evolves, invokes and audits protocol resources."""

    def __init__(
        self,
        store: Any = None,
        *,
        tool_registry: Any = None,
        log: Optional[EventLog] = None,
        lineage: Optional[LineageGraph] = None,
    ) -> None:
        from toolmarket.store import make_store

        self.store = store if store is not None else make_store()
        # Adopt autoforge's registry so the *same* enforcement object runs on
        # live calls; we never run a second, weaker copy.
        self.tools = tool_registry
        self.log: EventLog = log or self.store.load_events()
        # Every append becomes durable at the sink, so the operator's evolution
        # events are persisted exactly like the registry's own.
        #
        # *Which* sink is the concurrency contract, and getting it wrong is a 500
        # rather than a slow path. The API and the Celery worker each build their
        # own registry over one shared store, so both hold a `_events` mirror that
        # knows only what that process has seen. Let either of them take `seq` from
        # that mirror and both eventually write event N — the first after the
        # worker's evolution appended anything at all. So a store that can allocate
        # does (`reserve_event`), and only a store that cannot falls back to
        # sealing in the log and handing the finished event over.
        reserve = getattr(self.store, "reserve_event", None)
        self.log.on_reserve = reserve
        self.log.on_append = None if reserve else self.store.append_event
        self.lineage: LineageGraph = lineage or self.store.load_lineage()
        self._records: dict[str, ResourceRecord] = {
            r.id: r for r in self.store.load_resources()
        }
        self._baselines: dict[str, Any] = {}
        # Set by `make_queue` on first request, so every surface built over this
        # registry shares one inline queue. None means "no queue asked for yet".
        self.task_queue: Any = None

    # -- lazy autoforge wiring -------------------------------------------
    def _tools(self) -> Any:
        if self.tools is None:
            _require()
            self.tools = ToolRegistry()
        return self.tools

    # -- read -------------------------------------------------------------
    def get(self, resource_id: str) -> Optional[ResourceRecord]:
        return self._records.get(resource_id)

    def require(self, resource_id: str) -> ResourceRecord:
        rec = self._records.get(resource_id)
        if rec is None:
            raise KeyError(f"unknown resource: {resource_id}")
        return rec

    def list(self, *, type: Optional[str] = None,
             state: Optional[str] = None) -> list[ResourceRecord]:
        out = list(self._records.values())
        if type is not None:
            out = [r for r in out if r.type.value == type]
        if state is not None:
            out = [r for r in out if r.state.value == state]
        return sorted(out, key=lambda r: r.id)

    # -- write ------------------------------------------------------------
    def save(self, rec: ResourceRecord) -> None:
        rec.updated_at = time.time()
        self._records[rec.id] = rec
        self.store.save_resource(rec)
        self._invalidate(rec.id)

    def _invalidate(self, resource_id: str) -> None:
        """Drop the cached view of one resource.

        Called from `save` and nowhere else, because `save` is the only method
        that changes a record — including when the caller is the Celery worker
        in a different process, which is the case this has to work for. With a
        shared Redis, a worker's commit invalidates the API's cache entry even
        though the two share no memory; that property is why invalidation lives
        at the write rather than in the API's route handlers, where it would look
        like it worked in tests and silently not in production.

        Best-effort by design: an unreachable cache must never turn a successful
        write into a failed one. The cost of failing here is a stale read for
        one TTL; the cost of raising is a registry that cannot persist.
        """
        try:
            from toolmarket import metrics as _metrics
            from toolmarket.cache import get_cache, resource_key

            get_cache().delete(resource_key(resource_id))
            _metrics.STORE_OPERATIONS.inc(operation="save_resource")
        except Exception:  # noqa: BLE001 - see docstring
            pass

    def register(self, spec_or_record: Any, *, actor: str = "system",
                 state: Optional[ResourceState] = None) -> ResourceRecord:
        """Admit a tool into the substrate. Always lands at DRAFT (or the
        explicitly requested starting state, which is still a lifecycle node)."""
        _require()
        if isinstance(spec_or_record, ResourceRecord):
            rec = spec_or_record
        else:
            rec = ResourceRecord.from_toolspec(spec_or_record)
        if state is not None:
            rec.state = ResourceState(state)
        elif rec.state == ResourceState.DRAFT:
            rec.state = ResourceState.DRAFT

        if rec.id in self._records:
            raise ValueError(f"resource already registered: {rec.id}")

        self._tools().register(spec_or_record if not isinstance(spec_or_record, ResourceRecord)
                               else rec.to_toolspec(), replace=True)
        self.save(rec)

        node = LineageNode(
            node_id=f"{rec.id}@{rec.version_current.version}",
            resource_id=rec.id,
            version=rec.version_current.version,
            parents=[],
            reason="initial registration",
            mutation="register",
            verification=rec.contract.verification,
            created_at=rec.created_at,
        )
        self.add_lineage_node(node)

        self.log.append(
            EventKind.REGISTER, rec.id,
            data={
                "name": rec.name,
                "state": rec.state.value,
                "version": rec.version_current.version,
                "source": rec.provenance.get("source"),
            },
            actor=actor,
        )
        return rec

    def transition(self, resource_id: str, dst: ResourceState, *,
                   reason: str = "", actor: str = "system") -> ResourceRecord:
        """Move a resource along the *enforcement* lifecycle."""
        rec = self.require(resource_id)
        try:
            transition(rec.state, dst)
        except LifecycleError:
            raise
        src = rec.state
        rec.state = ResourceState(dst)
        self.save(rec)

        kind = {
            ResourceState.QUARANTINED: EventKind.QUARANTINE,
            ResourceState.PROBATION: EventKind.REHAB
            if src == ResourceState.QUARANTINED else EventKind.TRANSITION,
            ResourceState.RETIRED: EventKind.RETIRE,
        }.get(rec.state, EventKind.TRANSITION)
        if src == ResourceState.DRAFT and rec.state == ResourceState.PROBATION:
            kind = EventKind.TRANSITION

        self.log.append(
            kind, rec.id,
            data={"from": src.value, "to": rec.state.value, "reason": reason},
            actor=actor,
        )
        return rec

    def promote(self, resource_id: str, *, actor: str = "system") -> ResourceRecord:
        return self.transition(resource_id, ResourceState.ACTIVE,
                               reason="earned trust", actor=actor)

    def quarantine(self, resource_id: str, *, reason: str = "degraded",
                   actor: str = "system") -> ResourceRecord:
        return self.transition(resource_id, ResourceState.QUARANTINED,
                               reason=reason, actor=actor)

    def rehab(self, resource_id: str, *, reason: str = "rehabilitated",
              actor: str = "system") -> ResourceRecord:
        return self.transition(resource_id, ResourceState.PROBATION,
                               reason=reason, actor=actor)

    def retire(self, resource_id: str, *, reason: str = "withdrawn",
               actor: str = "system") -> ResourceRecord:
        return self.transition(resource_id, ResourceState.RETIRED,
                               reason=reason, actor=actor)

    # -- evolution commit hook -------------------------------------------
    def replace_live(
        self,
        resource_id: str,
        contract: ToolContract,
        *,
        note: str = "",
        mutation: str = "mutate",
        parent_node: Optional[str] = None,
        verification: Optional[dict[str, Any]] = None,
    ) -> ResourceRecord:
        """Advance the live version to `contract` and re-open the trial.

        This is the only sanctioned way a resource's *body* changes. It always:
          * archives the superseded version (provenance axis),
          * advances a new ACTIVE version,
          * draws a lineage edge from the parent,
          * pushes an ACTIVE tool back to PROBATION (enforcement axis) — a new
            version has not yet earned trust, no matter how trusted its parent.
        """
        rec = self.require(resource_id)
        old_state = rec.state
        rec.contract = contract
        if verification is not None:
            rec.contract.verification = verification
        new_version = rec.advance_version(note=note)
        rec.contract.verification = rec.contract.verification or {}
        self.save(rec)

        # Enforcement: a version change re-opens the trial.
        if old_state in (ResourceState.ACTIVE, ResourceState.PROBATION):
            rec.state = ResourceState.PROBATION
            self.save(rec)

        node = LineageNode(
            node_id=f"{rec.id}@{new_version.version}",
            resource_id=rec.id,
            version=new_version.version,
            parents=[parent_node] if parent_node else [],
            reason=note,
            mutation=mutation,
            verification=rec.contract.verification,
            created_at=time.time(),
        )
        self.add_lineage_node(node)
        return rec

    # -- autoforge bridge -------------------------------------------------
    def to_spec(self, rec: ResourceRecord, fn: Any = None) -> Any:
        """Rehydrate the live autoforge spec for enforcement to run against."""
        _require()
        spec = rec.to_toolspec(fn=fn)
        # If the backing registry already holds this tool, prefer its fn so the
        # callable is never lost across a rehydrate.
        existing = self._tools().get(rec.name) if self.tools is not None else None
        if existing is not None and fn is None:
            spec.fn = existing.fn
            spec.runner = getattr(existing, "runner", None)
        return spec

    def baseline_for(self, resource_id: str) -> Any:
        """The frozen baseline a candidate must not regress against."""
        _require()
        if resource_id in self._baselines:
            return self._baselines[resource_id]
        rec = self.require(resource_id)
        spec = self.to_spec(rec)
        baseline = FrozenBaseline.capture(spec)
        self._baselines[resource_id] = baseline
        return baseline

    def enforce_baseline(self, resource_id: str, spec: Any) -> Any:
        """Grow the frozen baseline after a version is accepted (the ratchet)."""
        _require()
        base = self.baseline_for(resource_id)
        grown = base.extended_with(spec)
        self._baselines[resource_id] = grown
        return grown

    def offline_verifier(self) -> Any:
        """A deterministic, network-free verifier: execution + robustness only.

        The adversarial and trigger checks need a real model; offline we run the
        checks that are reproducible so assessment is a function of the code, not
        of the weather.
        """
        _require()
        return ToolVerifier(
            MockLLMClient(),
            run_adversarial_check=False,
            run_trigger_check=False,
            run_negative_check=False,
        )

    # -- invocation -------------------------------------------------------
    def invoke(self, resource_id: str, arguments: dict[str, Any], *,
               force: bool = False, actor: str = "system") -> Any:
        """Call a resource through the backing registry, and log it."""
        rec = self.require(resource_id)
        tool_reg = self._tools()
        # Make sure the live code is what the registry holds.
        tool_reg.register(self.to_spec(rec), replace=True)
        result = tool_reg.call(rec.name, arguments, force=force)
        rec.ledger = self._spec_ledger(rec)
        self.save(rec)
        self.log.append(
            EventKind.INVOKE, rec.id,
            data={"arguments": arguments,
                  "ok": bool(getattr(result, "ok", True)),
                  "error": getattr(result, "error", None)},
            actor=actor,
        )
        return result

    def _spec_ledger(self, rec: ResourceRecord) -> dict[str, Any]:
        spec = self._tools().get(rec.name)
        return spec.stats.to_dict() if spec is not None and hasattr(spec, "stats") else rec.ledger

    # -- views ------------------------------------------------------------
    def event_log(self, resource_id: Optional[str] = None) -> list[dict[str, Any]]:
        events = (self.log.for_resource(resource_id) if resource_id
                  else list(self.log))
        return [e.to_dict() for e in events]

    # -- lineage ----------------------------------------------------------

    def add_lineage_node(self, node: Any) -> None:
        """Record a lineage node in memory *and* durably.

        Single entry point so no caller can add an in-memory node and forget
        to persist it — which is exactly how an evolution's provenance would
        quietly vanish on restart.
        """
        self.lineage.add(node)
        self.store.save_lineage_node(node)

    def lineage_view(self, resource_id: str) -> dict[str, Any]:
        nodes = self.lineage.of_resource(resource_id)
        return {
            "resource_id": resource_id,
            "nodes": [n.to_dict() for n in nodes],
            "roots": [n.node_id for n in nodes if not n.parents],
            "current": (f"{resource_id}@{self._records[resource_id].version_current.version}"
                        if resource_id in self._records else None),
        }
