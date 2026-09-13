"""SQLite persistence for resources, events and lineage.

The store is deliberately dumb: three append-mostly tables keyed by id, each
holding a serialised record. All semantics live in the protocol layer, so a
different backend (Postgres, a file tree, an object store) can replace this
without touching anything above it.

Two facts about the schema that matter:

  * The current record is persisted as a whole (`resources.json`), so a restart
    reloads exactly the in-memory projection without a migration.
  * Events are persisted with their `seq` as the primary key, which is what
    makes the chain verifiable after a reload — `EventLog.verify_chain` works on
    a store that has been closed and reopened.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from toolmarket.protocol.events import Event, EventLog
from toolmarket.protocol.lineage import LineageGraph, LineageNode
from toolmarket.protocol.resources import ResourceRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id          TEXT PRIMARY KEY,
    json        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY,
    resource_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    json         TEXT NOT NULL,
    at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_resource ON events(resource_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE TABLE IF NOT EXISTS lineage (
    node_id      TEXT PRIMARY KEY,
    resource_id  TEXT NOT NULL,
    json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_resource ON lineage(resource_id);
"""


class ResourceStore:
    """A thin persistence layer over SQLite. `path=':memory:'` for tests."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def ping(self) -> bool:
        """Liveness probe for the store itself — `/health` reports it.

        SQLite has no server to fail independently, so this is nearly always
        true; it exists so the health check does not have to special-case which
        backend is wired in. A closed connection is the case it catches.
        """
        try:
            self._conn.execute("SELECT 1").fetchone()
            return True
        except Exception:  # noqa: BLE001
            return False

    def wait_ready(self, attempts: int = 1, delay: float = 0.0) -> None:
        """No-op: a file-backed SQLite is ready the moment it opens.

        Present so callers can write one startup path against either backend
        instead of branching on the store type — which is the whole reason
        Postgres could be dropped in without touching the registry.
        """
        if not self.ping():
            raise RuntimeError(f"sqlite store at {self.path!r} is not usable")

    def __enter__(self) -> "ResourceStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- resources --------------------------------------------------------
    def save_resource(self, rec: ResourceRecord) -> None:
        self._conn.execute(
            "INSERT INTO resources(id, json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET json=excluded.json, "
            "updated_at=excluded.updated_at",
            (rec.id, json.dumps(rec.to_dict(), default=str), time.time()),
        )
        self._conn.commit()

    def load_resource(self, resource_id: str) -> Optional[ResourceRecord]:
        row = self._conn.execute(
            "SELECT json FROM resources WHERE id=?", (resource_id,)
        ).fetchone()
        return ResourceRecord.from_dict(json.loads(row[0])) if row else None

    def load_resources(self) -> list[ResourceRecord]:
        rows = self._conn.execute(
            "SELECT json FROM resources ORDER BY updated_at"
        ).fetchall()
        return [ResourceRecord.from_dict(json.loads(r[0])) for r in rows]

    # -- events -----------------------------------------------------------
    def append_event(self, event: Event) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events(seq, resource_id, kind, json, at) "
            "VALUES(?,?,?,?,?)",
            (event.seq, event.resource_id, event.kind.value,
             json.dumps(event.to_dict(), default=str), event.at),
        )
        self._conn.commit()

    def load_events(self) -> EventLog:
        rows = self._conn.execute(
            "SELECT json FROM events ORDER BY seq"
        ).fetchall()
        return EventLog([Event.from_dict(json.loads(r[0])) for r in rows])

    # -- lineage ----------------------------------------------------------
    def save_lineage_node(self, node: LineageNode) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO lineage(node_id, resource_id, json) "
            "VALUES(?,?,?)",
            (node.node_id, node.resource_id, json.dumps(node.to_dict(), default=str)),
        )
        self._conn.commit()

    def load_lineage(self) -> LineageGraph:
        rows = self._conn.execute(
            "SELECT json FROM lineage"
        ).fetchall()
        graph = LineageGraph()
        # Insert parents-first so add() validation passes regardless of order.
        pending = [LineageNode(**json.loads(r[0])) for r in rows]
        while pending:
            progressed = False
            for node in list(pending):
                if all(p in graph for p in node.parents):
                    graph.add(node)
                    pending.remove(node)
                    progressed = True
            if not progressed:
                # Orphans (missing parents) are force-added so a partial store
                # still loads rather than wedging.
                for node in pending:
                    graph._nodes[node.node_id] = node  # noqa: SLF001
                break
        return graph

    # -- aggregate --------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        n_res = self._conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        n_ev = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        n_ln = self._conn.execute("SELECT COUNT(*) FROM lineage").fetchone()[0]
        return {"resources": n_res, "events": n_ev, "lineage_nodes": n_ln,
                "backend": "sqlite", "path": self.path}


def make_store(url: Optional[str] = None) -> Any:
    """Pick a backend from a URL, so the choice is configuration and not code.

    Accepted:

        (unset) / ""        -> ResourceStore(":memory:")   in-process, disposable
        ":memory:"          -> same
        "sqlite:///path"    -> ResourceStore("path")
        "postgres://..."    -> PostgresStore(...)
        "postgresql://..."  -> PostgresStore(...)

    The URL is read from `url`, else `TOOLMARKET_STORE`, else `DATABASE_URL`
    (the name every PaaS injects when you attach a database, so a deploy needs
    no bespoke variable to pick up the database it was given).

    Resolution lives here rather than in the registry because the registry must
    not know which backends exist — otherwise adding one means editing the
    protocol layer, and that is exactly the coupling the store boundary is for.
    Importing `store_pg` is deferred to the Postgres branch so a machine without
    psycopg2 never touches it.
    """
    raw = (url or os.environ.get("TOOLMARKET_STORE")
           or os.environ.get("DATABASE_URL") or "").strip()
    if not raw or raw == ":memory:":
        return ResourceStore(":memory:")
    if raw.startswith(("postgres://", "postgresql://")):
        from toolmarket.store_pg import PostgresStore

        store = PostgresStore(raw)
        store.wait_ready(attempts=30, delay=1.0)
        return store
    if raw.startswith("sqlite://"):
        # sqlite:///abs/path -> /abs/path ; sqlite:///:memory: -> :memory:
        rest = raw[len("sqlite://"):]
        if rest.startswith("/") and not rest.startswith("//"):
            return ResourceStore(rest)
        return ResourceStore(rest.lstrip("/") or ":memory:")
    # Anything else is treated as a filesystem path -- the CLI's documented way
    # of asking for a durable substrate without standing up a server.
    return ResourceStore(raw)
