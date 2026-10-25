"""Best-available retrieval over the tool shelf.

Measured on 112 labelled queries (a need -> the tool actually forged to satisfy
it), k=5, 153 resources. Figures are hit@1 / hit@5 / MRR:

    what the service does now (bm25 in api/main.py)   0.607 / 0.893 / 0.724
    bm25                                              0.607 / 0.893 / 0.724
    rrf fusion, k=60                                  0.741 / 0.929 / 0.818
    dense alone                                       0.732 / 0.955 / 0.825
    weighted dense+bm25, alpha=0.8                    0.750 / 0.955 / 0.829
    dense pool 10 -> cross-encoder rerank             0.777 / 0.955 / 0.847

The last row is the default here. Three findings worth keeping, because each
one contradicts a reasonable guess:

  * Rank fusion is not automatically better than either leg. RRF(k=60) lost to
    the dense leg alone on hit@5. A normalized weighted sum (alpha=0.8) beat
    both. Fusion needs to be measured, not assumed.

  * Reranking does not have to cost hit@5. An earlier measurement showed
    0.955 -> 0.920 and I blamed the cross-encoder. It was the pool: that run
    reranked an RRF-fused pool. Rerank a dense pool of 10 and hit@5 holds at
    0.955 while hit@1 rises. The damage was upstream of the reranker.

  * Usage frequency is a bad prior. Ranking by forge_events call counts made
    results monotonically worse even in-sample (alpha 0.001/0.005/0.02 ->
    hit@1 0.741/0.402/0.152 against 0.786 baseline), because the counts are
    dominated by general-purpose tools (forge_tool, browser_eval) that are
    nobody's answer to any specific need. Do not re-add it without
    conditioning the prior on topic.

The dense leg is skipped when the collection's model_id does not match the
current embedding model -- vectors from a different model are noise, and
answering from noise is worse than answering from bm25 alone. health() says so
out loud rather than quietly returning worse results.
"""
import os
import time

from . import embed as _embed
from . import vectorstore as _vs

DEFAULT_POOL = 10
DEFAULT_ALPHA = 0.8
RERANK_MODEL = "bge-reranker-v2-m3"


def _normalized(scores):
    if not scores:
        return {}
    hi = max(scores.values())
    lo = min(scores.values())
    if hi <= 0.0:
        return dict((k, 0.0) for k in scores)
    if hi == lo:
        return dict((k, 1.0) for k in scores)
    return dict((k, (v - lo) / (hi - lo)) for k, v in scores.items())


def weight_search(store, collection, query, vector, k=DEFAULT_POOL, alpha=DEFAULT_ALPHA):
    """Normalized weighted sum of the dense and lexical legs.

    Both legs are min-max normalized over the same candidate set so alpha is a
    real weight rather than a scale artefact of whichever leg happens to
    produce larger numbers.
    """
    dense = store.dense_search(collection, vector, k=max(k * 3, 30)) if vector else []
    lex = store.bm25_search(collection, query, k=max(k * 3, 30)) if query else []
    d = _normalized(dict((r[0], r[1]) for r in dense))
    b = _normalized(dict((r[0], r[1]) for r in lex))
    meta = {}
    for row in list(dense) + list(lex):
        meta.setdefault(row[0], row[2])
    if not d:
        rows = [(rid, b[rid] * (1.0 - alpha), meta.get(rid)) for rid in b]
    elif not b:
        rows = [(rid, d[rid] * alpha, meta.get(rid)) for rid in d]
    else:
        rows = [(rid, alpha * d.get(rid, 0.0) + (1.0 - alpha) * b.get(rid, 0.0),
                 meta.get(rid)) for rid in set(d) | set(b)]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows[:k]


def backfill_vectors(store, collection, records=None, documents=None, limit=None):
    """Give the dense leg something to read.

    Measured 2026-10-25 on a live shelf: `/search/health` said "10 lexical rows
    have no vector" and /search reported ``mode: hybrid`` -- because the embedder
    answered -- while the collection held zero vectors, so `weight_search` fell
    through to the lexical leg alone and the ranking was bm25's. The hybrid
    label was not wrong about intent and was wrong about fact.

    The cause was not a threshold or a flag: `Store.index_vector` had no caller
    anywhere in the tree. Nothing ever embedded a document, so the dense leg
    could only ever be empty, and hit@5 could only ever be bm25's 0.893 no
    matter what EMBED_* was set to.

    This embeds exactly the documents the store says it is missing, at
    content-hash granularity (see `Store.missing_vectors`), and writes them with
    the model id that produced them. Nothing is embedded twice for the same
    text; nothing is left un-embedded for a text that changed.

    Returns a report rather than raising: a shelf without an embedder is a
    supported state -- it is just a lexically-retrieved one, and `info["mode"]`
    already says so.
    """
    report = {"collection": collection, "candidates": 0, "embedded": 0,
              "failed": 0, "remaining": 0, "model": None, "error": None,
              "note": None}
    # The text that gets embedded has to be the text that was indexed, or the two
    # legs describe different documents. The lexical row keeps term frequencies,
    # not prose, so the caller supplies it: `records` (the same dicts the index
    # was built from -- exact by construction) or `documents` (id -> text).
    by_id = {}
    for rec in (records or []):
        rid = rec.get("id") or rec.get("name")
        if rid:
            by_id[rid] = _vs.build_resource_text(rec)
    for rid, text in (documents or {}).items():
        by_id[rid] = text
    try:
        col = None
        for c in store.list_collections():
            if c["name"] == collection:
                col = c
                break
        s = _embed.settings()
        if not s.get("api_key"):
            report["error"] = "no embedding api key; dense leg stays empty"
            return report
        if col is None or not col["dim"]:
            report["error"] = ("collection has no vector space (dim unset); "
                               "recreate it with a dim before backfilling")
            return report
        if col["model_id"] not in (None, s["model"]):
            report["error"] = ("collection holds %r vectors, embedder is %r; "
                               "refusing to mix models" % (col["model_id"], s["model"]))
            return report
        report["model"] = s["model"]
        missing = store.missing_vectors(collection)
        if limit is not None:
            missing = missing[:int(limit)]
        report["candidates"] = len(missing)
        if not missing:
            return report
        # The text that is embedded has to be the text that was indexed, or the
        # two legs describe different documents. `documents` lets a caller hand
        # over the registry's own prose (it is the same string the lexical leg
        # was built from); otherwise the stored metadata is used, and a document
        # whose text cannot be recovered is skipped and counted, not guessed.
        texts, ids, unknown = [], [], []
        for rid in missing:
            text = by_id.get(rid)
            if not text:
                unknown.append(rid)
                continue
            ids.append(rid)
            texts.append(text)
        if unknown:
            # Named, not silently skipped: an id with no recoverable text is a
            # document the dense leg will keep missing, and a count of 0 embedded
            # with no reason is how the empty vector space stayed invisible.
            report["failed"] = len(unknown)
            report["note"] = ("no text for %d document(s): %s"
                              % (len(unknown), ", ".join(unknown[:5])))
        if not texts:
            report["remaining"] = len(store.missing_vectors(collection))
            return report
        hashes = store.lexical_hashes(collection)
        vectors = _embed.embed_texts(texts, s=s)
        for rid, vec in zip(ids, vectors):
            try:
                store.index_vector(collection, rid, vec,
                                   meta={"name": rid.split(":", 1)[-1]},
                                   chash=hashes.get(rid), model_id=s["model"])
                report["embedded"] += 1
            except Exception:                                      # noqa: BLE001
                report["failed"] += 1
        report["remaining"] = len(store.missing_vectors(collection))
    except _embed.EmbedUnavailable as exc:
        report["error"] = "embed unavailable: %s" % exc
    except Exception as exc:                                       # noqa: BLE001
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
    return report


def best_search(store, collection, query, k=5, pool=DEFAULT_POOL, alpha=DEFAULT_ALPHA,
                use_rerank=True, rerank_model=None, documents=None):
    """Retrieve, then rerank the top of the list with a cross-encoder.

    Returns (rows, info). rows are (resource_id, score, meta); info records what
    actually ran, so a caller can report the mode truthfully instead of claiming
    the best path when a fallback fired.
    """
    info = {"mode": "bm25", "pool": 0, "rerank": False, "notes": [],
            "confidence": None}
    query = (query or "").strip()
    if not query:
        return [], info

    col = None
    for c in store.list_collections():
        if c["name"] == collection:
            col = c
            break
    model = None
    try:
        s = _embed.settings()
        if s.get("api_key"):
            model = s["model"]
    except Exception:
        model = None

    vector = None
    if col and col["dim"]:
        if model and col["model_id"] not in (None, model):
            info["notes"].append(
                "collection holds %r vectors but the embedder is %r; dense leg skipped"
                % (col["model_id"], model))
        else:
            try:
                vector = _embed.embed_query(query)
            except Exception as exc:
                info["notes"].append("embed failed, dense leg skipped: %s" % exc)

    if vector is not None:
        rows = weight_search(store, collection, query, vector, k=pool, alpha=alpha)
        info["mode"] = "hybrid"
    else:
        rows = store.bm25_search(collection, query, k=pool)
        info["mode"] = "bm25"
    info["pool"] = len(rows)
    if not rows:
        return [], info

    if use_rerank and info["mode"] == "hybrid":
        docs = [(documents or {}).get(rid) for rid, _s, _m in rows]
        if any(not d for d in docs):
            info["notes"].append("no document text supplied; rerank skipped")
        else:
            try:
                order = _embed.rerank_texts(query, docs, model=rerank_model)
                ranked = [(rows[i][0], sc, rows[i][2]) for i, sc in order if 0 <= i < len(rows)]
                if ranked:
                    rows = ranked
                    info["rerank"] = True
            except Exception as exc:
                info["notes"].append("rerank unavailable: %s" % exc)

    info["confidence"] = _confidence(rows, info)
    return rows[:k], info


def _confidence(rows, info, floor=None):
    """How far the top hit can be trusted, measured rather than asserted.

    Calibrated on the 112 labelled queries: pooled-20 rerank top-1 scores have a
    median of 0.980 and only 2 of 112 fall below 0.010, so a near-zero top score
    is the signature of a query with no relevant tool rather than of a hard
    query. The floor defaults to 0.01.

    This is a report, not a filter. Rows come back either way and the caller
    decides: silently dropping results would hide exactly the information that
    flagging them exposes.
    """
    if not rows:
        return {"top_score": None, "confident": False, "basis": "no candidates"}
    top = float(rows[0][1])
    if info.get("rerank"):
        limit = 0.01 if floor is None else float(floor)
        basis = "cross-encoder relevance (median 0.98 over 112 labelled queries)"
    else:
        # Fusion scores are normalized over the candidate set, so they only say
        # "best of these", never "good enough". Say that instead of dressing a
        # relative number up as an absolute one.
        limit = 0.5 if floor is None else float(floor)
        basis = "normalized fusion score; relative to this candidate set only"
    return {"top_score": top, "confident": top >= limit, "threshold": limit,
            "basis": basis}


def install_health_route(app, store, collection, records_fn):
    """Attach the index self-check as a route, so corruption is visible in prod."""
    from fastapi import HTTPException

    @app.get("/search/health")
    def search_health():
        try:
            model = None
            s = _embed.settings()
            if s.get("api_key"):
                model = s["model"]
            return store.health(collection, records=records_fn(), expected_model=model)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, "health check failed: %s" % exc) from exc
