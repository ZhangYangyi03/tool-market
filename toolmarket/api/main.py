"""FastAPI surface over the resource substrate.

Routes are thin: each one calls exactly one registry/operator method. The API is
not where the semantics live, and it is not allowed to become so — a route that
decided a resource's state by itself would be a second, unaudited state machine.

    GET  /health
    GET  /ready                       deep check: the store and the cache answer
    GET  /stats
    GET  /metrics                     Prometheus exposition
    GET  /events                      (optionally ?resource_id=)
    GET  /resources                   (?type= &state=)
    POST /resources                   register a tool
    GET  /resources/{id}              served from cache when warm
    POST /resources/{id}/transition   the enforcement lifecycle
    POST /resources/{id}/invoke
    GET  /resources/{id}/lineage
    GET  /resources/{id}/events
    GET  /resources/{id}/trust             what the ledger earns it, and why
    POST /resources/{id}/evolve       the SEPL closed loop (propose→assess→commit)
    POST /resources/{id}/evolve/async same, but returns a task id immediately
    GET  /tasks/{task_id}             poll an async evolution

Two things the routes do *not* do, deliberately: they never advance a state, and
they never write an event. Both belong to the registry.

`/health` versus `/ready` is a distinction with a purpose. `/health` answers "is
this process alive" and must never touch the database — a liveness probe that
depends on a dependency restarts the API every time the database hiccups, which
turns one outage into two. `/ready` answers "can this process serve traffic" and
checks the store and the cache, because that is what a load balancer needs to
know before it sends a request here.
"""
from __future__ import annotations

import os
from typing import Any, Optional

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
    _HAVE_FASTAPI = True
except Exception:  # noqa: BLE001 - API extras are optional
    _HAVE_FASTAPI = False

from toolmarket import metrics as _metrics
from toolmarket.cache import get_cache, resource_key
from toolmarket.protocol.lifecycle import ResourceState, VersionStatus, parse_state
from toolmarket.protocol.resources import (
    ResourceRecord,
    ToolContract,
    compile_tool_fn,
)
from toolmarket.registry import ResourceRegistry
from toolmarket.tasks import TaskState, TaskStore, make_queue

DEFAULT_CACHE_TTL = 30.0


# -- request models ---------------------------------------------------------
if _HAVE_FASTAPI:

    class RegisterRequest(BaseModel):
        name: str
        description: str = ""
        parameters: dict[str, Any] = Field(default_factory=dict)
        code: str = ""
        source: str = "human"
        generator: str = ""
        invariances: list[str] = Field(default_factory=list)
        effect_signature: str = ""
        tags: list[str] = Field(default_factory=list)
        enable_evolving: bool = True
        permission_mode: str = "workspace_write"

    class TransitionRequest(BaseModel):
        to: str
        reason: str = ""

    class InvokeRequest(BaseModel):
        arguments: dict[str, Any] = Field(default_factory=dict)
        force: bool = False

    class EvolveRequest(BaseModel):
        goal: str
        commit: bool = True
        proposer: str = "stub"


def _record_view(rec: ResourceRecord) -> dict[str, Any]:
    d = rec.to_dict()
    d["capability_schema"] = rec.as_capability_schema()
    return d


def create_app(registry: Optional[ResourceRegistry] = None) -> Any:
    """Build the FastAPI app over a registry (a fresh one by default)."""
    if not _HAVE_FASTAPI:
        raise RuntimeError(
            "FastAPI is required for the API. Install the extra: "
            "pip install 'toolmarket[api]'"
        )

    reg = registry or ResourceRegistry()
    app = FastAPI(
        title="toolmarket",
        version="0.1.0",
        description=(
            "A protocol-registered resource platform for evolvable agent tools. "
            "The enforced implementation of AGP's resource substrate, on autoforge."
        ),
    )
    app.state.registry = reg
    # Inline unless `TASK_QUEUE=celery`. Built once per app so the task id a
    # client is handed can be polled against the same store it was written to.
    app.state.queue = make_queue(reg)
    app.state.cache_ttl = float(os.environ.get("CACHE_TTL", DEFAULT_CACHE_TTL) or 0)

    def _op(reg_: ResourceRegistry):
        from toolmarket.protocol.sepl import EvolutionOperator
        return EvolutionOperator(reg_)

    def _gather_dynamic() -> list[str]:
        """State gauges, read at scrape time from the registry itself.

        These are gathered rather than incremented because every one of them is a
        *property* of current state. A `resource.state == ACTIVE` counter that
        each transition had to remember to increment is a counter that will
        disagree with `GET /resources` the first time a path is added, and the
        disagreement will be found by whoever is on call, at 3am, staring at a
        dashboard that says the opposite of the API.
        """
        by_state: dict[str, int] = {}
        for r in reg.list():
            by_state[r.state.value] = by_state.get(r.state.value, 0) + 1
        lines = [
            "# HELP toolmarket_resources Information about registered resources, "
            "by lifecycle state.",
            "# TYPE toolmarket_resources gauge",
        ]
        for state in sorted(by_state):
            lines.append(f'toolmarket_resources{{state="{state}"}} '
                         f"{by_state[state]}")
        lines += [
            "# HELP toolmarket_events_total Events in the append-only log.",
            "# TYPE toolmarket_events_total gauge",
            f"toolmarket_events_total {len(reg.log)}",
            "# HELP toolmarket_lineage_nodes Lineage nodes recorded.",
            "# TYPE toolmarket_lineage_nodes gauge",
            f"toolmarket_lineage_nodes {len(reg.lineage)}",
            "# HELP toolmarket_chain_intact 1 if the event hash chain verifies, "
            "0 if it has been tampered with.",
            "# TYPE toolmarket_chain_intact gauge",
            f"toolmarket_chain_intact {1 if reg.log.verify_chain() else 0}",
        ]
        try:
            counts = TaskStore(get_cache()).by_state()
            lines += [
                "# HELP toolmarket_async_tasks Async evolution tasks, by state, "
                "as reported by the task store.",
                "# TYPE toolmarket_async_tasks gauge",
            ]
            for state in sorted(counts):
                lines.append(f'toolmarket_async_tasks{{state="{state}"}} '
                             f"{counts[state]}")
        except Exception:  # noqa: BLE001 - a cache without a task index
            pass

        # Dependency reachability, sampled at scrape time.
        #
        # This is the only gatherer that makes a network call, and it is here
        # because the alternative is worse in a way that is easy to miss: an
        # alert on `probe_success` requires a blackbox exporter nobody deployed,
        # so the rule reads as coverage while never being able to fire. A metric
        # that cannot be wrong is not a guard, it is decoration.
        #
        # It costs one round trip per scrape. At a 15s interval against a database
        # on the same bridge network that is free, and it is the same cost the
        # orchestrator's own readiness probe pays.
        lines += [
            "# HELP toolmarket_dependency_up 1 if a dependency answered a "
            "trivial call at scrape time, 0 otherwise.",
            "# TYPE toolmarket_dependency_up gauge",
        ]
        for component, probe in (("store", reg.store), ("cache", get_cache())):
            try:
                up = 1 if probe.ping() else 0
            except Exception:  # noqa: BLE001
                up = 0
            lines.append(f'toolmarket_dependency_up{{component="{component}"}} {up}')
        return lines

    _metrics.METRICS.add_gatherer(_gather_dynamic, key="toolmarket.registry")
    _metrics.BUILD_INFO.set(
        1.0,
        version=app.version,
        store_backend=str(getattr(reg.store, "backend", "sqlite")),
        cache_backend=str(getattr(get_cache(), "backend", "none")),
    )

    # -- meta -------------------------------------------------------------
    @app.get("/")
    def index() -> dict[str, Any]:
        """An index, so a deployment has a landing page that is not a 404.

        Served as JSON rather than HTML because this app is the *API*: the
        console is `toolmarket.web.build_app`, and giving the API a hand-written
        HTML page would mean two places to change when a route is added. FastAPI's
        own `/docs` is the interactive view.
        """
        return {
            "name": app.title,
            "version": app.version,
            "docs": "/docs",
            "endpoints": sorted(
                f"{sorted(getattr(r, 'methods', []) or [])[0]} "
                f"{r.path}"
                for r in app.routes
                if getattr(r, "methods", None)
            ),
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness: this process is up and its in-memory state is consistent.

        No store call, no cache call — see the module docstring. A probe that
        fails when a dependency is down gets you a restart loop, not a diagnosis.
        """
        return {"ok": True, "resources": len(reg.list()),
                "events": len(reg.log), "chain_ok": reg.log.verify_chain()}

    @app.get("/ready")
    def ready() -> dict[str, Any]:
        """Readiness: every dependency this process needs answers a trivial call.

        Returns 503 with the failing component named, because "not ready" without
        a reason is the least useful thing an orchestrator can be told.
        """
        from fastapi.responses import JSONResponse

        store_ok = False
        try:
            store_ok = bool(reg.store.ping())
        except Exception:  # noqa: BLE001
            store_ok = False
        cache_ok = False
        try:
            cache_ok = bool(get_cache().ping())
        except Exception:  # noqa: BLE001
            cache_ok = False
        payload = {
            "ready": store_ok and cache_ok,
            # `"unknown"`, never a specific backend, for a component that has not
            # declared one. The previous defaults were `"sqlite"` and `"none"` —
            # real backend names, and wrong ones whenever the object in hand did
            # not declare an attribute of its own. A Postgres stack reported
            # `sqlite`, and the single call whose whole job is to say which store
            # you actually connected to was the call that could not.
            #
            # Both stores now declare `backend`, so this branch is a guard rather
            # than a routine path. It stays a guard that cannot lie: naming a
            # backend the code did not confirm is worse than admitting ignorance,
            # because it is indistinguishable from a correct answer.
            "store": {"ok": store_ok,
                      "backend": getattr(reg.store, "backend", "unknown")},
            "cache": {"ok": cache_ok,
                      "backend": getattr(get_cache(), "backend", "unknown")},
            "queue": {"backend": getattr(app.state.queue, "backend", "inline")},
        }
        status = 200 if payload["ready"] else 503
        return JSONResponse(payload, status_code=status)

    @app.get("/metrics")
    def metrics_endpoint() -> Any:
        """Prometheus exposition. Content type carries the format version: a
        scraper that does not see `version=0.0.4` will guess, and guessing wrong
        here means silently dropped samples rather than an error."""
        from fastapi.responses import Response

        return Response(content=_metrics.render(),
                        media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for r in reg.list():
            by_state[r.state.value] = by_state.get(r.state.value, 0) + 1
        return {"store": reg.store.stats(), "by_state": by_state,
                "events": len(reg.log), "lineage_nodes": len(reg.lineage)}

    @app.get("/events")
    def events(resource_id: Optional[str] = None,
               limit: int = 100) -> dict[str, Any]:
        rows = reg.event_log(resource_id)
        return {"count": len(rows), "events": rows[-limit:]}

    # -- resources --------------------------------------------------------
    @app.get("/resources")
    def list_resources(type: Optional[str] = None,
                       state: Optional[str] = None) -> dict[str, Any]:
        rows = reg.list(type=type, state=state)
        return {"count": len(rows), "resources": [_record_view(r) for r in rows]}

    @app.post("/resources", status_code=201)
    def register(req: "RegisterRequest") -> dict[str, Any]:
        # Build an autoforge ToolSpec so enforcement has something real to run.
        try:
            from autoforge.tools.spec import ToolSpec
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"autoforge unavailable: {exc}") from exc

        fn = _compile_fn(req.code, req.name)
        spec = ToolSpec(
            name=req.name,
            description=req.description,
            parameters=req.parameters,
            fn=fn,
            code=req.code,
            source=req.source,
            generator=req.generator,
            invariances=list(req.invariances),
            effect_signature=req.effect_signature,
            tags=list(req.tags),
        )
        try:
            rec = reg.register(spec)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        rec.enable_evolving = req.enable_evolving
        rec.permission_mode = req.permission_mode
        reg.save(rec)
        return _record_view(rec)

    @app.get("/resources/{resource_id:path}/lineage")
    def lineage(resource_id: str) -> dict[str, Any]:
        if reg.get(resource_id) is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        return reg.lineage_view(resource_id)

    @app.get("/resources/{resource_id:path}/events")
    def resource_events(resource_id: str, limit: int = 200) -> dict[str, Any]:
        if reg.get(resource_id) is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        rows = reg.event_log(resource_id)
        return {"resource_id": resource_id, "count": len(rows),
                "events": rows[-limit:]}

    @app.get("/resources/{resource_id:path}/trust")
    def resource_trust(resource_id: str) -> dict[str, Any]:
        """Should this be ACTIVE? Answered with the evidence, not a verdict.

        GET, and read-only: the policy is applied on `invoke` (the moment the
        ledger moves) and can be swept by `tool-market trust --apply`, but a read
        never moves a resource. The route exists so the answer is *inspectable* --
        an operator asking why a well-behaved tool is still on probation gets the
        failing precondition and the thresholds it was measured against, rather
        than the empty `reason` string the manual transitions used to leave.
        """
        if reg.get(resource_id) is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        return reg.trust_view(resource_id)

    @app.get("/resources/{resource_id:path}")
    def get_resource(resource_id: str, fresh: bool = False) -> dict[str, Any]:
        """One resource, from the cache when it is warm.

        `?fresh=true` bypasses the cache. It exists because the alternative — a
        client that cannot get a value it knows is stale — turns a cache into a
        correctness bug for the one user who is looking at the consequence of a
        write. A cache needs an escape hatch or it needs to be small enough not
        to need one; this one is small, and it has the hatch anyway.

        Only 200s are cached. A 404 is not: "unknown resource" is the answer that
        changes the moment somebody registers the tool, and caching it means the
        registry's own `register` response is contradicted by a stale miss.
        """
        cache = get_cache()
        key = resource_key(resource_id)
        ttl = app.state.cache_ttl
        if not fresh and ttl > 0:
            hit = cache.get(key)
            if hit is not None:
                _metrics.CACHE_LOOKUPS.inc(result="hit")
                return hit
            _metrics.CACHE_LOOKUPS.inc(result="miss")
        rec = reg.get(resource_id)
        if rec is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        view = _record_view(rec)
        if ttl > 0:
            cache.set(key, view, ttl)
        return view

    @app.post("/resources/{resource_id:path}/transition")
    def do_transition(resource_id: str, req: "TransitionRequest") -> dict[str, Any]:
        if reg.get(resource_id) is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        # `parse_state`, not `ResourceState(req.to)`: the gRPC surface accepts
        # "ACTIVE" and "active" alike, and a caller that gets a 409 over HTTP
        # for a string the other door accepted would be looking at a rule that
        # only exists because the two surfaces each did their own coercion.
        # An unknown *name* is still 422 (bad request), not 409 (conflict):
        # gRPC maps the same ValueError to INVALID_ARGUMENT, not
        # FAILED_PRECONDITION, and the two must agree on which failure this is.
        try:
            dst = parse_state(req.to)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            rec = reg.transition(resource_id, dst, reason=req.reason)
        except Exception as exc:  # LifecycleError
            raise HTTPException(409, str(exc)) from exc
        return _record_view(rec)

    @app.post("/resources/{resource_id:path}/invoke")
    def do_invoke(resource_id: str, req: "InvokeRequest") -> dict[str, Any]:
        if reg.get(resource_id) is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        result = reg.invoke(resource_id, req.arguments, force=req.force)
        return {
            "resource_id": resource_id,
            "ok": bool(getattr(result, "ok", True)),
            "output": getattr(result, "output", None),
            "error": getattr(result, "error", None),
        }

    @app.post("/resources/{resource_id:path}/evolve")
    def do_evolve(resource_id: str, req: "EvolveRequest") -> dict[str, Any]:
        if reg.get(resource_id) is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        op = _op(reg)
        proposal = op.propose(resource_id, req.goal)
        report = op.assess(proposal.proposal_id)
        payload: dict[str, Any] = {
            "proposal_id": proposal.proposal_id,
            "assessment": report.to_dict(),
        }
        if req.commit and report.admissible:
            from toolmarket.protocol.sepl import ProposalRejected
            try:
                rec = op.commit(proposal.proposal_id)
                payload["committed"] = True
                payload["resource"] = _record_view(rec)
            except ProposalRejected as exc:
                payload["committed"] = False
                payload["reject_reason"] = str(exc)
        else:
            payload["committed"] = False
            payload["reject_reason"] = (
                report.summary if not report.admissible else "commit=False"
            )
        return payload

    @app.post("/resources/{resource_id:path}/evolve/async", status_code=202)
    def do_evolve_async(resource_id: str, req: "EvolveRequest") -> dict[str, Any]:
        """Enqueue an evolution and return immediately.

        Registered before the synchronous `/evolve` route on purpose. Both use
        `{resource_id:path}`, and a path converter is greedy — the shorter
        pattern is the one that must not be allowed first crack at a longer
        path. FastAPI matches in registration order, so the more specific route
        goes first.

        202, not 200: the work has been accepted and has not happened. A 200 here
        would tell every well-behaved client that the resource had evolved.
        """
        if reg.get(resource_id) is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        rec = app.state.queue.submit(resource_id, req.goal,
                                     commit=req.commit, proposer=req.proposer)
        return {
            "task_id": rec.task_id,
            "state": rec.state,
            "queue": rec.queue,
            "status_url": f"/tasks/{rec.task_id}",
        }

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        """Poll one async evolution.

        Served from the task store, which is the cache, because a task record is
        operational state and not substrate history — the durable record of what
        an evolution did is the events and lineage it wrote, and those are
        already queryable per resource. Duplicating them into a tasks table would
        give the substrate two places to disagree about the same fact.
        """
        rec = app.state.queue.status(task_id)
        if rec is None:
            raise HTTPException(404, f"unknown task: {task_id}")
        payload = rec.to_dict()
        payload["duration_seconds"] = rec.duration
        payload["terminal"] = rec.state in (TaskState.SUCCESS.value,
                                           TaskState.FAILURE.value)
        return payload

    return app


# The compiler moved to `toolmarket.protocol.resources.compile_tool_fn` when the
# gRPC surface landed — two front doors, one compiler. Re-exported here under
# its original private name so nothing that already spells it `_compile_fn`
# breaks for the sake of a layering argument.
_compile_fn = compile_tool_fn


# Module-level app for `uvicorn toolmarket.api.main:app`.
#
# `app` is the bare substrate; `served_app` is the same object wrapped in the
# rate limiter and the metrics middleware. They are two names rather than one
# because `web.build_app` mounts `create_app()` under `/api` and then wraps the
# *outer* app — so a single instrumented global would be instrumented twice, and
# every request would be counted twice. Containers run `served_app`; the console
# builds its own.
try:  # pragma: no cover
    if _HAVE_FASTAPI:
        app = create_app()
    else:
        app = None
except Exception:  # pragma: no cover
    app = None


def create_served_app(registry: Optional[ResourceRegistry] = None) -> Any:
    """The app as a deployment serves it: limiter + metrics + routes."""
    from toolmarket.ratelimit import install

    return install(create_app(registry))


try:  # pragma: no cover
    served_app = create_served_app() if _HAVE_FASTAPI else None
except Exception:  # pragma: no cover
    served_app = None


__all__ = ["create_app", "create_served_app", "served_app", "app",
           "ResourceRecord", "ToolContract", "VersionStatus"]
