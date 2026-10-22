"""Rerank the top of a retrieval list with a chat model, when there is no embedder.

Why this file exists. `retrieval.best_search` reranks with a cross-encoder
(`bge-reranker-v2-m3`) reached through the same gateway as the embedder. That
gateway answered fine when the pipeline was designed and answers 401 on this
machine on 2026-10-22 -- which means the dense leg is skipped, rerank is skipped
with it, and `/search` degrades to bm25 alone while still calling itself
"hybrid" in the response envelope. A reranker that depends on the embedder is a
reranker that dies with it.

A chat model is a different dependency and, on this host, a working one:
`wbproxy` on 127.0.0.1:8788 serves `deepseek-v4.1-flash` in ~2.6s. Asked to pick
the relevant names out of a candidate list it is a reranker, and a listwise one:
it sees all the candidates at once, so it cannot suffer the pooling problem that
made the cross-encoder lose hit@5 when it reranked an RRF-fused pool.

Deliberately not a generator. It is never asked to answer the need, only to
order what bm25 already retrieved -- so it cannot invent a tool that is not on
the shelf, and this whole path costs one short completion per query instead of an
agent loop.

Cost control that is not optional:

  * the candidate list is capped (DEFAULT_POOL), because the prompt grows with it;
  * answers are cached in memory keyed by (query, candidate ids), because the same
    need is looked up repeatedly across a session and a rerank is deterministic
    at temperature 0;
  * 429 is retried with backoff instead of being reported as "no rerank", because
    the first measurement run against this proxy hit one and a single rate limit
    would otherwise have silently rewritten the score of that query;
  * any failure returns None, and None means *no answer*, never *nothing
    relevant*. The caller keeps its original order rather than reading a broken
    rerank as an empty shelf.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8788/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_POOL = 30
DEFAULT_TIMEOUT = 45.0
_CONFIG_PATHS = (
    os.environ.get("LLM_RERANK_CONFIG") or "",
    r"C:\Users\china\tools\wbproxy\config.json",
)

_CACHE: dict[str, list[int]] = {}
_CACHE_MAX = 512


def settings() -> dict:
    """Where the reranker talks, and whether it has a key at all."""
    base = (os.environ.get("LLM_RERANK_BASE_URL") or DEFAULT_BASE).rstrip("/")
    key = os.environ.get("LLM_RERANK_API_KEY") or ""
    model = os.environ.get("LLM_RERANK_MODEL") or DEFAULT_MODEL
    if not key:
        for path in _CONFIG_PATHS:
            if not path:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except Exception:                              # noqa: BLE001
                continue
            if isinstance(cfg, dict) and cfg.get("apiKey"):
                key = str(cfg["apiKey"])
                base = (os.environ.get("LLM_RERANK_BASE_URL")
                        or "http://%s:%s/v1" % (cfg.get("host", "127.0.0.1"),
                                                cfg.get("port", 8788)))
                model = os.environ.get("LLM_RERANK_MODEL") or cfg.get("defaultModel") or model
                break
    try:
        pool = int(os.environ.get("LLM_RERANK_POOL") or DEFAULT_POOL)
    except Exception:                                      # noqa: BLE001
        pool = DEFAULT_POOL
    try:
        timeout = float(os.environ.get("LLM_RERANK_TIMEOUT") or DEFAULT_TIMEOUT)
    except Exception:                                      # noqa: BLE001
        timeout = DEFAULT_TIMEOUT
    return {"base_url": base, "model": model, "api_key": key,
            "pool": pool, "timeout": timeout}


def describe() -> dict:
    s = settings()
    key = s["api_key"] or ""
    return {"base_url": s["base_url"], "model": s["model"], "pool": s["pool"],
            "api_key": (key[:5] + "..." + key[-3:]) if len(key) > 10 else ("<set>" if key else "<unset>"),
            "configured": bool(key), "cached": len(_CACHE)}


def _prompt(need: str, names: list[str], topn: int) -> str:
    lines = "\n".join("%d. %s" % (i + 1, n) for i, n in enumerate(names))
    return ("A user needs: %s\n\nCandidate tools on a shared shelf, names only:\n%s\n\n"
            "Pick at most %d that plausibly satisfy the need, most relevant first. "
            "Answer with the numbers only, comma separated. If none is relevant answer 0."
            % (need[:800], lines, topn))


def _post(body: dict, s: dict, tries: int = 3) -> str:
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(
            s["base_url"] + "/chat/completions", data,
            {"Content-Type": "application/json",
             "Authorization": "Bearer " + str(s["api_key"])})
        try:
            with urllib.request.urlopen(req, timeout=s["timeout"]) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            return ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as exc:                            # noqa: BLE001
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("rerank request failed: %s" % (last,))


def rerank(query: str, candidates: list[tuple[str, str]], k: int = 5) -> list[tuple[str, str]] | None:
    """Order `candidates` by the model's judgement. None means no answer.

    candidates: [(resource_id, name), ...] in the retriever's own order.
    Returns the chosen candidates in the model's order, or None when the model
    could not be asked -- never [] for a failure, so the caller can tell
    "nothing relevant" from "nobody answered".
    """
    if not query or not candidates:
        return None
    cands = candidates[:settings()["pool"]]
    key = hashlib.sha256(
        (query + "\x00" + "\x00".join(rid for rid, _ in cands)).encode("utf-8")
    ).hexdigest()
    cached = _CACHE.get(key)
    if cached is not None:
        by_pos = list(range(len(cands)))
        return [cands[i] for i in cached if i in by_pos][:k]
    s = settings()
    if not s["api_key"]:
        return None
    names = [n for _, n in cands]
    try:
        raw = _post({"model": s["model"], "temperature": 0,
                     "max_tokens": 48,
                     "messages": [{"role": "user",
                                   "content": _prompt(query, names, k)}]}, s)
    except Exception:                                       # noqa: BLE001
        return None
    picks: list[int] = []
    for tok in re.findall(r"\d+", raw or ""):
        i = int(tok) - 1
        if 0 <= i < len(cands) and i not in picks:
            picks.append(i)
    if not picks and "0" not in str(raw):
        return None                       # unparseable answer is no answer
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = picks
    return [cands[i] for i in picks][:k]


def order_all(query: str, candidates: list[tuple[str, str]], timeout: float | None = None) -> list[tuple[str, str]] | None:
    """A full ordering of the candidates, not a shortlist. None means no answer.

    `rerank` returns at most k and therefore *truncates* the retriever's list:
    measured on 15 labelled needs, hit@1 rose 0.33 -> 0.67 while hit@5 fell
    0.87 -> 0.73, because the model names ~4.6 tools and the ones it omits are
    gone rather than demoted. Ordering all of them keeps the tail available for
    fusion with bm25, which is the version that can improve hit@1 without
    paying for it in hit@5.
    """
    if not query or not candidates:
        return None
    cands = candidates[: settings()["pool"]]
    s = settings()
    if not s["api_key"]:
        return None
    names = [n for _, n in cands]
    body = {"model": s["model"], "temperature": 0, "max_tokens": max(64, 8 * len(names)),
            "messages": [{"role": "user", "content":
                          ("A user needs: %s\n\nCandidate tools (names only):\n%s\n\n"
                           "Reorder ALL of them by how well they satisfy the need, best first. "
                           "Answer with every number, comma separated, nothing else.")
                          % (query[:700], "\n".join("%d. %s" % (i + 1, n) for i, n in enumerate(names)))}]}
    try:
        raw = _post(body, s)
    except Exception:                                       # noqa: BLE001
        return None
    order: list[int] = []
    for tok in re.findall(r"\d+", raw or ""):
        i = int(tok) - 1
        if 0 <= i < len(cands) and i not in order:
            order.append(i)
    if not order:
        return None
    order += [i for i in range(len(cands)) if i not in order]
    return [cands[i] for i in order]
