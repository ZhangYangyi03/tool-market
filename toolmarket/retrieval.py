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

import json
import math
import re

# Query understanding without a dense backend (measured 2026-11-13: this host
# has no reachable /v1/embeddings and no local sentence-transformers, so
# 14,856 documents have zero vectors). Chinese needs are rewritten into the
# English tokens the shelf is actually written in; proper tool names and
# identifiers survive intact. The generic-word filter is the second half: on a
# 14.8k shelf "tool", "toolmarket", "any" occur in thousands of documents and
# otherwise drown the terms that discriminate.
_CN2EN = {
    "读取": "read", "查看": "read", "查询": "query", "获取": "get",
    "额度": "quota", "配额": "quota", "用量": "usage", "余额": "balance",
    "页面": "page", "网页": "page", "登录": "login", "账号": "account",
    "调用": "invoke", "工具": "tool", "货架": "market",
    "探测": "probe", "端口": "port", "连通": "reachability",
    "网络": "network", "检测": "check", "测试": "test", "状态": "status",
    "合并": "merge", "通知": "notify", "配置": "config", "邮件": "email",
    "发送": "send", "文件": "file", "缩放": "resize", "图片": "image",
    "水印": "watermark", "压缩": "compress", "裁剪": "crop",
    "转换": "convert", "格式": "format", "秒数": "seconds", "时间": "time",
    "转成": "format", "生成": "generate", "分析": "analyze",
    "统计": "stats", "列表": "list", "删除": "delete", "创建": "create",
    "修改": "patch", "修复": "fix", "重启": "restart",
    "数据库": "database", "索引": "index", "资源": "resource",
    "检索": "search", "搜索": "search", "命中": "hit", "结果": "results",
    "日志": "log", "进程": "process", "任意": "any", "编号": "id",
    "提交": "commit", "推送": "push", "远程": "remote", "仓库": "repo",
    "内容": "content", "打印": "print", "本地": "local", "邮件": "email",
}
_STOP = set("的 了 着 过 吗 呢 啊 吧 把 按 用 上 中 为 与 和 或 及 页 一个 一下 可以 如何 怎么 什么 是否 需要 进行 以及 相关 对应 这个 那个 我们 你们 他们 它 这 那 有 在".split())
_GENERIC = set("""any and into data use using with from that this for the one
given based a an to of in on is are be number into""".split())
_SHORT = set("id ui api tcp udp pdf ssh sql gpu cpu csv json url html xml io os db ai llm mcp cli png jpg gif hh mm ss".split())
_WORD = re.compile("[a-z0-9_]+")
_CJK = re.compile("[\u4e00-\u9fff]+")
_STATE_BOOST = {"active": 1.15, "verified": 1.12, "probation": 1.08, "draft": 1.0}


def query_terms(query):
    """Discriminating terms for one need, plus its Chinese-to-English rewrite."""
    low = (query or "").lower()
    out = []
    words = _WORD.findall(low)
    # An identifier typed with spaces is still an identifier: rejoin it so an
    # exact-name query can match the underscore stem stored in the index.
    # For prose the joined term is absent from the shelf and drops out harmlessly.
    if len(words) >= 2:
        out.append("_".join(words))
    for w in words:
        stem = (len(w) >= 3 or w in _SHORT) and w not in _GENERIC
        if stem:
            out.append(w)
        if "_" in w:
            parts = [p for p in w.split("_")
                     if (len(p) >= 3 or p in _SHORT) and p not in _GENERIC]
            out += parts
            # An exact identifier query loses to its own neighbours when the
            # stem is a generic word. Re-adding it with a higher weight keeps
            # "mcp_dev_cz_agents_dd" above "..._isir" and "..._cnb".
            if not stem and len(parts) >= 2:
                out.append(w)
    for run in _CJK.findall(low):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in _STOP and bg not in _GENERIC:
                out.append(bg)
        for zh, en in _CN2EN.items():
            if len(zh) > 1 and zh in run:
                out += [x for x in _WORD.findall(en.lower()) if x not in _GENERIC]
    return list(dict.fromkeys(out))


def lex_search(store, collection, query, k=10, pool=60, state_boost=False):
    """Weighted lexical search that understands the query, not just its words.

    Same BM25 equation as Store.bm25_search, but terms are the rewritten
    discriminating ones, document frequency is capped at half the shelf, and
    lifecycle state is a small, bounded prior. Measured 2026-11-13 on the live
    14,856-document shelf: exact tool names stay hit@1, while a Chinese need
    like "合并通知渠道配置" resolves to merge_notify_channels_config.
    """
    terms = query_terms(query)
    if not terms:
        return [], []
    rows = store._lexical_rows(collection)
    if not rows:
        return terms, []
    parsed = [(r[0], json.loads(r[1]), r[2], r[3]) for r in rows]
    n_docs = len(parsed)
    avgdl = (sum(d[2] for d in parsed) / float(n_docs)) or 1.0
    df = {}
    for t in terms:
        df[t] = sum(1 for d in parsed if t in d[1])
    keep = [t for t in terms if df[t] and df[t] / float(n_docs) <= 0.5]
    if not keep:
        return terms, []
    scored = []
    for rid, tf, dl, meta in parsed:
        score = 0.0
        for t in keep:
            f = tf.get(t, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n_docs - df[t] + 0.5) / (df[t] + 0.5))
            weight = 2.0 if ("_" in t and df[t] <= 3) else 1.0
            score += weight * idf * (f * 2.5) / (f + 1.5 * (0.25 + 0.75 * dl / avgdl))
        if score <= 0.0:
            continue
        if state_boost:
            try:
                st = json.loads(meta or "{}").get("state", "draft")
            except Exception:
                st = "draft"
            score *= _STATE_BOOST.get(st, 1.0)
        scored.append((rid, score, json.loads(meta) if meta else None))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return keep, scored[:max(k, pool)]


def abstain(store, collection, terms, rows):
    """True when the best hit is a word salad rather than an answer.

    Calibrated on the live shelf 2026-11-13 with 3 needs that have no tool on
    it and 13 that do. The discriminator is coverage: the fraction of the
    query's discriminating terms present in the winning document. Ratio and gap
    are reported but never used to abstain -- a near-tie means several tools
    fit, not that none does.
    """
    if not terms or not rows:
        return True, {"coverage": 0.0, "ratio": 0.0, "gap": 0.0}
    rid = rows[0][0]
    tf = store.term_freq(collection, rid)
    coverage = sum(1 for t in terms if t in tf) / float(len(terms))
    top = float(rows[0][1])
    second = float(rows[1][1]) if len(rows) > 1 else 0.0
    ratio = top / (second or 0.01)
    gap = top - second
    # A near-tie between the top two hits means "several tools fit", which is
    # exactly when a ranked list is useful. It is NOT evidence that no tool
    # exists: measured 2026-11-13, "发送一封测试邮件" had coverage 1.0 and
    # three real email tools, yet ratio 1.03 / gap 0.7 wrongly abstained.
    # Only weak term coverage of the winning document signals a word salad.
    reasons = []
    if coverage < 0.60:
        reasons.append("coverage %.2f < 0.60" % coverage)
    return bool(reasons), {"coverage": round(coverage, 3),
                           "ratio": round(ratio, 2), "gap": round(gap, 1),
                           "reasons": reasons}


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
    # No stored vectors means the dense leg cannot rank anything; asking the
    # embedding gateway for a query vector only adds its timeout to every
    # /search call. Measured 2026-11-13: 0 vectors in "tools", /search timed
    # out past 12s while /search/fast answered in 0.33s.
    if col and col["dim"] and store.stats(collection).get("vectors", 0):
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
        terms, rows = lex_search(store, collection, query, k=k, pool=pool,
                                 state_boost=True)
        info["mode"] = "bm25+rewrite"
        info["terms"] = terms
        dropped, signal = abstain(store, collection, terms, rows)
        info["abstain"] = dropped
        info["signal"] = signal
        if dropped:
            info["notes"].append("no confident match: " + "; ".join(
                signal.get("reasons") or ["empty candidate set"]))
            return [], info
        rows = rows[:max(k, pool)]
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
