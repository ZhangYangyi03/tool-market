"""FastAPI surface over the resource substrate.

Routes are thin: each one calls exactly one registry/operator method. The API is
not where the semantics live, and it is not allowed to become so — a route that
decided a resource's state by itself would be a second, unaudited state machine.

    GET  /health
    GET  /stats
    GET  /events                      (optionally ?resource_id=)
    GET  /resources                   (?type= &state=)
    POST /resources                   register a tool
    GET  /resources/{id}
    POST /resources/{id}/transition   the enforcement lifecycle
    POST /resources/{id}/invoke
    GET  /resources/{id}/lineage
    GET  /resources/{id}/events
    POST /resources/{id}/evolve       the SEPL closed loop (propose→assess→commit)
"""
from __future__ import annotations

from typing import Any, Optional

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
    _HAVE_FASTAPI = True
except Exception:  # noqa: BLE001 - API extras are optional
    _HAVE_FASTAPI = False

from toolmarket.protocol.lifecycle import ResourceState, VersionStatus
from toolmarket.protocol.resources import ResourceRecord, ToolContract
from toolmarket.registry import ResourceRegistry


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

    def _op(reg_: ResourceRegistry):
        from toolmarket.protocol.sepl import EvolutionOperator
        return EvolutionOperator(reg_)

    # -- meta -------------------------------------------------------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "resources": len(reg.list()),
                "events": len(reg.log), "chain_ok": reg.log.verify_chain()}

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

    @app.get("/resources/{resource_id:path}")
    def get_resource(resource_id: str) -> dict[str, Any]:
        rec = reg.get(resource_id)
        if rec is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        return _record_view(rec)

    @app.post("/resources/{resource_id:path}/transition")
    def do_transition(resource_id: str, req: "TransitionRequest") -> dict[str, Any]:
        if reg.get(resource_id) is None:
            raise HTTPException(404, f"unknown resource: {resource_id}")
        try:
            dst = ResourceState(req.to)
        except ValueError as exc:
            raise HTTPException(422, f"unknown state: {req.to}") from exc
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

    return app


def _compile_fn(code: str, name: str) -> Any:
    """Compile submitted code into a callable, if any, without executing it."""
    if not code.strip():
        def _noop(**kwargs: Any) -> str:
            return f"{name}: no code registered"

        return _noop
    ns: dict[str, Any] = {}
    try:
        exec(compile(code, f"<tool:{name}>", "exec"), ns)  # noqa: S102
    except Exception:  # noqa: BLE001 - a tool that will not compile is callable-nowhere
        def _broken(**kwargs: Any) -> str:
            return f"{name}: code failed to compile"

        return _broken
    # Prefer a function named after the tool, else the first callable defined.
    fn = ns.get(name)
    if callable(fn):
        return fn
    for key, val in ns.items():
        if callable(val) and not key.startswith("_"):
            return val

    def _empty(**kwargs: Any) -> None:
        return None

    return _empty


# Module-level app for `uvicorn toolmarket.api.main:app`.
try:  # pragma: no cover
    if _HAVE_FASTAPI:
        app = create_app()
    else:
        app = None
except Exception:  # pragma: no cover
    app = None


__all__ = ["create_app", "ResourceRecord", "ToolContract", "VersionStatus"]
