"""The Celery worker process — `celery -A toolmarket.worker:celery_app worker`.

Run as its own container. It builds its **own** `ResourceRegistry` against the
same store the API uses, which is the whole reason the store is swappable: a
worker that could not see the API's resources would be a worker that reports
"unknown resource" for everything, and that failure is silent until the async
path is exercised by hand. With `DATABASE_URL` set to Postgres in both
containers, the worker sees exactly what the API sees; with the default SQLite
the two processes would each have a private substrate and the async endpoints
would be broken in a way no unit test would notice. `tests/test_tasks.py` pins
this by asserting the worker path reads a record the API wrote.

Configuration comes entirely from the environment:

    CELERY_BROKER_URL     default redis://localhost:6379/0
    CELERY_RESULT_BACKEND default: the broker
    TASK_QUEUE            set to `celery` in the API so it enqueues instead of
                          running inline
"""
from __future__ import annotations

import os
from typing import Any, Optional

try:
    from celery import Celery
    _HAVE_CELERY = True
    _CELERY_ERR: Optional[BaseException] = None
except Exception as _exc:  # noqa: BLE001
    Celery = None  # type: ignore[assignment]
    _HAVE_CELERY = False
    _CELERY_ERR = _exc

DEFAULT_BROKER = "redis://localhost:6379/0"


def _require() -> None:
    if not _HAVE_CELERY:
        raise RuntimeError(
            "the worker needs celery, which failed to import: "
            f"{_CELERY_ERR!r}. Install the extra: pip install 'tool-market[worker]'"
        )


def build_celery(broker: Optional[str] = None,
                 backend: Optional[str] = None) -> Any:
    """Construct the Celery app. A function, not just a module global, so tests
    can build one against a throwaway broker without import side effects."""
    _require()
    broker_url = broker or os.environ.get("CELERY_BROKER_URL") or DEFAULT_BROKER
    result_url = (backend or os.environ.get("CELERY_RESULT_BACKEND")
                  or broker_url)
    app = Celery("toolmarket", broker=broker_url, backend=result_url)

    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # Late acknowledgement + reject-on-worker-lost: the default (ack on
        # receipt) loses a task outright if the worker is killed mid-run. For an
        # evolution that has already spent model calls, re-running is cheaper
        # than dropping it.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        # An evolution runs candidate code; it can hang. A hard ceiling means a
        # wedged candidate costs one task slot for ten minutes, not forever.
        task_time_limit=600,
        task_soft_time_limit=540,
        result_expires=3600,
        timezone="UTC",
        broker_connection_retry_on_startup=True,
    )

    @app.task(bind=True, name="toolmarket.evolve", max_retries=0)
    def evolve(self: Any, resource_id: str, goal: str, task_id: str,
               commit: bool = True, proposer: str = "stub") -> dict[str, Any]:
        """Run one evolution in the worker process.

        Progress is mirrored into the cache as each stage begins, because
        Celery's own states are coarse (`STARTED` / `SUCCESS`) and a client
        polling an evolution wants to know whether it is stuck in *assessing* —
        the stage that runs untrusted code — or waiting in the queue.
        """
        from toolmarket import metrics as _metrics
        from toolmarket.tasks import TaskRecord, TaskState, TaskStore, execute_evolution

        store = TaskStore()

        def progress(stage: str, pct: float) -> None:
            rec = store.get(task_id) or TaskRecord(task_id=task_id,
                                                   resource_id=resource_id)
            rec.state = TaskState.RUNNING.value
            rec.started_at = rec.started_at or __import__("time").time()
            rec.stage = stage
            rec.progress = pct
            rec.queue = "celery"
            store.save(rec)
            self.update_state(state="PROGRESS",
                              meta={"stage": stage, "progress": pct})

        rec = store.get(task_id) or TaskRecord(task_id=task_id,
                                              resource_id=resource_id,
                                              queue="celery")
        rec.state = TaskState.RUNNING.value
        rec.started_at = __import__("time").time()
        rec.stage = "queued"
        rec.queue = "celery"
        store.save(rec)

        try:
            result = execute_evolution(
                resource_id, goal, commit=commit, proposer=proposer,
                task_id=task_id, registry=None, store=store, progress=progress,
            )
        except Exception as exc:  # noqa: BLE001
            rec.state = TaskState.FAILURE.value
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.finished_at = __import__("time").time()
            store.save(rec)
            _metrics.EVOLUTIONS.inc(outcome="error")
            # Re-raise so Celery records a FAILURE state too — the result backend
            # is what a restarted API reads, and swallowing here would leave it
            # reporting success for a run that never finished.
            raise
        rec.state = TaskState.SUCCESS.value
        rec.result = result
        rec.progress = 1.0
        rec.finished_at = __import__("time").time()
        store.save(rec)
        _metrics.EVOLUTIONS.inc(outcome="committed" if result.get("committed")
                                else "rejected")
        return result

    return app


# Module-level app for `celery -A toolmarket.worker:celery_app`. Guarded so
# importing this module on a machine that has no celery (and has no intention of
# running a worker) does not explode — the API's optional-extra pattern, applied
# to the worker.
celery_app: Any = None
if _HAVE_CELERY:  # pragma: no cover - exercised by the worker container
    try:
        celery_app = build_celery()
    except Exception:  # noqa: BLE001
        celery_app = None


__all__ = ["build_celery", "celery_app", "DEFAULT_BROKER"]
