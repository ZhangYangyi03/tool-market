"""The dense leg must actually receive vectors, or "hybrid" is a lie.

Measured 2026-10-25 on a live shelf (10 resources): `/search` reported
``mode: hybrid`` and ``rerank: true`` for every query, and `/search/health`
carried the warning ``10 lexical rows have no vector``. Both were true at once
because ``Store.index_vector`` had no caller anywhere in the tree -- nothing ever
embedded a document. The dense leg was therefore empty on every deployment, and
hit@5 could not have differed from bm25's no matter which EMBED_* was set.

These tests pin the two halves of the fix: `missing_vectors` must name exactly
the documents the dense leg cannot see, and `backfill_vectors` must write them
once, not twice, and not again until their text changes.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from toolmarket import embed as _embed
from toolmarket import retrieval
from toolmarket import vectorstore as vs


DIM = 8


def _fake_vector(text):
    """Deterministic, unit-length, and different for different text."""
    raw = hashlib.sha256(text.encode("utf-8")).digest()[:DIM]
    vec = [(b - 127.5) for b in raw]
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


@pytest.fixture()
def store(tmp_path):
    st = vs.Store(str(tmp_path / "idx.sqlite"))
    st.create_collection("tools", dim=DIM, model_id="fake-embed")
    st.index_text("tools", "tool:a", "read a file from disk and print it")
    st.index_text("tools", "tool:b", "format seconds as hh mm ss")
    return st


@pytest.fixture()
def embedder(monkeypatch):
    calls = []

    def settings():
        return {"api_key": "test", "model": "fake-embed", "dim": DIM, "batch": 32,
                "base_url": "http://example.invalid", "timeout": 1}

    def embed_texts(texts, s=None, batch=None, retries=2):
        texts = list(texts)
        calls.append(texts)
        return [_fake_vector(t) for t in texts]

    monkeypatch.setattr(_embed, "settings", settings)
    monkeypatch.setattr(_embed, "embed_texts", embed_texts)
    return calls


def test_missing_vectors_names_every_document_before_backfill(store):
    assert sorted(store.missing_vectors("tools")) == ["tool:a", "tool:b"]


def test_backfill_writes_one_vector_per_missing_document(store, embedder):
    records = [{"id": "tool:a", "name": "read_file", "description": "read a file"},
               {"id": "tool:b", "name": "seconds_to_hms", "description": "format seconds"}]
    report = retrieval.backfill_vectors(store, "tools", records=records)
    assert report["embedded"] == 2, report
    assert report["remaining"] == 0, report
    assert report["model"] == "fake-embed"
    assert store.stats("tools") == {"lexical": 2, "vectors": 2}
    assert store.missing_vectors("tools") == []


def test_backfill_is_idempotent(store, embedder):
    records = [{"id": "tool:a", "name": "a", "description": "read a file"},
               {"id": "tool:b", "name": "b", "description": "format seconds"}]
    retrieval.backfill_vectors(store, "tools", records=records)
    before = len(embedder)
    again = retrieval.backfill_vectors(store, "tools", records=records)
    assert again["candidates"] == 0 and again["embedded"] == 0
    assert len(embedder) == before, "unchanged text must not be embedded again"


def test_a_changed_document_is_embedded_again(store, embedder):
    records = [{"id": "tool:a", "name": "a", "description": "read a file"},
               {"id": "tool:b", "name": "b", "description": "format seconds"}]
    retrieval.backfill_vectors(store, "tools", records=records)
    store.index_text("tools", "tool:a", "push commits to a remote git repository")
    assert store.missing_vectors("tools") == ["tool:a"]
    records[0]["description"] = "push commits to a remote git repository"
    again = retrieval.backfill_vectors(store, "tools", records=records)
    assert again["embedded"] == 1, again


def test_without_a_key_it_reports_rather_than_raises(store, monkeypatch):
    monkeypatch.setattr(_embed, "settings",
                        lambda: {"api_key": "", "model": "m", "dim": DIM, "batch": 32,
                                 "base_url": "", "timeout": 1})
    report = retrieval.backfill_vectors(store, "tools")
    assert report["embedded"] == 0
    assert "no embedding api key" in (report["error"] or "")
    assert store.stats("tools")["vectors"] == 0


def test_a_collection_with_no_dimension_is_refused_explicitly(tmp_path, embedder):
    st = vs.Store(str(tmp_path / "nodim.sqlite"))
    st.create_collection("tools")
    st.index_text("tools", "tool:a", "read a file")
    report = retrieval.backfill_vectors(st, "tools")
    assert report["embedded"] == 0
    assert "no vector space" in (report["error"] or "")


def test_after_backfill_search_ranks_with_both_legs(store, embedder, monkeypatch):
    """The point of the backfill: `mode: hybrid` stops meaning "lexical only"."""
    docs = {"tool:a": "read a file from disk and print it",
            "tool:b": "format seconds as hh mm ss"}
    retrieval.backfill_vectors(store, "tools", documents=docs)
    monkeypatch.setattr(_embed, "embed_query", lambda q, s=None: _fake_vector(q))
    rows, info = retrieval.best_search(store, "tools", "format seconds as hh mm ss",
                                       k=2, documents=docs, use_rerank=False)
    assert info["mode"] == "hybrid"
    assert rows and rows[0][0] == "tool:b"
    assert store.stats("tools")["vectors"] == 2
