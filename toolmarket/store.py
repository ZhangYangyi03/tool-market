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
                "path": self.path}
